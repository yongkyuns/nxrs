#!/usr/bin/env python3
"""One NSH/byte oracle for native NuttX sim and ESP32-S3 system emulation."""
import argparse
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from nuttx_target_checks import validate_target, self_test as target_self_test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.nuttx import verify_identity

PASS = 'RC_NUTTX_SIM PASS records=3 packets=4 opens=2 closes=2'


def checksum(data):
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


def validate(text, require_std_main=False):
    lines = text.replace('\r', '').splitlines()
    assert not any('FAIL' in line or 'panic' in line.lower() for line in lines), text
    assert lines.count('RC_NUTTX_SIM BEGIN') == 1, text
    assert lines.count(PASS) == 1, text
    if require_std_main:
        assert lines.count('RC_RUST_STD_MAIN BEGIN') == 1, text
        assert lines.count('RC_RUST_STD_MAIN PASS') == 1, text
    phases = [line for line in lines if line.startswith('RC_CHECK')]
    assert phases == [f'RC_CHECK phase={i} OK' for i in range(1, 5)], phases
    records = [bytes.fromhex(line.split()[1]) for line in lines if line.startswith('RC_RECORD ')]
    packets = [bytes.fromhex(line.split()[1]) for line in lines if line.startswith('RC_PACKET ')]
    assert len(records) == 3 and len(packets) == 4, (len(records), len(packets))
    observed = {}
    for sequence, packet in enumerate(packets, 1):
        assert len(packet) == 28, packet
        version, pixels, width, height, reserved, seq, stamp, digest = struct.unpack('<BBHHHQQI', packet)
        assert (version, pixels, width, height, reserved, seq) == (1, 0, 2, 2, 0, sequence)
        expected = bytes(range(sequence, sequence + 4))
        assert digest == checksum(expected), (sequence, digest)
        assert stamp > 0
        observed[seq] = stamp
    assert list(observed.values()) == sorted(observed.values()), observed
    for sequence, record in zip([1, 3, 4], records):
        assert len(record) == 44
        magic, width, height, pixels, seq, stamp, length = struct.unpack('<8sHHB3xQQQ', record[:40])
        assert (magic, width, height, pixels, seq, length) == (b'RCAMREC1', 2, 2, 0, sequence, 4)
        assert record[13:16] == b'\0' * 3
        assert stamp == observed[seq]
        assert record[40:] == bytes(range(sequence, sequence + 4))


def self_test():
    lines = ['RC_NUTTX_SIM BEGIN'] + [f'RC_CHECK phase={i} OK' for i in range(1, 5)]
    for seq in [1, 3, 4]:
        record = struct.pack('<8sHHB3xQQQ', b'RCAMREC1', 2, 2, 0, seq, seq * 10, 4) + bytes(range(seq, seq + 4))
        lines.append('RC_RECORD ' + record.hex())
    for seq in range(1, 5):
        packet = struct.pack('<BBHHHQQI', 1, 0, 2, 2, 0, seq, seq * 10, checksum(bytes(range(seq, seq + 4))))
        lines.append('RC_PACKET ' + packet.hex())
    lines.append(PASS)
    valid = '\n'.join(lines)
    validate(valid)
    for bad in [valid.replace(PASS, ''), valid + '\n' + PASS,
                valid.replace('RC_CHECK phase=2 OK', ''), valid + '\nRC_NUTTX_SIM FAIL',
                valid.replace(lines[5], lines[6]), valid.replace(lines[8], lines[9])]:
        try:
            validate(bad)
        except (AssertionError, ValueError):
            continue
        raise AssertionError('oracle accepted a corrupted/incomplete transcript')
    print('PASS: target transcript oracle rejects incomplete, duplicate and corrupted results')


def qmp_quit(path):
    """Terminate QEMU via an acknowledged monitor command, not SIGKILL success."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        with connection.makefile('rwb', buffering=0) as stream:
            assert 'QMP' in json.loads(stream.readline(65536)), 'missing QMP greeting'
            for index, command in enumerate(['qmp_capabilities', 'quit']):
                stream.write((json.dumps({'execute': command, 'id': index}) + '\n').encode())
                while True:
                    line = stream.readline(65536)
                    if not line:
                        raise RuntimeError('QMP closed before acknowledging shutdown')
                    reply = json.loads(line)
                    if reply.get('id') == index:
                        assert 'return' in reply, reply
                        break


def run(binary, log_path, image=None, cycles=2, require_std_main=False):
    with tempfile.TemporaryDirectory(prefix='nxrs-qmp-') as directory:
        monitor = Path(directory) / 'control.sock'
        command = [str(binary.resolve())]
        if image is not None:
            command += ['-machine', 'esp32s3', '-display', 'none', '-serial', 'stdio',
                        '-monitor', 'none', '-nic', 'none', '-no-reboot', '-snapshot',
                        '-qmp', f'unix:{monitor},server=on,wait=off',
                        '-drive', f'file={image.resolve()},if=mtd,format=raw']
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                       cwd=image.parent if image else binary.parent,
                                       start_new_session=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        pending = ''
        transcript = []
        total = 0

        def until(marker, seconds=30):
            nonlocal pending, total
            deadline = time.monotonic() + seconds
            while marker not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'NuttX did not produce {marker!r}; exit={process.poll()}')
                if select.select([master], [], [], min(remaining, 0.2))[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        raise RuntimeError(f'NuttX console closed; exit={process.poll()}') from error
                    if not data:
                        raise RuntimeError('NuttX console EOF')
                    total += len(data)
                    if total > 2 * 1024 * 1024:
                        raise RuntimeError('console exceeded bounded transcript size')
                    text = data.decode('utf-8', errors='replace').replace('\r', '')
                    transcript.append(text)
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    pending += text
                elif process.poll() is not None:
                    raise RuntimeError(f'NuttX exited early: {process.returncode}')
            end = pending.index(marker) + len(marker)
            result, pending = pending[:end], pending[end:]
            return result

        try:
            until('nsh>', seconds=60)
            def shell_command(value):
                os.write(master, (value + '\n').encode())
                return until('nsh>')
            verify_identity(shell_command)
            for cycle in range(cycles):
                os.write(master, b'nxrs_sim\n')
                output = until('nsh>')
                validate(output, require_std_main=require_std_main)
                validate_target(output, 32 if image else 64)
                print(f'\nPASS: NuttX invocation {cycle + 1}, target records, UDP and ABI verified')
            os.write(master, b'nxrs_sim fail\n')
            failure = until('nsh>')
            assert 'RC_NUTTX_SIM FAIL injected' in failure and PASS not in failure, failure
            try:
                validate(failure)
            except AssertionError:
                pass
            else:
                raise AssertionError('oracle accepted the target failure injection')
            if image is None:
                os.write(master, b'poweroff\n')
            else:
                # ESP32-S3 has no host-process poweroff operation. The target
                # must return to NSH after every test; then explicitly quit QEMU.
                qmp_quit(monitor)
            assert process.wait(timeout=10) == 0, process.returncode
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            os.close(master)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(''.join(transcript), encoding='utf-8')
    print(f'PASS: {cycles} NuttX execution(s), negative-control rejection, and clean emulator exit')


def main():
    if not __debug__:
        raise RuntimeError('This assertion-based oracle must not run with Python -O')
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', nargs='?', type=Path)
    parser.add_argument('--qemu-image', type=Path)
    parser.add_argument('--log', type=Path, default=Path('target/nuttx-sim/console.log'))
    parser.add_argument('--cycles', type=int, default=2)
    parser.add_argument('--require-std-main', action='store_true')
    arguments = parser.parse_args()
    if arguments.qemu_image and not arguments.binary:
        parser.error('--qemu-image requires the qemu-system-xtensa executable')
    self_test()
    target_self_test()
    if arguments.binary:
        if arguments.cycles < 1:
            parser.error('--cycles must be at least 1')
        run(arguments.binary, arguments.log, arguments.qemu_image,
            cycles=arguments.cycles, require_std_main=arguments.require_std_main)


if __name__ == '__main__':
    main()
