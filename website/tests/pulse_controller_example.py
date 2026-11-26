#!/usr/bin/env python3
"""Exercise the canonical controller on the host, QEMU, or an existing serial image."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib

from check_examples import ROOT, block, environment

PAGE = 'pulse-controller.md'
PROJECT = ROOT / 'website/examples/pulse-controller'
PROFILE = ROOT / 'tests/profiles/esp32s3-qemu-pulse-mock.toml'
IMAGE = ROOT / 'target/build/controller/esp32s3-qemu-pulse-mock/nuttx/nuttx.merged.bin'
REPORT = ROOT / 'target/pulse-controller-tutorial'
MANIFEST = 'website/examples/pulse-controller/Cargo.toml'
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
CONSOLE_CASES = [
    ('controller', DEFAULT_OUTPUT, 0),
    ('controller on status stop', STOP_OUTPUT, 0),
    ('controller ' + ' '.join(OBSERVATION_COMMANDS), OBSERVATION_OUTPUT, 0),
    ('controller on invalid', INVALID_OUTPUT + [INVALID_ERROR], 1),
    ('controller', DEFAULT_OUTPUT, 0),
]
RECONFIGURE_CASE = ('controller set 25 50 wait 300 stats',
                    ['ready: off', 'set 25 50: on (25 Hz, 50%)', STATS_MARKER, 'stopped: off'], 0)
WATCHDOG_CASE = ('controller --watchdog 3000 on wait 1000 stats',
                 DEFAULT_OUTPUT[:2] + [STATS_MARKER, 'stopped: off'], 0)
HARDWARE_PROFILES = {
    'wroom-pulse': ('xtensa-esp32s3-nuttx', 'esp32s3-devkit:nsh'),
    'pico2w-pulse': ('thumbv8m.main-nuttx-eabi', 'raspberrypi-pico-2:usbnsh'),
}
CANDIDATE_FILES = ('nuttx/nuttx', 'nuttx/nuttx.bin', 'resolved.config',
                   'kconfig-requests.json', 'native-context.json', 'provider-selection.json')
RECEIPT_FILES = ('resolved-profile.json', 'abi-report.json', 'provenance.txt', 'binaries.sha256')
RESET_CAUSES = {'ChipReset', 'Brownout', 'Software', 'ExternalPin', 'LowPower',
                'MainWatchdog', 'RtcWatchdog', 'Unknown'}
FATAL = re.compile(rb'panic|assertion failed|guru meditation', re.I)


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
        if f'../examples/pulse-controller/{name}' not in guide:
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


def console_lines(text: str, sent: str) -> list[str]:
    """Require all complete lines and the returning prompt, excluding only exact echo."""
    lines = [line.strip() for line in text.replace('\r', '').splitlines() if line.strip()]
    if not lines or lines[-1] != 'nsh>':
        raise ValueError('missing shell prompt after command')
    lines.pop()
    if lines and lines[0] in (sent, '\x1b[K' + sent):
        lines.pop(0)
    return lines


def validate_console_response(text: str, sent: str, expected: list[str]) -> dict | None:
    return check_output(console_lines(text, sent), expected)


def console_cases(console, cases: list[tuple], results: list[dict]) -> None:
    """Use the same strict command, cleanup and exit checks for both transports."""
    for command, expected, status in cases:
        if FATAL.search(console.raw):
            raise RuntimeError('guest reported a fatal error')
        case = {'command': command, 'result': 'failed'}
        results.append(case)
        response = case['console'] = console.shell(command)
        if expected is None:
            if not console_lines(response, command):
                raise ValueError(f'{command}: missing diagnostic output')
        else:
            case['statistics'] = validate_console_response(response, command, expected)
        exit_response = case['exit_console'] = console.shell('echo $?')
        validate_console_response(exit_response, 'echo $?', [str(status)])
        if FATAL.search(console.raw):
            raise RuntimeError('guest reported a fatal error')
        case.update(exit_status=status, result='passed')


def boot(qemu: Path, image: Path, log: Path) -> dict:
    # Import Unix PTY support only for target execution, not host/source checks.
    from first_firmware import Console, digest, verify_identity

    before = digest(image)
    args = [str(qemu.resolve()), '-display', 'none', '-serial', 'stdio',
            '-monitor', 'none', '-machine', 'esp32s3', '-nic', 'none',
            '-no-reboot', '-snapshot',
            '-drive', f'file={image.resolve()},if=mtd,format=raw']
    console = Console(args, log)
    results = []
    try:
        console.until('nsh>', 60)
        verify_identity(console.shell)
        console_cases(console, CONSOLE_CASES, results)
        if console.process.poll() is not None:
            raise RuntimeError('emulator exited before test cleanup')
    finally:
        console.close()
    if digest(image) != before:
        raise RuntimeError('QEMU changed the build image despite snapshot mode')
    return {'qemu_command': args, 'image_sha256': before, 'invocations': results,
            'uname': 'NuttX', 'console_log': str(log.relative_to(ROOT)), 'result': 'passed'}


def file_receipt(path: Path) -> dict:
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    return {'sha256': digest, 'bytes': path.stat().st_size}


def validate_device_build(build_dir: Path, installed_image_sha256: str | None = None) -> dict:
    """Check a local candidate, including relocated CI artifacts, before serial I/O.

    Recorded absolute paths are identity labels, never paths to open. Only the
    fixed files below the supplied build directory and the canonical checkout's
    manifest are read. These receipts do not establish what a board has flashed.
    """
    build_dir = build_dir.resolve(strict=True)
    files = {}
    for name in CANDIDATE_FILES + RECEIPT_FILES:
        path = (build_dir / name).resolve(strict=True)
        if not path.is_relative_to(build_dir) or not path.is_file():
            raise ValueError(f'candidate file escapes the build directory: {name}')
        files[name] = file_receipt(path)

    ledger = {}
    for line in (build_dir / 'binaries.sha256').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if match is None:
            raise ValueError('malformed binaries.sha256 entry')
        digest, recorded = match.groups()
        path = PurePosixPath(recorded)
        if not path.is_absolute() or '..' in path.parts or '\0' in recorded:
            raise ValueError('noncanonical path in binaries.sha256')
        if path in ledger and ledger[path] != digest:
            raise ValueError('conflicting duplicate in binaries.sha256')
        ledger[path] = digest

    origins = set()
    for name in CANDIDATE_FILES:
        suffix = PurePosixPath(name).parts
        matches = [path for path in ledger if path.parts[-len(suffix):] == suffix]
        if len(matches) != 1:
            raise ValueError(f'missing or ambiguous candidate hash: {name}')
        recorded = matches[0]
        origins.add(PurePosixPath(*recorded.parts[:-len(suffix)]))
        if ledger[recorded] != files[name]['sha256']:
            raise ValueError(f'candidate hash mismatch: {name}')
    if len(origins) != 1:
        raise ValueError('candidate hashes come from different build directories')
    origin = origins.pop()

    resolved = json.loads((build_dir / 'resolved-profile.json').read_text())
    provider = json.loads((build_dir / 'provider-selection.json').read_text())
    native = json.loads((build_dir / 'native-context.json').read_text())
    abi = json.loads((build_dir / 'abi-report.json').read_text())
    requests = json.loads((build_dir / 'kconfig-requests.json').read_text())
    profile = resolved.get('profile')
    if profile not in HARDWARE_PROFILES:
        raise ValueError('serial acceptance requires wroom-pulse or pico2w-pulse')
    target, board = HARDWARE_PROFILES[profile]
    data = resolved.get('data', {})
    composition = provider.get('resolved-composition', {})
    workspace = PurePosixPath(resolved.get('workspace', ''))
    manifest = str(workspace / MANIFEST)
    if not workspace.is_absolute() or '..' in workspace.parts \
            or resolved.get('product_manifest') != manifest \
            or provider.get('app_manifest') != manifest \
            or provider.get('app_package') != 'nxrs-pulse-controller':
        raise ValueError('candidate does not identify the canonical controller package')
    manifest_receipt = file_receipt(PROJECT / 'Cargo.toml')
    # Git may check this text file out as CRLF on a Windows serial-testing host.
    # Only this checkout comparison tolerates that conversion; candidate image,
    # config and receipt hashes remain exact bytes from the downloaded build.
    normalized_manifest_hash = hashlib.sha256(
        (PROJECT / 'Cargo.toml').read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    build_manifest_hash = ledger.get(PurePosixPath(manifest))
    if build_manifest_hash not in (manifest_receipt['sha256'], normalized_manifest_hash):
        raise ValueError('candidate manifest differs from the canonical controller manifest')
    if resolved.get('version') != 1 or resolved.get('os') != 'nuttx' \
            or resolved.get('target') != target or data.get('target') != target \
            or data.get('name') != profile or data.get('board') != board \
            or data.get('image') != 'nuttx.bin' or data.get('requires-qemu') is not False \
            or provider.get('platform') != profile or provider.get('target_os') != 'nuttx' \
            or provider.get('nuttx-build-mode') != 'flat' \
            or provider.get('dependency_resolution', {}).get('status') != 'resolved':
        raise ValueError('candidate profile, target or provider selection is inconsistent')
    if data.get('composition', {}).get('mode') != 'hardware' \
            or composition.get('graph', {}).get('mode') != 'hardware':
        raise ValueError('serial acceptance requires resolved hardware composition')
    for resolved_key, provider_key in (('semantic_digest', 'semantic-digest'),
                                       ('raw_fingerprint', 'input-fingerprint')):
        value = resolved.get(resolved_key, '')
        if not re.fullmatch(r'[0-9a-f]{64}', value) or composition.get(provider_key) != value:
            raise ValueError('resolved profile does not match the provider receipt')
    for capability in ('pulse', 'boot', 'watchdog'):
        bindings = [entry for entry in provider.get('providers', [])
                    if entry.get('capability') == capability]
        if data.get('hal', {}).get(capability) != 'nuttx' or bindings != [{
                'capability': capability, 'implementation': 'nuttx', 'package': f'nxrs-{capability}'}]:
            raise ValueError(f'candidate must select the native {capability} provider')

    config_hash = files['resolved.config']['sha256']
    header_hash = ledger.get(origin / 'nuttx/include/nuttx/config.h')
    headers_hash = native.get('headers_sha256', '')
    if native.get('schema') != 1 or native.get('target') != target \
            or native.get('effective_config_sha256') != config_hash \
            or requests.get('effective_config_sha256') != config_hash \
            or abi.get('target') != target or abi.get('config_sha256') != config_hash \
            or header_hash is None or native.get('config_sha256') != header_hash \
            or abi.get('config_header_sha256') != header_hash \
            or not re.fullmatch(r'[0-9a-f]{64}', headers_hash) \
            or abi.get('headers_sha256') != headers_hash:
        raise ValueError('candidate configuration and native/ABI receipts do not match')
    if abi.get('errors') != [] or abi.get('probe_abi_compatible') is not True \
            or abi.get('poll_abi_compatible') is not True or not abi.get('checks'):
        raise ValueError('candidate ABI gates did not pass')
    for check in abi['checks']:
        if check.get('status') != 'compatible' and not (
                check.get('name') == 'SIG_IGN' and check.get('status') == 'known-unsupported-SIG_IGN'):
            raise ValueError('candidate ABI check disagrees with the reported gate results')
    config = (build_dir / 'resolved.config').read_text()
    required = data.get('kconfig', {}).get('require', [])
    forbidden = data.get('kconfig', {}).get('forbid-regex')
    if not required or any(line not in config.splitlines() for line in required) \
            or (forbidden and re.search(forbidden, config, re.M)):
        raise ValueError('candidate effective configuration violates its profile gates')
    arguments = re.findall(r'^CONFIG_NSH_MAXARGUMENTS=([0-9]+)$', config, re.M)
    if len(arguments) != 1 or int(arguments[0]) < 7:
        raise ValueError('candidate NSH argument budget is too small for the controller cases')

    provenance = {}
    keys = {'app_package', 'app_bin', 'app_command', 'platform', 'target', 'board',
            'nxrs', 'nxrs_dirty', 'abi_profile', 'nuttx_build_mode'}
    for line in (build_dir / 'provenance.txt').read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator and key in keys:
            if key in provenance:
                raise ValueError(f'duplicate provenance value: {key}')
            provenance[key] = value
    expected = {'app_package': 'nxrs-pulse-controller', 'app_bin': 'controller',
                'app_command': 'controller', 'platform': profile, 'target': target,
                'board': board, 'abi_profile': 'active', 'nuttx_build_mode': 'flat'}
    if any(provenance.get(key) != value for key, value in expected.items()):
        raise ValueError('candidate provenance does not match its profile or controller command')
    if not re.fullmatch(r'[0-9a-f]{40}', provenance.get('nxrs', '')) \
            or provenance.get('nxrs_dirty') not in ('yes', 'no'):
        raise ValueError('candidate provenance is missing its build revision or dirty state')

    declared = None
    if installed_image_sha256 is not None:
        declared = installed_image_sha256.lower()
        if not re.fullmatch(r'[0-9a-f]{64}', declared) or declared not in (
                files['nuttx/nuttx']['sha256'], files['nuttx/nuttx.bin']['sha256']):
            raise ValueError('declared installed-image hash does not match the local ELF or binary')
    return {'directory': str(build_dir), 'profile': profile, 'target': target, 'board': board,
            'files': files, 'manifest': manifest_receipt, 'build_manifest_sha256': build_manifest_hash,
            'build_provenance': provenance,
            'declared_installed_image_sha256': declared,
            'installed_image_verified': False,
            'current_checkout_sources': project_hashes(PROJECT),
            'compiled_source_identity_verified': False}


def serial_checks(port: str, build_dir: Path, *, repeat: int = 10, watchdog: bool = False,
                  out: Path | None = None, installed_image_sha256: str | None = None,
                  console_factory=None) -> dict:
    """Run an already flashed controller; never flash, reset or change candidates."""
    if not isinstance(port, str) or not port.strip() or any(char in port for char in '\r\n\0'):
        raise ValueError('serial port must be a nonempty device name')
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 100:
        raise ValueError('repeat must be between 1 and 100')
    build_dir = build_dir.resolve()
    parent = REPORT.resolve() if out is None else out.resolve()
    if parent.is_relative_to(build_dir):
        raise ValueError('serial reports must be outside the candidate build directory')
    if out is None:
        parent.mkdir(parents=True, exist_ok=True)
        out = Path(tempfile.mkdtemp(prefix='serial-', dir=parent))
    else:
        out = parent
        out.mkdir(parents=True, exist_ok=False)
    result_path = out / 'report.json'
    log = out / 'console.log'
    log.write_bytes(b'')
    report = {'result': 'failed', 'scope': 'serial-console acceptance',
              'started_at': datetime.now(timezone.utc).isoformat(), 'port': port,
              'repeat': repeat, 'watchdog_requested': watchdog,
              'installed_image_verified': False, 'console_log': str(log),
              'invocations': [], 'resources': {'assessment': 'captured; no heap or stack threshold'}}
    try:
        report['local_candidate'] = validate_device_build(build_dir, installed_image_sha256)
        # Do not import the Unix PTY/QEMU harness, pyserial, or a compiler during
        # candidate validation. This branch also works with copied CI artifacts.
        sys.path.insert(0, str(ROOT / 'tests'))
        from rtos_harness.nuttx import verify_identity
        if console_factory is None:
            from rtos_harness.serial_console import SerialConsole
            console_factory = SerialConsole
        with console_factory(port, log, timeout=20.0) as console:
            console.synchronize()
            report['uname_console'] = verify_identity(console.shell)
            console_cases(console, CONSOLE_CASES + [RECONFIGURE_CASE], report['invocations'])
            if watchdog:
                console_cases(console, [WATCHDOG_CASE], report['invocations'])
            reset = report['reset_cause'] = []
            console_cases(console, [('controller --reset-cause', None, 0)], reset)
            lines = console_lines(reset[0]['console'], 'controller --reset-cause')
            if len(lines) != 1 or not lines[0].startswith('reset cause: ') \
                    or lines[0].removeprefix('reset cause: ') not in RESET_CAUSES:
                reset[0]['result'] = 'failed'
                raise ValueError('missing or malformed reset cause')
            for phase in ('after_warmup', 'after_repeats'):
                snapshot = report['resources'][phase] = []
                console_cases(console, [('free', None, 0), ('ps', None, 0)], snapshot)
                if phase == 'after_warmup':
                    console_cases(console, [CONSOLE_CASES[2]] * repeat, report['invocations'])
            if FATAL.search(console.raw):
                raise RuntimeError('guest reported a fatal error')
        if validate_device_build(build_dir, installed_image_sha256) != report['local_candidate']:
            raise RuntimeError('local candidate or checkout changed during serial acceptance')
        report['result'] = 'passed'
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        result_path.write_text(json.dumps(report, indent=2) + '\n')
        print(f'Controller serial report: {result_path}', flush=True)
    return report


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--target', action='store_true', help='also build and execute NuttX/QEMU')
    mode.add_argument('--serial-port', help='test an already flashed physical controller at 115200 baud')
    parser.add_argument('--build-dir', type=Path, help='physical controller build artifacts to validate')
    parser.add_argument('--repeat', type=int, help='serial observation cycles after warm-up (1..100; default 10)')
    parser.add_argument('--watchdog', action='store_true', help='also exercise a 3000 ms runtime watchdog')
    parser.add_argument('--out', type=Path, help='new serial report directory; must not exist')
    parser.add_argument('--installed-image-sha256', help='optional operator-declared ELF/bin hash; not device verification')
    args = parser.parse_args(argv)
    if args.serial_port is not None:
        if args.build_dir is None:
            parser.error('--serial-port requires --build-dir')
        serial_checks(args.serial_port, args.build_dir, repeat=10 if args.repeat is None else args.repeat,
                      watchdog=args.watchdog, out=args.out,
                      installed_image_sha256=args.installed_image_sha256)
        print('CONTROLLER_SERIAL PASS installed_image_verified=false', flush=True)
        return
    if any(value is not None for value in (args.build_dir, args.repeat, args.out,
                                          args.installed_image_sha256)) or args.watchdog:
        parser.error('--build-dir, --repeat, --watchdog, --out and --installed-image-sha256 require --serial-port')
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
