#!/usr/bin/env python3
"""Capture exactly one std-channel command after a fresh boot, with an external deadline."""
import argparse
import errno
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import time

from analyze import FAILURE, MODES, analyze, build_receipt, checked_file, qualify, read_json, require

PROMPT = b'nsh> '
BOOT_BANNERS = (b'NuttShell', b'*** Booting NuttX ***')
MAX_LOG_BYTES = 16 * 1024 * 1024
ROM_DIGEST_WARNING = b'SHA-256 comparison failed:'


def fresh_boot(data):
    return PROMPT in data and any(banner in data for banner in BOOT_BANNERS)


def command_for(mode, samples, warmup, phase_seed=None):
    require(mode in MODES, 'unknown mode')
    require(type(samples) is int and 1 <= samples <= 4096, 'invalid sample count')
    require(type(warmup) is int and 0 <= warmup <= 1024, 'invalid warmup count')
    if phase_seed is not None:
        require(mode == 'latency', 'phase pacing is only a latency case')
        require(type(phase_seed) is int and 1 <= phase_seed <= 0xffffffff, 'invalid phase seed')
        require(samples <= 1024 and warmup <= 128, 'phase count exceeds watchdog budget')
    if mode == 'latency':
        suffix = '' if phase_seed is None else f' {phase_seed}'
        return f'std_channel_perf latency {samples} {warmup}{suffix}'
    return f'std_channel_perf {mode}'


def read_until(fd, transcript, predicate, deadline, process=None):
    pending = bytearray()
    while not predicate(pending):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('external console deadline: incomplete fixture; no progress claim')
        ready, _, _ = select.select([fd], [], [], min(.1, remaining))
        if ready:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError as error:
                if error.errno == errno.EIO:
                    raise RuntimeError('console closed before complete result and prompt') from error
                raise
            if not data:
                raise RuntimeError('console EOF before complete result and prompt')
            transcript.extend(data)
            pending.extend(data)
            require(len(transcript) <= MAX_LOG_BYTES, 'console size limit')
            if FAILURE.search(pending.decode(errors='replace')):
                raise RuntimeError('guest failure or kernel fault')
        elif process is not None and process.poll() is not None:
            raise RuntimeError('emulator exited before complete result and prompt')
    return bytes(pending)


def write_command(fd, command, deadline):
    # Outside measurements. NSH has small input buffers on MCU targets.
    for byte in (command + '\n').encode():
        if time.monotonic() >= deadline:
            raise TimeoutError('console transmit deadline')
        _, writable, _ = select.select([], [fd], [], min(.1, max(0, deadline - time.monotonic())))
        if not writable:
            raise TimeoutError('console not writable')
        require(os.write(fd, bytes([byte])) == 1, 'short console write')
        time.sleep(.005)


def serial_open(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    saved = None
    try:
        saved = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
        return fd, saved
    except BaseException:
        os.close(fd)
        raise


def capture(args):
    receipt = build_receipt(args.build_directory, args.mode)
    command = command_for(args.mode, args.samples, args.warmup, args.phase_seed)
    require(not args.output.exists(), 'output directory must be new; preserve previous runs')
    args.output.mkdir(parents=True)
    transcript = bytearray()
    fd = slave = process = serial_saved = None
    report = {
        'schema': 'std-channel-capture-v1', 'capture_complete': False,
        'runtime_kind': args.runtime, 'mode': args.mode, 'command': command,
        'image_sha256': receipt['image_sha256'], 'one_command_per_boot': True,
        'physical_timing_qualified': False, 'failure_stage': 'setup', 'error': None,
    }
    try:
        if args.runtime == 'qemu':
            require(receipt['platform'] == 'mps2-an521-mock', 'QEMU requires MPS2 image')
            image = checked_file(args.build_directory, receipt.get('elf_file'))
            argv = [args.qemu, '-machine', 'mps2-an521', '-kernel', str(image),
                    '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-nic', 'none', '-no-reboot']
            report['emulator_command'] = argv
            fd, slave = pty.openpty()
            process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            slave = None
            session = {'schema': 'std-channel-session-v1', 'runtime_kind': 'qemu',
                       'image_sha256': receipt['image_sha256'], 'board': 'mps2-an521'}
        else:
            session = read_json(args.session)
            require(session.get('runtime_kind') == 'physical', 'serial capture needs physical session evidence')
            require(session.get('image_sha256') == receipt['image_sha256'], 'wrong flashed image receipt')
            fd, serial_saved = serial_open(args.port)
            print('Serial capture ready. Reset the test board now; waiting for a new NuttShell banner.', flush=True)
        deadline = time.monotonic() + args.timeout
        report['failure_stage'] = 'fresh-boot'
        boot = read_until(fd, transcript, fresh_boot, deadline, process)
        require(fresh_boot(boot), 'fresh boot banner missing')
        report['fresh_boot_observed'] = True
        report['boot_rom_digest_warning_observed'] = ROM_DIGEST_WARNING in boot
        report['failure_stage'] = 'command'
        write_command(fd, command, deadline)
        read_until(fd, transcript, lambda data: PROMPT in data, deadline, process)
        report['failure_stage'] = 'analysis'
        summary = analyze(transcript.decode(errors='strict'))
        require(summary['mode'] == args.mode, 'unexpected command mode')
        if args.mode == 'latency':
            require(summary['metadata']['samples'] == args.samples and
                    summary['metadata']['warmup'] == args.warmup, 'wrong command sample count')
            sampling = summary['sampling']
            if args.phase_seed is not None:
                require(sampling.get('profile') == 'setup-counter-delay-v1' and
                        sampling.get('seed') == args.phase_seed, 'wrong captured phase schedule')
            else:
                require(sampling.get('profile') in {'historical-unrecorded', 'tight-v1'},
                        'unexpected paced workload')
        summary = qualify(
            summary, receipt, session, fresh_boot_observed=True,
            calibration_directory=(args.session.parent if args.runtime == 'physical' else None),
            boot_digest_warning=report['boot_rom_digest_warning_observed'])
        (args.output / 'analysis.json').write_text(json.dumps(summary, indent=2) + '\n')
        report.update(capture_complete=True, failure_stage=None, outcome=summary['outcome'],
                      physical_timing_qualified=summary['physical_timing_qualified'],
                      clock_calibration_qualified=summary['clock_calibration_qualified'],
                      clock_calibration_status=summary['clock_calibration']['status'],
                      timing_qualification_blockers=summary.get('timing_qualification_blockers', []))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        if process is not None:
            report['emulator_returncode_before_cleanup'] = process.poll()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if serial_saved is not None and fd is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, serial_saved)
            except OSError as error:
                report['serial_restore_error'] = str(error)
        for descriptor in [fd, slave]:
            if descriptor is not None:
                os.close(descriptor)
        (args.output / 'console.log').write_bytes(transcript)
        (args.output / 'capture.json').write_text(json.dumps(report, indent=2) + '\n')
        (args.output / 'build.json').write_text(json.dumps(receipt, indent=2) + '\n')
        if args.runtime == 'physical' and args.session and args.session.is_file():
            # Retain exactly the supplied session even if analysis rejected it.
            # Calibration supporting evidence remains separately hash-addressed.
            (args.output / 'session.json').write_bytes(args.session.read_bytes())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', choices=['qemu', 'physical'])
    parser.add_argument('--build-directory', type=Path, required=True)
    parser.add_argument('--mode', choices=list(MODES), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--warmup', type=int, default=100)
    parser.add_argument('--phase-seed', type=int, help='enable bounded seeded setup delay')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--qemu', default='qemu-system-arm')
    parser.add_argument('--port')
    parser.add_argument('--session', type=Path)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error('--timeout must be 1..600 seconds')
    if args.runtime == 'physical' and (not args.port or not args.session):
        parser.error('physical requires --port and --session')
    if args.runtime == 'qemu' and (args.port or args.session):
        parser.error('QEMU does not accept physical session metadata')
    report = capture(args)
    print(json.dumps(report, indent=2))
    return 0 if report['capture_complete'] else 1


if __name__ == '__main__':
    sys.exit(main())
