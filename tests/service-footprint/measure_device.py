#!/usr/bin/env python3
"""Flash a qualification image and measure its NSH heap before/after execution."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serial_io import command, read_prompt, set_serial
from rtos_harness.device import open_serial as _open_serial


MEMORY_ROW = re.compile(
    r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+Umem\s*$',
    re.MULTILINE,
)
ANSI_ESCAPE = re.compile(rb'\x1b\[[0-9;]*[A-Za-z]')
SILENT_SUCCESS = 'NXRS_SILENT_EXIT_0'
SILENT_FAILURE = 'NXRS_SILENT_EXIT_NONZERO'


def parse_free(output):
    rows = MEMORY_ROW.findall(output.decode(errors='replace').replace('\r', ''))
    if len(rows) != 1:
        raise ValueError(f'expected one Umem row, found {len(rows)}')
    return dict(zip(('total', 'used', 'free', 'maxused', 'maxfree', 'nused', 'nfree'),
                    map(int, rows[0])))


def count_output_lines(output, expected, *, prefix=False):
    """Avoid accepting a command echo or a partial diagnostic as app output."""
    lines = output.replace(b'\r', b'').splitlines()
    if prefix:
        return sum(line.startswith(expected.encode()) for line in lines)
    return lines.count(expected.encode())


def verify_silent_exit(app_output, then_output, else_output, app_command):
    """Check a silent NSH app through the shell's if/then/else exit status."""
    clean = ANSI_ESCAPE.sub(b'', app_output).replace(b'\r', b'')
    # read_prompt can stop between bytes of NSH's trailing clear-line escape.
    # Accept only an incomplete CSI immediately after the final shell prompt;
    # do not strip arbitrary app output or an incomplete app diagnostic.
    clean = re.sub(rb'(nsh>[ \t]*)\x1b(?:\[[0-9;]*)?$', rb'\1', clean)
    # NSH's ESC[K clear-line sequence can be split across serial reads,
    # leaving just `[K` or `K` at the beginning of the next command reply.
    lines = [line.strip().lstrip(b'\x1b[K') for line in clean.splitlines() if line.strip()]
    if lines != [f'if {app_command}'.encode(), b'nsh>']:
        raise RuntimeError(f'silent app emitted output: {app_output[-1200:]!r}')
    if (count_output_lines(then_output, SILENT_SUCCESS) != 1 or
            count_output_lines(else_output, SILENT_FAILURE) != 0):
        raise RuntimeError('silent app did not exit successfully')


def open_serial(port):
    """Compatibility re-export retaining this module's serial configurator seam."""
    return _open_serial(port, set_serial_fn=set_serial)


def flash_command(flasher, port, image):
    """Use the esptool v4 spelling also accepted by the pinned board tools."""
    return [flasher, '--chip', 'esp32s3', '--port', port, '--baud', '460800',
            'write_flash', '--flash_size', 'detect', '0x0', str(image)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--flasher', default='esptool.py')
    parser.add_argument('--command', default='cq_probe')
    parser.add_argument('--runs', type=int, default=3,
                        help='number of complete app invocations (default: 3)')
    parser.add_argument('--command-timeout', type=float, default=30,
                        help='seconds to wait for one app invocation (default: 30)')
    parser.add_argument('--expected-line', default='Hello, world!')
    parser.add_argument('--expected-prefix', help='match a complete line starting with this text')
    parser.add_argument('--silent-exit', action='store_true',
                        help='require no app output and a successful NSH if/then/else result')
    parser.add_argument('--skip-flash', action='store_true',
                        help='measure the already-flashed image after a harness retry')
    args = parser.parse_args()
    if args.runs < 1 or args.command_timeout <= 0:
        parser.error('--runs and --command-timeout must be positive')
    if args.silent_exit and args.expected_prefix:
        parser.error('--silent-exit cannot be combined with --expected-prefix')
    image = args.image.resolve()
    if not image.is_file():
        parser.error(f'missing image: {image}')
    if args.out.exists():
        parser.error(f'output already exists: {args.out}')
    args.out.mkdir(parents=True)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (args.out / 'image.sha256').write_text(f'{digest}  {image}\n')
    if args.skip_flash:
        (args.out / 'flash.log').write_text('Skipped: image must already be flashed.\n')
    else:
        flash = subprocess.run(
            flash_command(args.flasher, args.port, image),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180,
        )
        (args.out / 'flash.log').write_text(flash.stdout)
        flash.check_returncode()

    fd = open_serial(args.port)
    transcript = bytearray()
    runs = []
    failure = None
    try:
        try:
            transcript.extend(read_prompt(fd, 5))
        except TimeoutError:
            transcript.extend(command(fd, '', 35))
        for _ in range(args.runs):
            before_raw = command(fd, 'free', 10)
            started = time.monotonic()
            if args.silent_exit:
                app_raw = command(fd, f'if {args.command}', args.command_timeout)
                elapsed_ms = round((time.monotonic() - started) * 1000)
                then_raw = command(fd, f'then echo {SILENT_SUCCESS}', 5)
                else_raw = command(fd, f'else echo {SILENT_FAILURE}', 5)
                fi_raw = command(fd, 'fi', 5)
                silent_check = (app_raw, then_raw, else_raw, args.command)
                app_raw += then_raw + else_raw + fi_raw
            else:
                app_raw = command(fd, args.command, args.command_timeout)
                elapsed_ms = round((time.monotonic() - started) * 1000)
            after_raw = command(fd, 'free', 10)
            transcript.extend(before_raw + app_raw + after_raw)
            if args.silent_exit:
                verify_silent_exit(*silent_check)
            else:
                expected = args.expected_prefix or args.expected_line
                if count_output_lines(app_raw, expected, prefix=bool(args.expected_prefix)) != 1:
                    raise RuntimeError('expected app output missing or duplicated')
            before, after = parse_free(before_raw), parse_free(after_raw)
            runs.append({
                'before': before, 'after': after,
                'used_delta': after['used'] - before['used'],
                'high_water_delta': after['maxused'] - before['maxused'],
                'elapsed_ms': elapsed_ms,
            })
    except Exception as error:
        failure = f'{type(error).__name__}: {error}'
        transcript.extend(f'\nMEASUREMENT_FAILED after {len(runs)} runs: {failure}\n'.encode())
        raise
    finally:
        (args.out / 'serial.log').write_bytes(transcript)
        os.close(fd)
        (args.out / 'measurement.json').write_text(json.dumps({
            'schema': 1, 'image': str(image), 'image_sha256': digest,
            'port': args.port, 'command': args.command,
            'requested_runs': args.runs, 'completed_runs': len(runs),
            'failure': failure,
            'expected_line': None if args.silent_exit else args.expected_line,
            'expected_prefix': args.expected_prefix,
            'silent_exit': args.silent_exit,
            'flashed': not args.skip_flash,
            'runs': runs,
        }, indent=2) + '\n')
    print(f'FOOTPRINT_DEVICE_MEASUREMENT={args.out / "measurement.json"}')


if __name__ == '__main__':
    main()
