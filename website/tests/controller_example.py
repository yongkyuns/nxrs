#!/usr/bin/env python3
"""Execute the controller guide's source, commands, and NuttX/QEMU lifecycle."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shlex
import subprocess
import textwrap
import tomllib

from check_examples import ROOT, block, environment

PAGE = 'controller.md'
PROJECT = ROOT / 'website/examples/controller'
PROFILE = ROOT / 'tests/profiles/esp32s3-qemu-led-mock.toml'
IMAGE = ROOT / 'target/build/controller/esp32s3-qemu-led-mock/nuttx/nuttx.merged.bin'
REPORT = ROOT / 'target/controller-tutorial'
MANIFEST = 'website/examples/controller/Cargo.toml'
HOST = ['cargo', 'run', '--locked', '--manifest-path', MANIFEST, '--bin', 'controller']
TEST = ['cargo', 'test', '--locked', '--manifest-path', MANIFEST]
BUILD = ['cargo', 'nxrs', 'build', '--manifest-path', MANIFEST,
         '--profile', 'esp32s3-qemu-led-mock']
DEFAULT_OUTPUT = ['ready: off', 'on: on', 'status: on', 'off: off', 'stopped: off']
STOP_OUTPUT = ['ready: off', 'on: on', 'status: on', 'stopped: off']
INVALID_OUTPUT = ['ready: off', 'on: on', 'stopped: off']
INVALID_ERROR = 'controller: unknown command "invalid" (use on, off, status, stop)'
UNBOUND_ERROR = 'controller: cannot acquire LED output: Unsupported'


def source_region(source: str, tag: str) -> str:
    """Extract one deliberately published region; never silently pick a duplicate."""
    start = rf'^[ \t]*// tutorial:{re.escape(tag)}:start[ \t]*$'
    end = rf'^[ \t]*// tutorial:{re.escape(tag)}:end[ \t]*$'
    if len(re.findall(start, source, re.M)) != 1 or len(re.findall(end, source, re.M)) != 1:
        raise ValueError(f'{tag}: expected exactly one source-region marker pair')
    found = re.findall(start + r'\n(.*?)\n' + end, source, re.M | re.S)
    if len(found) != 1:
        raise ValueError(f'{tag}: invalid source-region order')
    return textwrap.dedent(found[0]).strip()


def guide_command(tag: str, expected: list[str], *, mock: bool = False) -> list[str]:
    args = shlex.split(block(PAGE, tag, 'sh'))
    prefix = ['NXRS_PLATFORM=mock'] if mock else []
    if args != prefix + expected:
        raise ValueError(f'{tag}: published command differs from the qualified command')
    return args[len(prefix):]


def check_sources() -> dict[str, dict[str, str | int]]:
    """The published source is exactly the source that Cargo will compile."""
    for tag, language, path in (
        ('controller-manifest', 'toml', PROJECT / 'Cargo.toml'),
        ('controller-main', 'rust', PROJECT / 'src/main.rs'),
    ):
        if block(PAGE, tag, language).strip() != path.read_text().strip():
            raise ValueError(f'{tag}: published source differs from {path.relative_to(ROOT)}')
    library = (PROJECT / 'src/lib.rs').read_text()
    for name in ('commands', 'admit', 'stop', 'owner'):
        tag = f'controller-{name}'
        if block(PAGE, tag, 'rust').strip() != source_region(library, tag):
            raise ValueError(f'{tag}: published source differs from the compiled library')
    if '<!-- tutorial:controller-start -->' in (ROOT / 'website/guide' / PAGE).read_text():
        if block(PAGE, 'controller-start', 'rust').strip() != source_region(library, 'controller-start'):
            raise ValueError('controller-start: published source differs from the compiled library')
    if block(PAGE, 'controller-output', 'text').splitlines() != DEFAULT_OUTPUT:
        raise ValueError('controller-output: published output differs from the exercised behavior')
    guide_command('controller-host', HOST, mock=True)
    guide_command('controller-test', TEST, mock=True)
    guide_command('controller-build', BUILD)
    manifest = tomllib.loads((PROJECT / 'Cargo.toml').read_text())
    nxrs = manifest['package']['metadata']['nxrs']
    if nxrs['firmware']['command'] != 'controller' or nxrs['firmware']['bin'] != 'controller':
        raise ValueError('the source manifest does not register the exercised shell command')
    if 'led' not in nxrs.get('required-capabilities', []):
        raise ValueError('the controller must declare its required LED capability')
    profile = tomllib.loads(PROFILE.read_text())
    if profile.get('extends') != 'esp32s3-qemu-mock' or profile.get('hal', {}).get('led') != 'mock':
        raise ValueError('the tutorial profile must explicitly select the mock LED on ESP32-S3 QEMU')
    launch = block(PAGE, 'controller-qemu', 'sh')
    expected_image = str(IMAGE.relative_to(ROOT))
    required = ('-machine esp32s3', '-nic none', '-snapshot', expected_image,
                'if=mtd,format=raw', 'source target/qemu-tools/environment.sh')
    if not launch.startswith('(\n') or not all(value in launch for value in required):
        raise ValueError('controller-qemu: missing isolated environment or qualified launch options')
    subprocess.run(['bash', '-n'], input=launch, text=True, check=True)
    paths = [PROJECT / 'Cargo.toml', PROJECT / 'Cargo.lock', PROFILE,
             ROOT / 'website/guide' / PAGE, *sorted((PROJECT / 'src').rglob('*.rs')),
             *sorted((PROJECT / 'tests').rglob('*.rs'))]
    return {str(path.relative_to(ROOT)): {
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'bytes': path.stat().st_size,
    } for path in paths}


def invoke(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    print('$ ' + shlex.join(args), flush=True)
    result = subprocess.run(args, cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', flush=True)
    return result


def check_host_response(result: subprocess.CompletedProcess[str], expected: list[str],
                        status: int = 0, error: str | None = None) -> None:
    if result.returncode != status:
        raise ValueError(f'expected host exit status {status}, got {result.returncode}')
    if result.stdout.splitlines() != expected:
        raise ValueError(f'host output does not match the completed command sequence: {result.stdout!r}')
    if error is not None and result.stderr.splitlines().count(error) != 1:
        raise ValueError('missing or repeated application error after cleanup')


def host_checks(env: dict[str, str]) -> list[dict]:
    mock_env = dict(env, NXRS_PLATFORM='mock')
    invoke(guide_command('controller-test', TEST, mock=True), mock_env).check_returncode()
    args = guide_command('controller-host', HOST, mock=True)
    cases = [
        ('default', [], DEFAULT_OUTPUT, 0, None),
        ('explicit-stop', ['on', 'status', 'stop'], STOP_OUTPUT, 0, None),
        ('invalid-command-cleanup', ['on', 'invalid'], INVALID_OUTPUT, 1, INVALID_ERROR),
        ('trailing-stop-cleanup', ['on', 'stop', 'off'], INVALID_OUTPUT, 1,
         'controller: stop must be the last command'),
        ('fresh-start-after-error', [], DEFAULT_OUTPUT, 0, None),
    ]
    results = []
    for name, commands, expected, status, error in cases:
        command = args + (['--', *commands] if commands else [])
        result = invoke(command, mock_env)
        check_host_response(result, expected, status, error)
        results.append({'case': name, 'command': command, 'stdout': result.stdout,
                        'stderr': result.stderr, 'exit_status': result.returncode, 'result': 'passed'})
    unbound_env = dict(env)
    unbound_env.pop('NXRS_PLATFORM', None)
    result = invoke(args, unbound_env)
    check_host_response(result, [], 1, UNBOUND_ERROR)
    results.append({'case': 'unbound-capability-rejection', 'command': args,
                    'profile': None, 'stdout': result.stdout, 'stderr': result.stderr,
                    'exit_status': result.returncode, 'result': 'passed'})
    return results


def validate_console_response(text: str, sent: str, expected: list[str]) -> None:
    """Require all complete lines and the returning prompt, excluding only exact echo."""
    lines = [line.strip() for line in text.replace('\r', '').splitlines() if line.strip()]
    if not lines or lines[-1] != 'nsh>':
        raise ValueError('missing shell prompt after command')
    lines.pop()
    if lines and lines[0] in (sent, '\x1b[K' + sent):
        lines.pop(0)
    if lines != expected:
        raise ValueError(f'{sent!r}: expected {expected!r}, got {lines!r}')


def boot(qemu: Path, image: Path, log: Path) -> dict:
    # Import Unix PTY support only for target execution, not host/source checks.
    from first_firmware import Console, digest, verify_identity

    before = digest(image)
    args = [str(qemu.resolve()), '-display', 'none', '-serial', 'stdio',
            '-monitor', 'none', '-machine', 'esp32s3', '-nic', 'none',
            '-no-reboot', '-snapshot',
            '-drive', f'file={image.resolve()},if=mtd,format=raw']
    console = Console(args, log)
    cases = [
        ('controller', DEFAULT_OUTPUT, 0),
        ('controller on status stop', STOP_OUTPUT, 0),
        ('controller on invalid', INVALID_OUTPUT + [INVALID_ERROR], 1),
        ('controller', DEFAULT_OUTPUT, 0),
    ]
    results = []
    try:
        console.until('nsh>', 60)
        verify_identity(console.shell)
        for command, expected, status in cases:
            validate_console_response(console.shell(command), command, expected)
            validate_console_response(console.shell('echo $?'), 'echo $?', [str(status)])
            results.append({'command': command, 'output': expected,
                            'exit_status': status, 'result': 'passed'})
        if re.search(rb'panic|assertion failed|guru meditation', console.raw, re.I):
            raise RuntimeError('guest reported a fatal error')
        if console.process.poll() is not None:
            raise RuntimeError('emulator exited before test cleanup')
    finally:
        console.close()
    if digest(image) != before:
        raise RuntimeError('QEMU changed the build image despite snapshot mode')
    return {'qemu_command': args, 'image_sha256': before, 'invocations': results,
            'uname': 'NuttX', 'console_log': str(log.relative_to(ROOT)), 'result': 'passed'}


def target_checks(env: dict[str, str]) -> dict:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('controller target qualification requires Linux x86_64')
    from first_firmware import build

    locate = invoke(['bash', '-c', 'source "$1"; command -v qemu-system-xtensa',
                     'controller-qemu', str(ROOT / 'target/qemu-tools/environment.sh')], env)
    locate.check_returncode()
    qemu = Path(locate.stdout.strip())
    if not qemu.is_file():
        raise FileNotFoundError('install the pinned QEMU tools first')
    args = guide_command('controller-build', BUILD)
    build(args, env, REPORT / 'build.log', expected_image=IMAGE)
    result = boot(qemu, IMAGE, REPORT / 'console.log')
    result['build_command'] = args
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', action='store_true', help='also build and execute NuttX/QEMU')
    args = parser.parse_args()
    env = environment()
    REPORT.mkdir(parents=True, exist_ok=True)
    result_path = REPORT / ('report.json' if args.target else 'host-report.json')
    result_path.unlink(missing_ok=True)
    report = {'result': 'failed', 'toolchain': env['RUSTUP_TOOLCHAIN'],
              'target_requested': args.target}
    try:
        report['sources'] = check_sources()
        report['host'] = host_checks(env)
        if args.target:
            report['target'] = target_checks(env)
        report['result'] = 'passed'
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        result_path.write_text(json.dumps(report, indent=2) + '\n')
    print(f'CONTROLLER_TUTORIAL PASS target={str(args.target).lower()}', flush=True)


if __name__ == '__main__':
    main()
