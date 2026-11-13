#!/usr/bin/env python3
"""Build the guide's source project and execute each revision on NuttX/QEMU."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import pty
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from check_examples import ROOT, command, create_project, environment, phases, run

sys.path.insert(0, str(ROOT / 'tests'))
from rtos_harness.nuttx import verify_identity

IMAGE = ROOT / 'target/build/hello/esp32s3-qemu-mock/nuttx/nuttx.merged.bin'
REPORT = ROOT / 'target/first-firmware'


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_response(text: str, sent: str, expected: str) -> None:
    """Only complete output lines count; command echo is not application output."""
    lines = [line.strip() for line in text.replace('\r', '').splitlines()]
    lines = [line for line in lines if line]
    if not lines or lines[-1] != 'nsh>':
        raise ValueError('missing shell prompt after command')
    lines.pop()
    # NSH readline prefixes its command echo with VT100 clear-to-end-of-line.
    # Accept only that exact echo; keep application output and raw logs intact.
    if lines and lines[0] in (sent, '\x1b[K' + sent):
        lines.pop(0)
    if lines != [expected]:
        raise ValueError(f'{sent!r}: expected one {expected!r} output line, got {lines!r}')


def stop_process(process: subprocess.Popen) -> None:
    """Reap the owned process and stop its process group on every exit path."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


class Console:
    def __init__(self, args: list[str], log: Path, *, limit: int = 128 * 1024):
        self.log = log
        self.limit = limit
        self.raw = bytearray()
        self.pending = ''
        self.closed = False
        self.fd, slave = pty.openpty()
        try:
            self.process = subprocess.Popen(args, stdin=slave, stdout=slave,
                                            stderr=slave, start_new_session=True)
        except BaseException:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)

    def until(self, marker: str, seconds: float) -> str:
        deadline = time.monotonic() + seconds
        while marker not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'console deadline waiting for {marker!r}')
            if select.select([self.fd], [], [], min(remaining, 0.1))[0]:
                try:
                    data = os.read(self.fd, 8192)
                except OSError as error:
                    raise RuntimeError('console closed before the expected response') from error
                if not data:
                    raise RuntimeError('console EOF')
                if len(self.raw) + len(data) > self.limit:
                    self.raw.extend(data[:max(0, self.limit - len(self.raw))])
                    raise RuntimeError('console transcript limit exceeded')
                self.raw.extend(data)
                self.pending += data.decode('utf-8', errors='replace').replace('\r', '')
            elif self.process.poll() is not None:
                raise RuntimeError(f'emulator exited early: {self.process.returncode}')
        end = self.pending.index(marker) + len(marker)
        answer, self.pending = self.pending[:end], self.pending[end:]
        return answer

    def shell(self, value: str) -> str:
        if '\n' in value or '\r' in value:
            raise ValueError('shell requests must be single commands')
        os.write(self.fd, (value + '\n').encode())
        return self.until('nsh>', 20)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            stop_process(self.process)
        finally:
            os.close(self.fd)
            self.log.parent.mkdir(parents=True, exist_ok=True)
            self.log.write_bytes(self.raw)


def boot(qemu: Path, image: Path, expected: str, log: Path) -> dict:
    before = digest(image)
    # Same machine/flash/no-network/snapshot options as the interactive guide.
    # A separate stdio console replaces -nographic's interactive monitor mux.
    args = [str(qemu.resolve()), '-display', 'none', '-serial', 'stdio',
            '-monitor', 'none', '-machine', 'esp32s3', '-nic', 'none',
            '-no-reboot', '-snapshot',
            '-drive', f'file={image.resolve()},if=mtd,format=raw']
    console = Console(args, log)
    try:
        console.until('nsh>', 60)
        verify_identity(console.shell)
        for _ in range(2):
            validate_response(console.shell('hello'), 'hello', expected)
            validate_response(console.shell('echo $?'), 'echo $?', '0')
        if re.search(rb'panic|assertion failed|guru meditation', console.raw, re.I):
            raise RuntimeError('guest reported a fatal error')
        if console.process.poll() is not None:
            raise RuntimeError('emulator exited before test cleanup')
    finally:
        console.close()
    if digest(image) != before:
        raise RuntimeError('QEMU changed the build image despite snapshot mode')
    return {'command': args, 'image_sha256': before, 'invocations': 2,
            'expected_output': expected, 'exit_status': 0, 'uname': 'NuttX',
            'console_log': str(log.relative_to(ROOT)), 'result': 'passed'}


def build(args: list[str], env: dict[str, str], log: Path, *,
          expected_image: Path | None = None) -> None:
    print('$ ' + shlex.join(args), flush=True)
    with log.open('w') as stream:
        process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            status = process.wait(timeout=1200)
        finally:
            if process.poll() is None:
                stop_process(process)
    if status != 0:
        print(log.read_text(errors='replace')[-16000:], flush=True)
        raise subprocess.CalledProcessError(status, args)
    image = IMAGE if expected_image is None else expected_image
    if not image.is_file():
        raise FileNotFoundError(f'build did not produce {image}')


def main() -> None:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('the first-firmware target test requires Linux x86_64')
    env = environment()
    qemu_name = run(['bash', '-c', 'source "$1"; command -v qemu-system-xtensa',
                     'tutorial-qemu', str(ROOT / 'target/qemu-tools/environment.sh')], ROOT, env).strip()
    qemu = Path(qemu_name)
    if not qemu.is_file():
        raise FileNotFoundError('install the pinned QEMU tools first')
    REPORT.mkdir(parents=True, exist_ok=True)
    result_path = REPORT / 'report.json'
    result_path.unlink(missing_ok=True)
    report = {'result': 'failed', 'toolchain': env['RUSTUP_TOOLCHAIN'], 'phases': []}
    previous_image = None
    try:
        with tempfile.TemporaryDirectory(prefix='nxrs-first-firmware-') as directory:
            project = Path(directory) / 'my-firmware'
            create_project(project, env)
            report['application_manifest'] = str(project / 'Cargo.toml')
            report['manifest_sha256'] = digest(project / 'Cargo.toml')
            for name, source, expected in phases():
                (project / 'src/main.rs').write_text(source)
                (REPORT / f'{name}.rs').write_text(source)
                output = run(command('first-application.md', 'host', project), ROOT, env)
                if output.strip() != expected:
                    raise RuntimeError(f'{name}: unexpected host output')
                args = command('firmware.md', 'build', project)
                # Keep normal incremental state, as in the documented workflow.
                # Successful output and the changed image must match this source.
                build(args, env, REPORT / f'{name}-build.log')
                image_hash = digest(IMAGE)
                if image_hash == previous_image:
                    raise RuntimeError(f'{name}: changed source produced the previous image')
                row = boot(qemu, IMAGE, expected, REPORT / f'{name}-console.log')
                row.update(phase=name, source_sha256=digest(project / 'src/main.rs'),
                           build_command=args, host_output=output.strip())
                report['phases'].append(row)
                previous_image = image_hash
                print(f'TUTORIAL_PHASE PASS phase={name} image_sha256={image_hash}', flush=True)
        report['result'] = 'passed'
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        result_path.write_text(json.dumps(report, indent=2) + '\n')
    print('FIRST_FIRMWARE PASS phases=3 target_invocations=6', flush=True)


if __name__ == '__main__':
    main()
