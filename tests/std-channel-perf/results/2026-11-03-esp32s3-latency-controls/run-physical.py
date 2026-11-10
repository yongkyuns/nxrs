"""Private operator for PR 259. Preserve raw failures and always restore flash."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import serial
from serial.tools import list_ports
from esptool.reset import HardReset

ROOT = Path(__file__).resolve().parent
REPO = Path('/private/tmp/nxrs-pr259-wroom-20261103')
PORT = '<serial-device>'
DEVICE = (0x303a, 0x1001, 'esp32s3-bench-01')
sys.path.insert(0, str(REPO / 'tests/std-channel-perf'))
import analyze


def save(name, value):
    temporary = ROOT / (name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(ROOT / name)


def event(phase, **fields):
    value = dict(phase=phase, unix=time.time(), **fields)
    save('phase.json', value)
    print(json.dumps(value), flush=True)


def identity():
    ports = [p for p in list_ports.comports() if p.device == PORT]
    assert len(ports) == 1
    p = ports[0]
    assert (p.vid, p.pid, p.serial_number) == DEVICE, 'USB identity changed'


def tool(arguments, log):
    identity()
    with log.open('xb') as output:
        result = subprocess.run([sys.executable, '-m', 'esptool', '--chip', 'esp32s3',
                                 '--port', PORT, '--baud', '921600', *arguments],
                                stdout=output, stderr=subprocess.STDOUT, timeout=300)
    assert result.returncode == 0, 'esptool failed; retained ' + log.name


def flash(image, prefix):
    event('flashing', image=prefix)
    tool(['write_flash', '--flash_mode', 'keep', '--flash_freq', 'keep',
          '--flash_size', 'keep', '0', str(image)], ROOT / (prefix + '-flash.log'))
    tool(['verify_flash', '0', str(image)], ROOT / (prefix + '-verify.log'))
    event('flash_verified', image=prefix)


def reset():
    identity()
    # The capture process already owns the data FD. This second FD only toggles
    # the documented USB-JTAG reset lines; it never reads or transmits data.
    console = serial.Serial(port=None, baudrate=115200, timeout=.1)
    console.dtr = console.rts = False
    console.port = PORT
    console.open()
    try:
        HardReset(console, uses_usb=True).reset()
    finally:
        console.close()


def run_case(label, warmup=100, seed=None, fixed=None):
    image_case = "latency-rom-digest"
    mode = "latency"
    build = ROOT / 'ci-firmware/esp32s3' / image_case
    receipt = analyze.build_receipt(build, mode)
    out = ROOT / 'runs' / label
    session = {
        'schema': 'std-channel-session-v1', 'runtime_kind': 'physical',
        'measurement_session': 'pr259-20261103-' + label,
        'board': 'ESP32-S3 QFN56 rev v0.2, 16 MiB quad flash / 8 MiB embedded octal PSRAM',
        'board_serial': DEVICE[2], 'image_sha256': receipt['image_sha256'],
        'active_cores': 1, 'cpu_clock_hz': 240000000,
        'clock_verification': 'No independent reference instrument available; absolute timing remains unqualified.',
        'cache_state': 'Resolved config: 16 KiB instruction and 32 KiB data cache, 8 ways, 32-byte lines; fresh boot for each run.',
        'memory_placement': 'Resolved N16R8 linker/image with octal PSRAM at 40 MHz in common heap; exact channel allocation address not instrumented.',
        'power_management': 'CONFIG_PM=n; configured fixed 240 MHz single-core build.',
        'external_load': 'Standalone benchmark NSH image; no camera/radio application started; normal kernel tick/USB interrupts; UART result output follows worker joins.',
        'flash_verification': image_case + '-flash.log and ' + image_case + '-verify.log: complete 16 MiB image written with header settings kept and esptool flash verification succeeded.'
    }
    session_path = ROOT / (label + '-session.json')
    session_path.write_text(json.dumps(session, indent=2) + '\n')
    args = [sys.executable, str(REPO / 'tests/std-channel-perf/capture.py'), 'physical',
            '--build-directory', str(build), '--mode', mode, '--samples', '1000',
            '--warmup', str(warmup), '--port', PORT, '--session', str(session_path),
            '--output', str(out), '--timeout', '120']
    if seed is not None:
        args.extend(['--phase-seed', str(seed)])
    if fixed is not None:
        args.extend(['--fixed-delay-us', str(fixed)])
    event('capturing', case=label)
    child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = bytearray()
    ready = False
    deadline = time.monotonic() + 135
    try:
        while child.poll() is None or select.select([child.stdout], [], [], 0)[0]:
            if time.monotonic() > deadline:
                raise TimeoutError('physical capture host deadline')
            if not select.select([child.stdout], [], [], .1)[0]:
                continue
            data = os.read(child.stdout.fileno(), 65536)
            if not data:
                break
            log.extend(data)
            if not ready and b'Serial capture ready.' in log:
                ready = True
                event('fresh_boot_reset', case=label)
                reset()
        code = child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        (ROOT / (label + '-capture.log')).write_bytes(log)
    report = json.loads((out / 'capture.json').read_text())
    assert code == 0 and report['capture_complete'], report
    summary = json.loads((out / 'analysis.json').read_text())
    assert summary['metadata']['build_id'] == receipt['build_id']
    subprocess.run([sys.executable, str(REPO / 'tests/std-channel-perf/analyze_alternation.py'),
                    str(out/'console.log'), '--output', str(out/'alternation.json')], check=True, timeout=20)
    event('case_complete', case=label, outcome=summary['outcome'],
          physical_timing_qualified=summary['physical_timing_qualified'])
    return dict(case=label, outcome=summary['outcome'],
                receiver_blocked_verified=summary.get('receiver_blocked_verified'),
                physical_timing_qualified=summary['physical_timing_qualified'],
                send_to_receive_return=summary.get('send_to_receive_return'))


def main():
    os.umask(0o077)
    original = ROOT / 'fresh-original-flash.bin'
    assert original.stat().st_size == 0x1000000
    backup_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    save('fresh-original-flash.json', dict(file=original.name, bytes=original.stat().st_size,
         sha256=backup_hash, captured_before_test=True, previous_backups_unused=True))
    tool(['verify_flash', '0', str(original)], ROOT / 'backup-verify.log')
    (ROOT / 'runs').mkdir()
    results = []
    failure = None
    try:
        build = ROOT / 'ci-firmware/esp32s3/latency-rom-digest'
        receipt = analyze.build_receipt(build, 'latency')
        for name, expected in receipt['artifacts_sha256'].items():
            assert hashlib.sha256((build/name).read_bytes()).hexdigest() == expected, name
        flash(build / receipt['image_file'], 'latency-rom-digest')
        cases = [
            dict(label='01-tight'), dict(label='02-seed-1-a', seed=1),
            dict(label='03-seed-17', seed=17),
            dict(label='04-fixed-100-w100-a', fixed=100),
            dict(label='05-fixed-100-w101-a', fixed=100, warmup=101),
            dict(label='06-fixed-1000-w100-a', fixed=1000),
            dict(label='07-fixed-1000-w101-a', fixed=1000, warmup=101),
            dict(label='08-seed-113', seed=113), dict(label='09-seed-1-b', seed=1),
            dict(label='10-fixed-1000-w101-b', fixed=1000, warmup=101),
            dict(label='11-fixed-1000-w100-b', fixed=1000),
            dict(label='12-fixed-100-w101-b', fixed=100, warmup=101),
            dict(label='13-fixed-100-w100-b', fixed=100),
        ]
        save('campaign.json', dict(samples=1000, cases=cases, same_executable=True,
             counterbalanced_fixed_pairs=True, fresh_boot_per_command=True))
        for case in cases:
            results.append(run_case(**case))
    except Exception as error:
        failure = type(error).__name__ + ': ' + str(error)
        event('measurement_failed', reason=failure)
    finally:
        event('restoring_fresh_original_firmware')
        flash(original, 'fresh-original-restore')
        save('restoration.json', dict(restored=True, original_sha256=backup_hash,
             verification='fresh-original-restore-verify.log', unix=time.time()))
        event('original_firmware_restored')
    save('summary.json', dict(pr=259, head='2b84a8b37193fb5839ec9a9a7fa6baad4a28eb53',
         ci_source_revision='32dca9f1ff869d27a556c65cbc03f4f58676ac9f',
         source_inventory_matches=True, results=results, failure=failure,
         firmware_restored=True, physical_timing_qualified=False))
    return bool(failure)

if __name__ == '__main__':
    sys.exit(main())
