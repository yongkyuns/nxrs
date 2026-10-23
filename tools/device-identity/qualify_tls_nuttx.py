#!/usr/bin/env python3
"""Execute scoped SDK TLS over real NuttX TCP in two fresh private images.

An independent, fully verifying OpenSSL peer reaches the target TCP client via
bounded console ciphertext frames. The server, Mbed TLS and TCP loopback execute
inside NuttX. This proves neither external network/RF nor production resources,
onboard entropy, trusted-time acquisition, protected floors or key custody.
Only qualification.json may leave the private work directory.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load('tls_nuttx_identity_tools', Path(__file__).with_name('qualify_nuttx.py'))
tls = load('tls_nuttx_peer', Path(__file__).with_name('target_tls.py'))
require, digest, canonical = base.require, base.digest, base.canonical
INPUTS = tuple(dict.fromkeys((*base.INPUTS,
    'tools/device-identity/qualify_tls_nuttx.py',
    'tests/host/test_target_tls_nuttx.py',
    'service/stream/src/worker.rs',
    'app/camera-stream/Cargo.toml', 'service/camera/Cargo.toml',
    'service/stream/Cargo.toml', 'hal/camera/Cargo.toml',
    'hal/camera/src/lib.rs',
    'platform/hal-build.rs')))
REPORT_FIELDS = {'captured', 'busy', 'frames', 'tx', 'rx', 'gap_ms', 'elapsed_ms',
    'buffer_bytes', 'worker_stack', 'rejected', 'refused_frames', 'accepted',
    'disconnected', 'errors', 'retired', 'lease_released'}


def transcript(text, status, mode):
    require(mode == 'pass' and len(text) <= 256 * 1024 and len(status) <= 1024)
    text = text.replace('\r', '')
    require(text.splitlines().count('NXRS_TLS_TARGET_ENTERED') == 1)
    require(not any(value in text for value in ('PANIC', 'panic:', 'panicked at', 'Assertion failed')))
    require(re.findall(r'^NXRS_STATUS_([0-9]+)\s*$', status.replace('\r', ''), re.M) == ['0'])
    for index in (1, 2):
        require(text.splitlines().count(f'NXRS_TLS_RECONNECTED {index} refused_frames=0') == 1)
    reports = re.findall(r'^NXRS_TLS_TARGET_REPORT (.*)$', text, re.M)
    require(len(reports) == 1)
    fields = reports[0].split()
    require(len(fields) == len(REPORT_FIELDS) and all(re.fullmatch('[a-z_]+=[0-9]{1,12}', value) for value in fields))
    values = dict(value.split('=', 1) for value in fields)
    require(set(values) == REPORT_FIELDS)
    values = {name: int(value) for name, value in values.items()}
    for name in ('captured', 'frames', 'tx', 'rx', 'elapsed_ms'):
        require(values[name] > 0)
    require(values['frames'] >= 2 and values['elapsed_ms'] < 150000)
    for name, value in dict(buffer_bytes=8192, worker_stack=65536, rejected=2,
        refused_frames=0, accepted=3, disconnected=3, errors=2, retired=1, lease_released=1).items():
        require(values[name] == value)
    return values


def execute(args):
    peer = tls.Interaction(args.authority, args.version)
    def verify(text, status, mode):
        target = transcript(text, status, mode)
        require(peer.result is not None and peer.result['verified_frames'] == 2
                and peer.result['missing_client_certificate_refused'] is True
                and peer.result['cleartext_refused'] is True
                and peer.result['recovery_after_two_refusals'] is True
                and peer.result['refused_media_frames'] == 0 and peer.result['qualified'] is False)
        (args.output / 'controls.json').write_bytes(canonical(dict(target=target, peer=peer.result)) + b'\n')
    base.runner.run_cases(args.execute / 'nuttx/nuttx', args.output,
        [('pass', 'identity_tls')], verify, args.execute / 'nuttx/nuttx.merged.bin', interaction=peer)


def qualify(args):
    import cryptography
    canonical_host = sys.platform == 'linux' and platform.machine() == 'x86_64' and args.local_tools is None
    require(canonical_host or (sys.platform == 'darwin' and platform.machine() == 'arm64'
                              and args.local_tools is not None))
    require(sys.version_info >= (3, 12) and cryptography.__version__ == '50.0.2')
    tools = (args.local_tools or ROOT / 'target/qemu-tools').resolve()
    require((tools / 'environment.sh').is_file())
    work = args.work.absolute()
    require(not work.exists() and not work.is_symlink())
    work.mkdir(mode=0o700); (work / 'logs').mkdir(mode=0o700)
    inputs = {name: digest(ROOT / name) for name in INPUTS}
    for parent in ('upstream/nuttx', 'upstream/nuttx-apps', 'upstream/esp-hal-3rdparty'):
        for path in sorted((ROOT / parent).rglob('*')):
            if path.is_file(): inputs[str(path.relative_to(ROOT))] = digest(path)
    def git(*arguments):
        return subprocess.check_output(['git', *arguments], cwd=ROOT, text=True, timeout=30).strip()
    checkout = git('rev-parse', 'HEAD')
    require(not git('status', '--porcelain', '--untracked-files=normal'))
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'RUSTUP_HOME', 'CARGO_HOME', 'TMPDIR') if key in os.environ}
    environment.update(NXRS_BOOT_PYTHON=sys.executable, PYTHONNOUSERSITE='1', NUTTX_STD_COMPAT_FIXES='1',
                       NXRS_QEMU_TOOLS_DIR=str(tools))
    if not canonical_host: environment['NXRS_ESP32S3_NATIVE_TOOLS'] = '1'
    variants = []
    for index, version in enumerate(('13', '12')):
        print(f'Building fresh private TLS {version} target image {index + 1}/2', flush=True)
        seed = work / f'entropy-{index}.bin'
        with seed.open('xb') as output: output.write(secrets.token_bytes(4096))
        authority = work / f'authority-{index}'
        fixture = tls.prepare(authority)
        out = work / f'image-{index}'
        env = dict(environment, NXRS_IDENTITY_ENTROPY_FIXTURE=str(seed), NXRS_IDENTITY_CERTIFICATE_FIXTURE=str(authority))
        command = ['bash', '-euc', 'source "$1"; shift; exec bash "$@"', 'tls-target-build', str(tools / 'environment.sh'),
            str(ROOT / 'tools/build-nuttx-std-app.sh'), '--app-manifest', 'tests/nuttx-std/identity/Cargo.toml',
            '--app-package', 'nxrs-identity-sdk-target', '--bin', 'identity-tls-target', '--command', 'identity_tls',
            '--priority', '100', '--stack-size', '65536', '--platform', 'esp32s3-identity-tls',
            '--feature', 'qualification', '--out', str(out)]
        status, stdout = base.process.run(command, ROOT, env, work / 'logs', f'build-{index}', 1800)
        if status:
            base.diagnostics(stdout); base.diagnostics((work / 'logs' / f'build-{index}.err').read_text(errors='replace'))
            base.cargo_diagnostics(out / 'cargo-messages.jsonl')
        require(status == 0)
        status, _ = base.process.run(['bash', '-euc', 'source "$1"; exec "$2" "$3" --out "$4" --tcp',
            'tls-tcp-abi', str(tools / 'environment.sh'), sys.executable,
            str(ROOT / 'tests/nuttx-std/udp/check-abi.py'), str(out)], ROOT, env, work / 'logs', f'abi-{index}', 120)
        require(status == 0)
        build = base.record_build(out, certificate_scope=tls.SCOPE, entry_symbol='identity_tls_main', tls_loopback=True)
        require(fixture == tls.public_bindings(authority) == build['sdk_build']['certificate_fixture'])
        execution = work / f'execution-{index}'
        status, _ = base.process.run(['bash', '-euc', 'source "$1"; exec "$2" "$3" --execute "$4" --output "$5" --authority "$6" --version "$7"',
            'tls-target-execute', str(tools / 'environment.sh'), sys.executable, str(Path(__file__).resolve()),
            str(out), str(execution), str(authority), version], ROOT, env, work / 'logs', f'execute-{index}', 210)
        require(status == 0 and digest(out / 'nuttx/nuttx.merged.bin') == build['flash_image_sha256'])
        results = json.loads((execution / 'results.json').read_bytes())
        require(len(results) == 1 and results[0]['success'] is True and results[0]['image_sha256'] == build['flash_image_sha256'])
        console = (execution / 'console-0-pass.log').read_text().replace('\r', '')
        status_lines = '\n'.join(line for line in console.splitlines() if line.startswith('NXRS_STATUS_'))
        controls = json.loads((execution / 'controls.json').read_bytes())
        require(controls['target'] == transcript(console, status_lines, 'pass'))
        require(controls['peer']['tls_version'] == ('TLSv1.3' if version == '13' else 'TLSv1.2'))
        require(fixture == tls.public_bindings(authority))
        variants.append(dict(build=build, controls=controls, entropy_fixture_sha256=digest(seed), fresh_kernel=True))
    for key in ('entropy_fixture_sha256',):
        require(len({value[key] for value in variants}) == 2)
    require(len({value['build']['flash_image_sha256'] for value in variants}) == 2)
    require(len({value['controls']['peer']['approved_server_der_sha256'] for value in variants}) == 2)
    require(inputs == {name: digest(ROOT / name) for name in inputs} and checkout == git('rev-parse', 'HEAD')
            and not git('status', '--porcelain', '--untracked-files=normal'))
    report = dict(schema=1, scope='actual-xtensa-nuttx-sdk-tls-tcp-loopback-with-host-trust-fixtures',
        qualified=True, canonical_linux_qualified=canonical_host, checkout=dict(commit=checkout, clean=True),
        inputs=inputs, variants=variants, fresh_images=2, tls_versions=['TLSv1.3', 'TLSv1.2'],
        verified_jpeg_frames=4, unauthenticated_connections_refused=4, refused_media_frames=0,
        tls_loopback_runtime_qualified=True, device_tls_qualified=False, external_network_qualified=False,
        production_resource_budgets_qualified=False, stack_high_water_measured=False,
        hardware_entropy_qualified=False, protected_device_key_storage_qualified=False,
        trusted_time_acquisition_qualified=False, protected_generation_qualified=False,
        native_advisory_closure=False, physical_qualified=False, fips_validated=False,
        firmware_release_qualified=False, deployment_permitted=False)
    raw = canonical(report) + b'\n'; require(len(raw) <= 65536)
    (work / 'qualification.json').write_bytes(raw)
    print('PASS: two fresh NuttX TLS/TCP kernels, exact JPEGs, unauthenticated/cleartext refusal and recovery; production gates remain open')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--local-tools', type=Path, help='explicit private macOS prototype tools; never canonical Linux evidence')
    parser.add_argument('--execute', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--authority', type=Path)
    parser.add_argument('--version', choices=('12', '13'))
    args = parser.parse_args(); os.umask(0o077)
    try:
        if args.execute:
            require(args.output is not None and args.authority is not None and args.version is not None
                    and args.work is None and args.local_tools is None)
            execute(args)
        else:
            require(args.work is not None and args.output is None and args.authority is None and args.version is None)
            qualify(args)
    except Exception:
        print('NuttX TLS qualification refused; private diagnostics retained', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
