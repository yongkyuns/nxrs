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
import shutil
import subprocess
import tempfile
import textwrap
import tomllib

from check_examples import ROOT, block, environment

PAGE = 'controller.md'
PROJECT = ROOT / 'website/examples/controller'
PROFILE = ROOT / 'tests/profiles/esp32s3-qemu-pulse-mock.toml'
IMAGE = ROOT / 'target/build/controller/esp32s3-qemu-pulse-mock/nuttx/nuttx.merged.bin'
REPORT = ROOT / 'target/controller-tutorial'
MANIFEST = 'website/examples/controller/Cargo.toml'
HOST = ['cargo', 'run', '--locked', '--manifest-path', MANIFEST, '--bin', 'controller']
TEST = ['cargo', 'test', '--locked', '--manifest-path', MANIFEST]
BUILD = ['cargo', 'nxrs', 'build', '--manifest-path', MANIFEST,
         '--profile', 'esp32s3-qemu-pulse-mock']
DEFAULT_OUTPUT = ['ready: off', 'on: on (25 Hz, 25%)', 'status: on (25 Hz, 25%)',
                  'off: off', 'stopped: off']
STOP_OUTPUT = DEFAULT_OUTPUT[:3] + ['stopped: off']
INVALID_OUTPUT = DEFAULT_OUTPUT[:2] + ['stopped: off']
STATS_MARKER = '<parsed pulse statistics>'
OBSERVATION_COMMANDS = ['on', 'wait', '300', 'stats', 'off']
OBSERVATION_OUTPUT = DEFAULT_OUTPUT[:2] + [STATS_MARKER, 'off: off', 'stopped: off']
INTERACTIVE_INPUT = 'set 25 50\nwait 300\nstats\noff\nstop\n'
INVALID_ERROR = ('controller: unknown command "invalid" '
                 '(use on, off, set HZ DUTY, status, stats, wait MS, stop)')
UNBOUND_ERROR = 'controller: cannot acquire pulse device: Unsupported'


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
    """The guide explains checked excerpts of the one canonical source package."""
    guide = (ROOT / 'website/guide' / PAGE).read_text()
    for name in ('Cargo.toml', 'Cargo.lock', 'src/main.rs', 'src/lib.rs', 'tests/behaviour.rs'):
        if f'../examples/controller/{name}' not in guide:
            raise ValueError(f'the guide must link the canonical {name}')
    library = (PROJECT / 'src/lib.rs').read_text()
    for name in ('commands', 'start', 'admit', 'stop'):
        tag = f'controller-{name}'
        if block(PAGE, tag, 'rust').strip() != source_region(library, tag):
            raise ValueError(f'{tag}: published source differs from the compiled library')
    if block(PAGE, 'controller-output', 'text').splitlines() != DEFAULT_OUTPUT:
        raise ValueError('controller-output: published output differs from the exercised behavior')
    guide_command('controller-host', HOST, mock=True)
    guide_command('controller-test', TEST, mock=True)
    guide_command('controller-build', BUILD)
    manifest = tomllib.loads((PROJECT / 'Cargo.toml').read_text())
    nxrs = manifest['package']['metadata']['nxrs']
    if nxrs['firmware']['command'] != 'controller' or nxrs['firmware']['bin'] != 'controller':
        raise ValueError('the source manifest does not register the exercised shell command')
    if 'pulse' not in nxrs.get('required-capabilities', []):
        raise ValueError('the controller must declare its required pulse capability')
    profile = tomllib.loads(PROFILE.read_text())
    if profile.get('extends') != 'esp32s3-qemu-mock' or profile.get('hal', {}).get('pulse') != 'mock':
        raise ValueError('the tutorial profile must explicitly select mock pulses on ESP32-S3 QEMU')
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


def invoke(args: list[str], env: dict[str, str], *, cwd: Path = ROOT,
           input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    print('$ ' + shlex.join(args), flush=True)
    result = subprocess.run(args, cwd=cwd, env=env, text=True, input=input_text,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', flush=True)
    return result


def parse_stats(line: str) -> dict[str, int | str | None]:
    """Validate receipts and accounting without treating them as physical edge times."""
    match = re.fullmatch(
        r'stats: observed=(?P<observed>[0-9]+) rejected=(?P<rejected>[0-9]+) '
        r'processed=(?P<processed>[0-9]+) gaps=(?P<gaps>[0-9]+) '
        r'max_dispatch_us=(?P<max_dispatch_us>[0-9]+) '
        r'last_interval_us=(?P<last_interval_us>[0-9]+|none) fault=(?P<fault>[A-Za-z]+) '
        r'watchdog_fault=(?P<watchdog_fault>[A-Za-z]+)', line)
    if match is None:
        raise ValueError('malformed pulse statistics')
    values = match.groupdict()
    stats = {key: (None if value == 'none' else int(value))
             for key, value in values.items() if key not in ('fault', 'watchdog_fault')}
    for key in ('fault', 'watchdog_fault'):
        stats[key] = values[key]
        if stats[key] != 'none':
            raise ValueError('pulse acquisition or watchdog reported a terminal fault')
    observed, rejected, processed, gaps = (stats[key] for key in
                                           ('observed', 'rejected', 'processed', 'gaps'))
    if not (0 < processed <= observed <= (1 << 64) - 1
            and rejected + processed <= observed and gaps <= rejected):
        raise ValueError('inconsistent or absent pulse observation accounting')
    if (processed == 1) != (stats['last_interval_us'] is None):
        raise ValueError('pulse interval does not match the processed observations')
    # In-flight or queued receipts can make observed - rejected exceed processed.
    # No equality, expected pulse count, electrical period or latency bound is inferred.
    return stats


def check_output(lines: list[str], expected: list[str]) -> dict | None:
    stats = None
    if len(lines) != len(expected):
        raise ValueError(f'output does not match the completed command sequence: {lines!r}')
    for actual, wanted in zip(lines, expected):
        if wanted == STATS_MARKER:
            if stats is not None:
                raise ValueError('repeated pulse statistics in the expected sequence')
            stats = parse_stats(actual)
        elif actual != wanted:
            raise ValueError(f'output does not match the completed command sequence: {lines!r}')
    return stats


def check_host_response(result: subprocess.CompletedProcess[str], expected: list[str],
                        status: int = 0, error: str | None = None) -> dict | None:
    if result.returncode != status:
        raise ValueError(f'expected host exit status {status}, got {result.returncode}')
    stats = check_output(result.stdout.splitlines(), expected)
    if error is not None and result.stderr.splitlines().count(error) != 1:
        raise ValueError('missing or repeated application error after cleanup')
    return stats


def check_interactive_response(result: subprocess.CompletedProcess[str]) -> dict:
    """Require every prompt for the fixed set/wait/stats/off/stop transcript."""
    if result.returncode != 0:
        raise ValueError('interactive controller did not exit successfully')
    prefixes = ['', 'controller> ', 'controller> controller> ', 'controller> ', 'controller> ']
    lines = result.stdout.splitlines()
    if len(lines) != len(prefixes) or any(not line.startswith(prefix)
                                        for line, prefix in zip(lines, prefixes)):
        raise ValueError('interactive controller prompts are missing or out of order')
    normalized = [line[len(prefix):] for line, prefix in zip(lines, prefixes)]
    return check_output(normalized, ['ready: off', 'set 25 50: on (25 Hz, 50%)',
                                     STATS_MARKER, 'off: off', 'stopped: off'])


def host_checks(env: dict[str, str]) -> list[dict]:
    mock_env = dict(env, NXRS_PLATFORM='mock')
    invoke(guide_command('controller-test', TEST, mock=True), mock_env).check_returncode()
    args = guide_command('controller-host', HOST, mock=True)
    cases = [
        ('default', [], DEFAULT_OUTPUT, 0, None),
        ('explicit-stop', ['on', 'status', 'stop'], STOP_OUTPUT, 0, None),
        ('observe-acquisition', OBSERVATION_COMMANDS, OBSERVATION_OUTPUT, 0, None),
        ('invalid-command-cleanup', ['on', 'invalid'], INVALID_OUTPUT, 1, INVALID_ERROR),
        ('trailing-stop-cleanup', ['on', 'stop', 'off'], INVALID_OUTPUT, 1,
         'controller: stop must be the last command'),
        ('watchdog-unsupported-startup', ['--watchdog', '1000'], [], 1,
         'controller: cannot arm watchdog: Unsupported; pulse cleanup: None'),
        ('reset-cause-unsupported', ['--reset-cause'], [], 1,
         'controller: cannot read reset cause: Unsupported'),
        ('fresh-start-after-error', [], DEFAULT_OUTPUT, 0, None),
    ]
    results = []
    for name, commands, expected, status, error in cases:
        command = args + (['--', *commands] if commands else [])
        result = invoke(command, mock_env)
        stats = check_host_response(result, expected, status, error)
        results.append({'case': name, 'command': command, 'stdout': result.stdout,
                        'stderr': result.stderr, 'exit_status': result.returncode,
                        'statistics': stats, 'result': 'passed'})
    command = args + ['--', '--interactive']
    result = invoke(command, mock_env, input_text=INTERACTIVE_INPUT)
    stats = check_interactive_response(result)
    results.append({'case': 'interactive-reconfiguration', 'command': command,
                    'input': INTERACTIVE_INPUT, 'stdout': result.stdout, 'stderr': result.stderr,
                    'statistics': stats, 'exit_status': result.returncode, 'result': 'passed'})
    unbound_env = dict(env)
    unbound_env.pop('NXRS_PLATFORM', None)
    result = invoke(args, unbound_env)
    check_host_response(result, [], 1, UNBOUND_ERROR)
    results.append({'case': 'unbound-capability-rejection', 'command': args,
                    'profile': None, 'stdout': result.stdout, 'stderr': result.stderr,
                    'exit_status': result.returncode, 'result': 'passed'})
    return results


def project_hashes(project: Path) -> dict[str, str]:
    paths = [project / 'Cargo.lock', *sorted((project / 'src').rglob('*')),
             *sorted((project / 'tests').rglob('*'))]
    return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def copy_detached_project(destination: Path) -> dict[str, str]:
    """Relocate the canonical package, rewriting only its SDK dependency paths."""
    destination.mkdir()
    original = (PROJECT / 'Cargo.toml').read_text()
    manifest = tomllib.loads(original)
    rewritten = original
    for name, dependency in manifest.get('dependencies', {}).items():
        if not isinstance(dependency, dict) or 'path' not in dependency:
            continue
        target = (PROJECT / dependency['path']).resolve()
        target.relative_to(ROOT.resolve())
        if not (target / 'Cargo.toml').is_file():
            raise ValueError(f'{name}: SDK dependency is missing')
        pattern = (rf'(?m)^({re.escape(name)}\s*=\s*\{{[^\n]*?\bpath\s*=\s*)'
                   + re.escape(json.dumps(dependency['path'])))
        rewritten, count = re.subn(pattern, lambda found: found[1] + json.dumps(str(target)),
                                   rewritten)
        if count != 1:
            raise ValueError(f'{name}: expected one canonical dependency path')
        dependency['path'] = str(target)
    if tomllib.loads(rewritten) != manifest:
        raise ValueError('relocation changed more than dependency paths')
    (destination / 'Cargo.toml').write_text(rewritten)
    for name in ('src', 'tests'):
        shutil.copytree(PROJECT / name, destination / name)
    shutil.copyfile(PROJECT / 'Cargo.lock', destination / 'Cargo.lock')
    hashes = project_hashes(PROJECT)
    if project_hashes(destination) != hashes:
        raise ValueError('relocated controller source differs from the canonical package')
    return hashes


def detached_check(env: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory(prefix='nxrs-controller-') as directory:
        project = Path(directory) / 'controller'
        hashes = copy_detached_project(project)
        args = ['cargo', 'run', '--locked', '--manifest-path', str(project / 'Cargo.toml'),
                '--bin', 'controller', '--', *OBSERVATION_COMMANDS]
        detached_env = dict(env, NXRS_PLATFORM='mock', NXRS_SDK_ROOT=str(ROOT.resolve()))
        result = invoke(args, detached_env, cwd=project)
        stats = check_host_response(result, OBSERVATION_OUTPUT)
        if project_hashes(project) != hashes or project_hashes(PROJECT) != hashes:
            raise ValueError('controller source or lockfile changed during detached execution')
        return {'command': args, 'sdk_root': str(ROOT.resolve()), 'sources': hashes,
                'stdout': result.stdout, 'stderr': result.stderr,
                'statistics': stats, 'exit_status': result.returncode, 'result': 'passed'}


def validate_console_response(text: str, sent: str, expected: list[str]) -> dict | None:
    """Require all complete lines and the returning prompt, excluding only exact echo."""
    lines = [line.strip() for line in text.replace('\r', '').splitlines() if line.strip()]
    if not lines or lines[-1] != 'nsh>':
        raise ValueError('missing shell prompt after command')
    lines.pop()
    if lines and lines[0] in (sent, '\x1b[K' + sent):
        lines.pop(0)
    return check_output(lines, expected)


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
        ('controller ' + ' '.join(OBSERVATION_COMMANDS), OBSERVATION_OUTPUT, 0),
        ('controller on invalid', INVALID_OUTPUT + [INVALID_ERROR], 1),
        ('controller', DEFAULT_OUTPUT, 0),
    ]
    results = []
    try:
        console.until('nsh>', 60)
        verify_identity(console.shell)
        for command, expected, status in cases:
            response = console.shell(command)
            stats = validate_console_response(response, command, expected)
            validate_console_response(console.shell('echo $?'), 'echo $?', [str(status)])
            results.append({'command': command, 'console': response, 'statistics': stats,
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
        report['detached'] = detached_check(env)
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
