#!/usr/bin/env python3
"""Qualify the physical detector HAL over exclusive USB, without Wi-Fi/camera.

Timing covers the full HAL detect call and excludes serial transfer. This is not
an FPS test. A failed upload is bounded by the fixture's 30-second read deadline;
a model/target fault requires an explicit reset before another qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import struct
import sys
import time

RESULT = re.compile(rb'^NXRS_DETECT_RESULT index=(\d+) score=(\d+) inference_us=(\d+) boxes=(\d+) truncated=([01])$')


def line_until(port, predicate, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = port.readline().strip(b'\r\n')
        if any(marker in line for marker in (b'NXRS_STREAM_FAIL', b'PANIC', b'ASSERT')):
            raise RuntimeError('device reported a qualification failure')
        if predicate(line):
            return line
    raise TimeoutError('device response deadline')


def load_dataset(dataset):
    metadata_bytes = (dataset / 'dataset.json').read_bytes()
    metadata = json.loads(metadata_bytes)
    rows = []
    for index, line in enumerate((dataset / 'manifest.tsv').read_text().splitlines()):
        label, width, height, path = line.split('\t')
        payload = pathlib.Path(path).read_bytes()
        if (not 0 < len(payload) <= 153600 or label not in ('0', '1')
                or not 0 < int(width) <= 640 or not 0 < int(height) <= 480):
            raise ValueError('invalid prepared image')
        item = metadata['images'][index]
        if item['label'] != int(label) or hashlib.sha256(payload).hexdigest() != item['jpeg_sha256']:
            raise ValueError('prepared image identity/label differs from frozen corpus')
        rows.append((index, int(label), int(width), int(height), payload))
    if not rows or len(rows) != len(metadata['images']):
        raise ValueError('dataset count mismatch')
    return rows, hashlib.sha256(metadata_bytes).hexdigest()


def parse_result(line, index):
    match = RESULT.fullmatch(line)
    if match is None:
        raise ValueError('malformed native detector result')
    actual, score, elapsed, boxes, truncated = map(int, match.groups())
    if actual != index or not 0 <= score <= 1000 or elapsed <= 0 or boxes > 10:
        raise ValueError('invalid native detector result')
    return score, elapsed, boxes, bool(truncated)


def run(args):
    import serial
    from serial.tools import list_ports

    rows, dataset_hash = load_dataset(args.dataset.resolve())
    devices = [p for p in list_ports.comports() if p.device == args.port]
    if (len(devices) != 1 or (devices[0].vid, devices[0].pid, devices[0].serial_number)
            != (0x303a, 0x1001, args.expected_serial)):
        raise ValueError('physical camera identity differs')
    if args.scores.exists() or args.report.exists() or args.scores.resolve() == args.report.resolve():
        raise ValueError('qualification outputs must be distinct new files')
    args.scores.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        'schema': 1, 'dataset_sha256': dataset_hash,
        'mode': 'exclusive USB, camera/Wi-Fi not started; full HAL detect call timing excludes serial',
        'rows': [], 'passed': False,
    }
    try:
        with serial.Serial(args.port, 115200, timeout=.2, write_timeout=30, exclusive=True) as port, args.scores.open('x') as scores:
            port.reset_input_buffer()
            port.write(b'\r')
            # NSH's prompt has no trailing newline.
            deadline = time.monotonic() + 20
            prompt = bytearray()
            while b'nsh> ' not in prompt and time.monotonic() < deadline:
                prompt.extend(port.read(256))
            if b'nsh> ' not in prompt:
                raise TimeoutError('console is not idle')
            port.write(b'stream_test detect-batch\r')
            port.flush()
            line_until(port, lambda line: line == b'NXRS_DETECT_BATCH_READY')
            for index, label, width, height, payload in rows:
                port.write(struct.pack('<4sIIHH', b'NXP1', index, len(payload), width, height))
                port.flush()
                line_until(port, lambda line: line == f'NXRS_DETECT_CHUNK index={index} offset=0'.encode())
                # The qualified USB console has a 64-byte RX ring. Acknowledged
                # 32-byte chunks keep unread data bounded below its capacity.
                for offset in range(0, len(payload), 32):
                    end = min(offset + 32, len(payload))
                    port.write(payload[offset:end])
                    port.flush()
                    line_until(port, lambda line: line == f'NXRS_DETECT_CHUNK index={index} offset={end}'.encode())
                line = line_until(port, lambda line: line.startswith(b'NXRS_DETECT_RESULT'))
                score, elapsed, boxes, truncated = parse_result(line, index)
                scores.write(f'{index}\t{label}\t{score}\t{elapsed}\t0\n')
                scores.flush()
                evidence['rows'].append({
                    'index': index, 'jpeg_sha256': hashlib.sha256(payload).hexdigest(),
                    'score': score, 'inference_us': elapsed, 'boxes': boxes, 'truncated': truncated,
                })
                if (index + 1) % 100 == 0:
                    print(f'{index+1}/{len(rows)} complete', flush=True)
            port.write(struct.pack('<4sIIHH', b'NXP1', len(rows), 0, 0, 0))
            port.flush()
            line_until(port, lambda line: line == f'NXRS_DETECT_BATCH_PASS count={len(rows)}'.encode())
            evidence['passed'] = True
    except BaseException as error:
        # Preserve partial evidence; an interrupted/failed run cannot pass.
        evidence['error'] = type(error).__name__
        raise
    finally:
        with args.report.open('x') as output:
            output.write(json.dumps(evidence, indent=2) + '\n')
    print('PASS: native detector corpus completed and joined')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--expected-serial', required=True)
    parser.add_argument('--dataset', type=pathlib.Path, required=True)
    parser.add_argument('--scores', type=pathlib.Path, required=True)
    parser.add_argument('--report', type=pathlib.Path, required=True)
    run(parser.parse_args())
