#!/usr/bin/env python3
"""Run the unchanged std probe inside NuttX; fresh kernel per invocation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.nuttx import verify_identity

EXPECTED = dict(main_entries=1, rounds=4, joined_workers=8, messages=2048,
                checksum=394752, backpressure_checks=8,
                tls_isolation=True, timeout=True, disconnect=True, tls_drops=8, cpu_peer_steps=64)


def prompt_ready(data):
    # NSH readline may append erase-to-end-of-line in the same serial chunk.
    return data.endswith((b'nsh> ', b'nsh> \x1b[K'))


def verify(text, status, mode):
    lines = text.replace('\r', '').splitlines()
    assert lines.count('NXRS_MAIN_ENTERED') == 1, 'main entry count'
    assert not any(x in text for x in ('PANIC', 'panic:', 'panicked at', 'Assertion failed')), 'kernel/Rust failure'
    reports = [json.loads(x.removeprefix('NXRS_THREAD_REPORT '))
               for x in lines if x.startswith('NXRS_THREAD_REPORT ')]
    if mode == 'pass':
        assert reports == [EXPECTED], 'missing/duplicate/wrong result'
        assert lines.count('NXRS_CPU_BEGIN') == lines.count('NXRS_CPU_END') == 1
        assert 'NXRS_INJECTED_FAILURE' not in text
        expected_status = '0'
    else:
        assert not reports and lines.count('NXRS_INJECTED_FAILURE') == 1
        expected_status = '1'  # This NSH preserves only success/failure, not exit 7.
    values = re.findall(r'^NXRS_STATUS_([0-9]+)\s*$', status.replace('\r', ''), re.M)
    assert values == [expected_status], f'wrong NSH result: {values}'


def qemu_command(image, debug_port, esp32_image=None, arm_mps2=False):
    if esp32_image is not None:
        # This pinned machine initializes two hardware cores; -smp 1 leaves its
        # second ROM address space uninitialized. NuttX itself has SMP disabled.
        # Snapshot protects the hashed flash input; each test starts a new QEMU.
        return ['qemu-system-xtensa', '-machine', 'esp32s3', '-smp', '2',
                '-display', 'none', '-serial', 'stdio', '-monitor', 'none',
                '-nic', 'none', '-no-reboot', '-snapshot',
                '-drive', f'file={esp32_image.resolve()},if=mtd,format=raw',
                '-gdb', f'tcp:127.0.0.1:{debug_port}']
    if arm_mps2:
        return ['qemu-system-arm', '-M', 'mps2-an521',
                '-display', 'none', '-serial', 'stdio', '-monitor', 'none',
                '-nic', 'none', '-no-reboot', '-kernel', str(image.resolve()),
                '-gdb', f'tcp:127.0.0.1:{debug_port}']
    return ['qemu-system-riscv32', '-semihosting', '-M', 'virt,aclint=on',
            '-cpu', 'rv32', '-smp', '1', '-m', '128M', '-bios', 'none',
            '-kernel', str(image.resolve()), '-nographic',
            '-gdb', f'tcp:127.0.0.1:{debug_port}']


def self_test():
    text = '\n'.join(['NXRS_MAIN_ENTERED', 'NXRS_CPU_BEGIN', 'NXRS_CPU_END',
                      'NXRS_THREAD_REPORT ' + json.dumps(EXPECTED)])
    verify(text, 'NXRS_STATUS_0\n', 'pass')
    verify('NXRS_MAIN_ENTERED\nNXRS_INJECTED_FAILURE', 'NXRS_STATUS_1\n', 'fail')
    invalid = [(text.replace('NXRS_MAIN_ENTERED', ''), 'NXRS_STATUS_0', 'pass'),
               (text + '\nNXRS_MAIN_ENTERED', 'NXRS_STATUS_0', 'pass'),
               (text.replace('394752', '0'), 'NXRS_STATUS_0', 'pass'),
               (text + '\nNXRS_THREAD_REPORT ' + json.dumps(EXPECTED), 'NXRS_STATUS_0', 'pass'),
               (text, 'NXRS_STATUS_1', 'pass'),
               (text + '\nPANIC', 'NXRS_STATUS_0', 'pass'),
               (text, 'NXRS_STATUS_1', 'fail'),
               ('NXRS_MAIN_ENTERED\nNXRS_INJECTED_FAILURE', 'NXRS_STATUS_0', 'fail')]
    for field, value in [('tls_drops', 7), ('cpu_peer_steps', 63)]:
        bad_report = dict(EXPECTED, **{field: value})
        invalid.append((text.replace(json.dumps(EXPECTED), json.dumps(bad_report)),
                        'NXRS_STATUS_0', 'pass'))
    for args in invalid:
        try:
            verify(*args)
        except (AssertionError, ValueError):
            continue
        raise AssertionError('oracle accepted invalid transcript')
    print(f'PASS: {len(invalid)} NuttX transcript negative controls')
    prompt_cases = [(b'NuttShell\r\nnsh> ', True),
                    (b'uname output\r\nnsh> \x1b[K', True),
                    (b'nsh>', False), (b'nsh> \x1b[', False),
                    (b'nsh> \x1b[Knot a prompt', False), (b'output only', False)]
    for data, expected in prompt_cases:
        assert prompt_ready(data) == expected, repr(data)
    print(f'PASS: {len(prompt_cases)} serial prompt controls')
    rv = qemu_command(Path('nuttx'), 1234)
    esp = qemu_command(Path('nuttx'), 1234, Path('nuttx.merged.bin'))
    arm = qemu_command(Path('nuttx'), 1234, arm_mps2=True)
    assert rv[0] == 'qemu-system-riscv32' and '-kernel' in rv
    assert esp[0] == 'qemu-system-xtensa' and '-kernel' not in esp and '-snapshot' in esp
    assert arm[0] == 'qemu-system-arm' and arm[arm.index('-M') + 1] == 'mps2-an521'
    assert arm[arm.index('-kernel') + 1].endswith('nuttx')
    assert esp[esp.index('-smp') + 1] == '2'
    assert rv[rv.index('-smp') + 1] == '1'
    assert 'if=mtd,format=raw' in esp[esp.index('-drive') + 1]
    print('PASS: explicit emulator CPU counts and immutable-flash command controls')


def run(image, output, esp32_image=None, arm_mps2=False):
    cases = [(mode, 'rust_std ' + mode) for mode in ['pass', 'pass', 'pass', 'fail']]
    run_cases(image, output, cases, verify, esp32_image, arm_mps2)


def run_cases(image, output, cases, verifier, esp32_image=None, arm_mps2=False, interaction=None):
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for index, (mode, invocation) in enumerate(cases):
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            debug_port = reserved.getsockname()[1]
        command = qemu_command(image, debug_port, esp32_image, arm_mps2)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        transcript = bytearray()
        def read_console(predicate, start, timeout=60):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f'QEMU exited unexpectedly: {process.returncode}')
                if predicate(transcript[start:]):
                    return transcript[start:].decode(errors='replace')
                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if ready:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise AssertionError('QEMU console EOF')
                    transcript.extend(chunk)
                    if len(transcript) > 256 * 1024:
                        raise ValueError('NuttX console budget exceeded')
            raise TimeoutError('NuttX console deadline exceeded')
        def read_prompt(timeout=60):
            return read_console(prompt_ready, len(transcript), timeout)
        def write_bytes(data):
            if interaction is not None:
                if not isinstance(data, bytes) or len(data) > 128:
                    raise ValueError('interactive UART frame budget exceeded')
                # Qualification UART only: a bounded line can still overrun
                # the guest while its concurrent reader is unscheduled. Pace
                # delivery as well as bounding each frame; product deadlines
                # and target capture/TLS behavior are unchanged.
                for byte in data:
                    process.stdin.write(bytes((byte,))); process.stdin.flush(); time.sleep(0.001)
            elif arm_mps2:
                time.sleep(0.02)
                for byte in data:
                    process.stdin.write(bytes((byte,))); process.stdin.flush(); time.sleep(0.01)
            else:
                process.stdin.write(data); process.stdin.flush()
        def send(command):
            start = len(transcript); write_bytes((command + '\n').encode())
            return read_console(prompt_ready, start)
        result = dict(mode=mode, success=False, command=command,
                      image_sha256=hashlib.sha256((esp32_image or image).read_bytes()).hexdigest())
        try:
            read_prompt()
            verify_identity(send)
            result['uname'] = send('uname -a')
            if interaction is None:
                text = send(invocation)
            else:
                start = len(transcript)
                def read_until(marker):
                    return read_console(lambda data: marker in data.replace(b'\r', b''), start, 60)
                interaction(invocation, write_bytes, read_until)
                text = read_console(prompt_ready, start)
            status = send('echo NXRS_STATUS_$?')
            verifier(text, status, mode)
            result['success'] = True
            print(f'PASS: NuttX fresh boot {index + 1}: {mode}', flush=True)
        except Exception as error:
            result['error'] = str(error)
            # Preserve the failed state before terminating the kernel. Diagnostic
            # failure cannot turn the original failed execution into a pass.
            try:
                if esp32_image is None:
                    script = (Path(__file__).parents[1] / 'host' / 'debug-nuttx-tasks.gdb'
                              if arm_mps2 else Path(__file__).with_name('debug.gdb'))
                    debugger = ['gdb-multiarch', '--batch', '-q', str(image.resolve()),
                                '-ex', f'target remote 127.0.0.1:{debug_port}',
                                '-x', str(script)]
                else:
                    debugger = ['xtensa-esp32s3-elf-gdb', '--batch', '-q', str(image.resolve()),
                                '-ex', f'target remote 127.0.0.1:{debug_port}',
                                '-ex', 'info registers', '-ex', 'bt']
                debug = subprocess.run(debugger, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, timeout=15)
                (output / f'debug-{index}-{mode}.log').write_bytes(debug.stdout)
            except (OSError, subprocess.SubprocessError) as diagnostic_error:
                result['diagnostic_error'] = str(diagnostic_error)
            raise
        finally:
            results.append(result)
            (output / f'console-{index}-{mode}.log').write_bytes(transcript)
            (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--image', type=Path)
    parser.add_argument('--esp32-image', type=Path)
    parser.add_argument('--arm-mps2', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('target/nuttx-std'))
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        if not args.image or not args.image.is_file():
            parser.error('--image must name the built NuttX kernel')
        if args.esp32_image is not None and not args.esp32_image.is_file():
            parser.error('--esp32-image must name the merged flash image')
        if args.esp32_image is not None and args.arm_mps2:
            parser.error('--esp32-image and --arm-mps2 are mutually exclusive')
        run(args.image, args.output, args.esp32_image, args.arm_mps2)
