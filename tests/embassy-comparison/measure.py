#!/usr/bin/env python3
"""Measure a frozen Embassy image locally; run through run_matrix.py to restore."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = load('embassy_serial', HERE.parent / 'zephyr-comparison' / 'measure.py')
marker_rows = shared.marker_rows


def read_command(fd, timeout=30):
    """Ignore a late boot/blank prompt, never mistake it for command completion."""
    data = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data.extend(shared.read_prompt(fd, deadline - time.monotonic(), b'embassy> '))
        if b'EMBASSY_COMMAND_EXIT' in data:
            return bytes(data)
    raise TimeoutError('Embassy command completion missing')


def validate(output, mode):
    text = output.decode(errors='replace').replace('embassy> ', '').replace('\r', '')
    if 'FAIL' in text or marker_rows(text, 'EMBASSY_COMMAND_EXIT', 1) != [{'status': 0}]:
        raise ValueError('Embassy command failed')
    if mode == 'baseline':
        row = marker_rows(text, 'EMBASSY_BASELINE_PASS', 1)[0]
        if row != {'heap_allocated': 0, 'shared_stack': 8192}:
            raise ValueError('baseline resource mismatch')
        return {'baseline': row}
    resources = marker_rows(text, 'EMBASSY_RESOURCES', 1)[0]
    ack_bytes = 28 if mode == 'packet' else 16
    expected = {'heap_allocated': 0, 'queue_buffers': 45 * 248 + 15 * 4 * ack_bytes,
                'shared_stack': 8192, 'tasks': 20, 'queues': 60}
    if (any(resources.get(k) != v for k, v in expected.items()) or
            type(resources.get('channel_storage')) is not int or
            resources['channel_storage'] < resources['queue_buffers']):
        raise ValueError('Embassy resource contract mismatch')
    transport = marker_rows(text, 'CQ_TRANSPORT_PASS', 1)[0]
    expected = {'language': 'embassy', 'mode': 'large', 'event_bytes': 248, 'ack_bytes': ack_bytes}
    if (any(transport.get(k) != v for k, v in expected.items()) or
            type(transport.get('cycles')) is not int or not 0 < transport['cycles'] < 3840000000):
        raise ValueError('Embassy clock/transport mismatch')
    scale = marker_rows(text, 'CQ_EMBASSY_SCALE_PASS', 1)[0]
    expected = {'mode': 'large', 'queues': 60, 'logical_streams': 60, 'tasks': 20,
                'messages': 5760, 'digest': 441445568, 'samples': 720}
    if any(scale.get(k) != v for k, v in expected.items()):
        raise ValueError('Embassy workload mismatch')
    for prefix in ('rx', 'ack'):
        values = [scale.get(f'{prefix}_{key}_us') for key in ('p50', 'p99', 'max')]
        if any(type(v) is not int or v < 0 for v in values) or values != sorted(values):
            raise ValueError('invalid Embassy latency quantiles')
    return {'transport': transport, 'scale': scale, 'resources': resources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--port', required=True)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--mode', required=True, choices=('wire', 'packet', 'baseline'))
    parser.add_argument('--runs', type=int, default=10)
    parser.add_argument('--flasher', required=True)
    args = parser.parse_args()
    if args.out.exists() or args.runs < 1 or not args.image.is_file():
        parser.error('fresh output, positive runs and real image required')
    args.out.mkdir(parents=True)
    command = 'baseline' if args.mode == 'baseline' else 'large'
    record = {'schema': 1, 'image_sha256': hashlib.sha256(args.image.read_bytes()).hexdigest(),
              'mode': args.mode, 'command': command, 'requested_runs': args.runs,
              'completed_runs': 0, 'runs': [], 'failure': None, 'flashed': False}
    transcript = bytearray()
    fd = None
    try:
        flashed = subprocess.run([args.flasher, '--chip', 'esp32s3', '--port', args.port,
            '--baud', '460800', 'write_flash', '--flash_mode', 'keep', '--flash_freq', 'keep',
            '--flash_size', 'keep', '0x0', str(args.image)], capture_output=True, text=True, timeout=180)
        (args.out / 'flash.log').write_text(flashed.stdout + flashed.stderr)
        flashed.check_returncode()
        record['flashed'] = True
        fd = shared.open_serial(args.port)
        # Request a prompt only if boot output was missed; otherwise an extra
        # blank prompt can race the first measured command.
        try:
            transcript.extend(shared.read_prompt(fd, 2, b'embassy> '))
        except TimeoutError:
            os.write(fd, b'\r')
            transcript.extend(shared.read_prompt(fd, 15, b'embassy> '))
        for _ in range(args.runs):
            os.write(fd, command.encode() + b'\r')
            output = read_command(fd)
            transcript.extend(output)
            record['runs'].append(validate(output, args.mode))
            record['completed_runs'] += 1
    except Exception as exc:
        record['failure'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if fd is not None:
            os.close(fd)
        (args.out / 'serial.log').write_bytes(transcript)
        (args.out / 'measurement.json').write_text(json.dumps(record, indent=2) + '\n')
    print(f'EMBASSY_MEASUREMENT_PASS runs={args.runs}')


if __name__ == '__main__':
    main()
