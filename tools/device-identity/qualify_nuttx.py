#!/usr/bin/env python3
"""Execute the real identity SDK/Rust boundary in two fresh NuttX/QEMU images.

Each image embeds fresh private host OS entropy, solely as an ABI fixture. Images,
seeds, SDK source, CSRs, UART, build commands and issuer state stay private. Only
qualification.json is publishable. This does not qualify an onboard RNG or key
custody, and the same fixture image must never count as fresh entropy on reboot.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity = load('identity_host_tools', Path(__file__).with_name('qualify.py'))
certificate = load('target_certificate_issuer', Path(__file__).with_name('target_issuer.py'))
process = identity.process
runner = load('identity_nuttx_runner', ROOT / 'tests/nuttx-std/run.py')
require = identity.require
digest = identity.digest
canonical = identity.canonical
INPUTS = tuple(dict.fromkeys((*identity.INPUTS,
    'tools/device-identity/build_nuttx.py', 'tools/device-identity/qualify_nuttx.py',
    'tools/device-identity/target_issuer.py', 'tests/host/test_target_certificate_issuer.py',
    'tests/host/test_device_identity_nuttx.py',
    'tests/host/test_nuttx_native_inputs.py',
    'tests/host/test_platform_profiles.py', 'tests/nuttx-std/identity/Cargo.toml',
    'tests/nuttx-std/identity/src/main.rs', 'platform/nuttx/platforms/esp32s3-identity-sdk.toml',
    'platform/nuttx/platforms/esp32s3-qemu-mock.toml', 'platform/nuttx/std-app/Makefile',
    'tools/build-nuttx-std-app.sh', 'tools/platform_config.py', 'tools/nuttx_native_inputs.py',
    'tools/install-qemu-tools.sh', 'tools/boot/qualify.py', 'tools/boot/install_target.py',
    'tools/boot/target-requirements.txt', 'tools/ci-apt.py', 'tools/apply-nuttx-patches.py',
    'tools/prepare-esp-hal-cache.sh', 'tools/nuttx-version.py',
    'tests/nuttx-std/run.py', 'tests/nuttx-std/check-abi.py', 'tests/nuttx-std/prepare-std.py',
    'tests/nuttx-std/link.py', 'tests/nuttx-std/test-link.py', 'tests/rtos_harness/nuttx.py',
    'docs/hal-platform-architecture.md', 'docs/nuttx-device-access.md',
    '.github/workflows/device-identity-nuttx.yml')))
REPORT = ('requests=2 startup_refused=1 busy_refused=1 parallel_busy_refused=1 thread_joined=1 deadline_checked=1 refresh_refused=1 '
          'sticky_refused=1 restored=1 certificates_bound=2 certificate_refusals=26 expired_bindings=2 poisoned_certificate_refused=1 entropy_bytes=')


def transcript(text, status, mode):
    require(mode == 'pass' and len(text) <= 65536 and len(status) <= 1024)
    text = text.replace('\r', '')
    lines = text.splitlines()
    require(lines.count('NXRS_IDENTITY_TARGET_ENTERED') == 1)
    for label in ('A', 'B'):
        require(lines.count('NXRS_IDENTITY_CERTIFICATE_READY ' + label) == 1)
        require(lines.count('NXRS_IDENTITY_CERTIFICATE_BOUND ' + label + ' refusals=13 expiry_checked=1') == 1)
    require(not any(word in text for word in ('PANIC', 'panic:', 'panicked at', 'Assertion failed')))
    require(re.findall(r'^NXRS_STATUS_([0-9]+)\s*$', status.replace('\r', ''), re.M) == ['0'])
    reports = [line.removeprefix('NXRS_IDENTITY_TARGET_REPORT ') for line in lines
               if line.startswith('NXRS_IDENTITY_TARGET_REPORT ')]
    require(len(reports) == 1 and reports[0].startswith(REPORT))
    count = reports[0][len(REPORT):]
    require(re.fullmatch('[1-9][0-9]{0,3}', count) and 0 < int(count) <= 4096)
    blocks = re.findall(r'^NXRS_IDENTITY_CSR_BEGIN ([AB])\n(.*?)^NXRS_IDENTITY_CSR_END \1$', text, re.M | re.S)
    require([label for label, _ in blocks] == ['A', 'B'])
    require(sum(line.startswith('NXRS_IDENTITY_CSR_BEGIN ') for line in lines) == 2)
    require(sum(line.startswith('NXRS_IDENTITY_CSR_END ') for line in lines) == 2)
    measurements = re.findall(r'^NXRS_IDENTITY_CERTIFICATE_MEASURE ([AB]) bind_ms=([0-9]+) short_bind_ms=([0-9]+) expiry_wait_ms=([0-9]+)$', text, re.M)
    require([value[0] for value in measurements] == ['A', 'B'])
    timing = []
    for label, binding, short, wait in measurements:
        binding, short, wait = int(binding), int(short), int(wait)
        require(0 <= binding < 300000 and 0 <= short < 10000 and 0 < wait <= 10100)
        timing.append(dict(key_label=label, binding_ms=binding, short_binding_ms=short, expiry_wait_ms=wait))
    for _, pem in blocks:
        require(pem.startswith('-----BEGIN CERTIFICATE REQUEST-----\n')
                and pem.endswith('-----END CERTIFICATE REQUEST-----\n') and len(pem) <= 2048)
    return dict(entropy_fixture_bytes_consumed=int(count), successful_requests=2,
                startup_refused=True, busy_refused=True, parallel_busy_refused=True,
                thread_joined=True, deadline_checked=True, refresh_refused=True, sticky_refused=True,
                owner_recovery=True, certificates_bound=2, certificate_refusals=26, expired_bindings=2,
                poisoned_certificate_refused=True, certificate_measurements=timing, requests=[pem.encode('ascii') for _, pem in blocks])


def diagnostics(stderr):
    # Publish only fixed stage/category names and SDK symbol/header identifiers.
    # Raw compiler output, paths and command lines remain private.
    stages = re.findall(r'^NXRS_NUTTX_BUILD_STAGE=([a-z-]{1,32})$', stderr, re.M)
    if stages: print('NXRS_IDENTITY_TARGET_FAILURE stage=' + stages[-1], file=sys.stderr)
    # Cargo can refuse before rustc produces JSON (lock, feature, download or
    # target discovery). Categories are fixed; no Cargo message is published.
    categories = {
        'locked-resolution': r'(?:lock file|lockfile)[^\n]*(?:needs to be updated|needs updating|--locked)',
        'dependency-resolution': r'(?:failed to select a version|no matching package named|failed to resolve)',
        'feature-selection': r'(?:does not have (?:these |the )?feature|contains (?:this |these )?feature|none of the selected packages)',
        'manifest': r'(?:failed to (?:parse|load) manifest|manifest path[^\n]*does not exist)',
        'package-selection': r'package ID specification[^\n]*did not match',
        'dependency-transport': r'(?:failed to download|failed to fetch|failed to get [^\n]*dependency)',
        'target-discovery': r'(?:target-specific information|could not find specification|Error loading target specification|target file[^\n]*does not exist)',
        'dependency-integrity': r'checksum[^\n]*(?:changed|does not match)',
        'storage-full': r'(?:No space left on device|Disk quota exceeded)',
        'pipe-failure': r'(?:Broken pipe|broken pipe)',
        'permission': r'Permission denied',
        'file-missing': r'No such file or directory',
        'tool-format': r'(?:Exec format error|cannot execute binary file)',
        'rustc-probe': r'(?:failed to run [^\n]*rustc|could not execute process[^\n]*rustc)',
    }
    for category, pattern in categories.items():
        if re.search(pattern, stderr):
            print('NXRS_IDENTITY_TARGET_CARGO_FAILURE category=' + category, file=sys.stderr)
    for line, status in re.findall(r'^NXRS_NUTTX_BUILD_FAILURE line=([0-9]{1,5}) status=([0-9]{1,3})$', stderr, re.M):
        print('NXRS_IDENTITY_TARGET_BACKEND_FAILURE line=' + line + ' status=' + status, file=sys.stderr)
    for token in ('pthread_create', 'pthread_join', 'clock_gettime'):
        if 'Active std app missing expected import ' + token in stderr:
            print('NXRS_IDENTITY_TARGET_IMPORT_FAILURE token=' + token, file=sys.stderr)
    capture_categories = {'record', 'arguments', 'compiler-identity', 'dependency-probe',
        'dependency-file', 'source-mutation', 'object', 'origin', 'duplicate-output', 'sdk-identity', 'io', 'compiler-status'}
    for category in sorted(set(re.findall(r'NXRS_NATIVE_INPUT_FAILURE category=([a-z-]+)', stderr))):
        if category in capture_categories:
            print('NXRS_IDENTITY_TARGET_CAPTURE_FAILURE category=' + category, file=sys.stderr)
    regions = ('iram0_0_seg', 'dram0_0_seg', 'irom0_0_seg', 'drom0_0_seg')
    for region in regions:
        match = re.search(r"region [`'\"]" + region + r"['\"] overflowed by ([0-9]{1,10}) bytes", stderr)
        if match:
            print('NXRS_IDENTITY_TARGET_LINK_MEMORY region=' + region + ' overflow_bytes=' + match[1], file=sys.stderr)
    for token in sorted(set(re.findall(r'relocation truncated to fit: (R_XTENSA_[A-Z0-9_]{1,64})', stderr))):
        print('NXRS_IDENTITY_TARGET_LINK_FAILURE category=relocation-range token=' + token, file=sys.stderr)
    if 'dangerous relocation' in stderr:
        print('NXRS_IDENTITY_TARGET_LINK_FAILURE category=dangerous-relocation', file=sys.stderr)
    if 'unbound variable' in stderr:
        print('NXRS_IDENTITY_TARGET_SHELL_FAILURE category=unbound-variable', file=sys.stderr)
        # These are source variable names, never values or arbitrary log words.
        known = ('CARGO_BIN', 'CARGO_CONFIG_ARGS', 'NXRS_APP_MANIFEST', 'NXRS_APP_PACKAGE',
                 'CARGO_FEATURES_CSV', 'NXRS_APP_BIN', 'TARGET_ARG', 'STD_FEATURE_ARGS', 'OUT', 'RUSTFLAGS')
        for token in known:
            if re.search(r'\b' + token + r'(?:\[[^]\n]{0,16}\])?: unbound variable', stderr):
                print('NXRS_IDENTITY_TARGET_SHELL_VARIABLE token=' + token, file=sys.stderr)
    for category, phrase in (('expansion', 'bad substitution'), ('syntax', 'syntax error'),
                             ('argument-budget', 'Argument list too long'), ('file-limit', 'Too many open files'),
                             ('read-only-output', 'Read-only file system'), ('output-is-directory', 'Is a directory'),
                             ('llvm-backend', 'LLVM ERROR'), ('compiler-internal', 'internal compiler error')):
        if phrase in stderr:
            print('NXRS_IDENTITY_TARGET_FAILURE_CATEGORY category=' + category, file=sys.stderr)
    if re.search(r'^tee:', stderr, re.M):
        print('NXRS_IDENTITY_TARGET_FAILURE_CATEGORY category=output-sink', file=sys.stderr)
    for name in sorted(set(re.findall(r"undefined reference to [`']([A-Za-z_][A-Za-z0-9_]{0,127})'", stderr))):
        print('NXRS_IDENTITY_TARGET_UNDEFINED symbol=' + name, file=sys.stderr)
    for code in sorted(set(re.findall(r'error\[(E[0-9]{4})\]', stderr))):
        print('NXRS_IDENTITY_TARGET_RUST_ERROR code=' + code, file=sys.stderr)
    for line in stderr.splitlines():
        if re.fullmatch(r'NXRS_IDENTITY_SDK_FAILURE stage=[a-z-]+ category=[a-z-]+(?: token=[A-Za-z0-9_.-]{1,128})?', line):
            print(line, file=sys.stderr)
    for token in sorted(set(re.findall(r'cannot find -l([A-Za-z0-9_.-]{1,64})', stderr))):
        print('NXRS_IDENTITY_TARGET_LINK_FAILURE category=missing-library token=' + token, file=sys.stderr)


def cargo_diagnostics(path):
    # Cargo sends compiler errors to JSON stdout. Read the private message file,
    # publishing only bounded diagnostic codes/categories and linker identifiers.
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        return
    for line in path.read_text(errors='replace').splitlines():
        try: row = json.loads(line)
        except (ValueError, TypeError): continue
        if not isinstance(row, dict) or row.get('reason') != 'compiler-message': continue
        message = row.get('message')
        if not isinstance(message, dict) or message.get('level') != 'error': continue
        code = message.get('code')
        if isinstance(code, dict) and re.fullmatch(r'E[0-9]{4}', str(code.get('code', ''))):
            print('NXRS_IDENTITY_TARGET_RUST_ERROR code=' + code['code'], file=sys.stderr)
        value = message.get('message', '')
        if isinstance(value, str) and value.startswith('linking with '):
            print('NXRS_IDENTITY_TARGET_RUST_FAILURE category=link', file=sys.stderr)
        texts = [value, *(child.get('message', '') for child in message.get('children', []) if isinstance(child, dict))]
        for text in texts:
            if not isinstance(text, str): continue
            diagnostics(text)


def record_build(out):
    sdk = json.loads((out / 'identity-sdk-build.json').read_bytes())
    native = json.loads((out / 'native-compiler-inputs.json').read_bytes())
    elf = out / 'nuttx/nuttx'
    require(sdk['scope'] == 'identity-sdk-actual-nuttx-header-build-only' and sdk['qualified'] is True)
    require(sdk['target'] == 'xtensa-esp32s3-nuttx'
            and sdk['resolved_config_sha256'] == digest(out / 'resolved.config')
            and sdk['rust_api_sha256'] == digest(ROOT / 'driver/device-identity/src/lib.rs')
            and sdk['abi_glue_sha256'] == digest(ROOT / 'driver/device-identity/src/ffi/sdk.c'))
    pins = json.loads((ROOT / 'security/device-identity-dependencies.json').read_bytes())['mbedtls']
    require(sdk['sdk'] == pins and sdk['archive_indexes_and_required_symbols_verified'] is True
            and sdk['entropy_callback_import_verified'] is True and sdk['trusted_time_callback_import_verified'] is True)
    require(sdk['certificate_fixture']['scope'] == 'public-certificate-build-fixture-only'
            and sdk['certificate_fixture']['qualified'] is False
            and sdk['certificate_fixture']['deployment_permitted'] is False)
    for key in ('hardware_entropy_qualified', 'protected_device_key_storage_qualified',
                'runtime_qualified', 'native_advisory_closure', 'physical_qualified', 'deployment_permitted'):
        require(sdk[key] is False)
    require(set(sdk['libraries']) == {'libnxrs_identity_sdk.a', 'libmbedx509.a', 'libtfpsacrypto.a'})
    for name, value in sdk['libraries'].items():
        require(value == digest(out / 'apps/staging' / name))
    require(native['qualified'] is True and native['image_sha256'] == digest(elf)
            and native['producer_sha256'] == digest(ROOT / 'tools/nuttx_native_inputs.py')
            and native['compilers']['gcc'] == sdk['compiler_sha256'])
    units = [unit for unit in native['units']
             if unit['source'].startswith('generated:identity-crypto/mbedtls-4.1.1/')]
    require(len(units) == sdk['sdk_compilation_units'] and len(units) > 0)
    require(sum(unit['source'] == 'nxrs:driver/device-identity/src/ffi/sdk.c' for unit in native['units']) == 1)
    require(native['inputs']['nxrs:driver/device-identity/src/ffi/sdk.c'] == sdk['abi_glue_sha256'])
    for key in ('retained_link_coverage_qualified', 'full_firmware_inventory_qualified',
                'native_advisory_closure', 'rust_input_closure_qualified', 'physical_qualified', 'deployment_permitted'):
        require(native[key] is False)
    # The ordinary backend's real header/libc ABI witness remains mandatory.
    abi = json.loads((out / 'abi-report.json').read_bytes())
    require(abi['probe_abi_compatible'] is True and not abi['errors'] and abi['target'] == 'xtensa-esp32s3-nuttx')
    symbols = (out / 'symbols.txt').read_text()
    for symbol in ('identity_sdk_main', 'nx_start', 'mbedtls_platform_get_entropy',
                   'psa_generate_key', 'mbedtls_x509write_csr_pem', 'psa_random_reseed',
                   'mbedtls_x509_crt_verify_with_profile', 'nxrs_di_sdk_time', 'nxrs_di_trusted_utc'):
        require(re.search(r' [TtWw] ' + re.escape(symbol) + r'$', symbols, re.M))
    require('Tensilica Xtensa Processor' in (out / 'image-layout.txt').read_text())
    return dict(sdk_build=sdk, native_compiler_report_sha256=digest(out / 'native-compiler-inputs.json'),
                native_compiled_units=native['compiled_units'], sdk_captured_units=len(units),
                actual_libc_abi_report_sha256=digest(out / 'abi-report.json'),
                application_stack_bytes=65536, elf_bytes=elf.stat().st_size,
                flash_image_bytes=(out / 'nuttx/nuttx.merged.bin').stat().st_size,
                elf_sha256=digest(elf), flash_image_sha256=digest(out / 'nuttx/nuttx.merged.bin'))


def qualify(args):
    import cryptography
    require(sys.platform == 'linux' and platform.machine() == 'x86_64'
            and sys.version_info >= (3, 12) and cryptography.__version__ == '50.0.2')
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
    tools = ROOT / 'target/qemu-tools/environment.sh'
    require(tools.is_file())
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'RUSTUP_HOME', 'CARGO_HOME', 'TMPDIR') if key in os.environ}
    environment.update(NXRS_BOOT_PYTHON=sys.executable, PYTHONNOUSERSITE='1', NUTTX_STD_COMPAT_FIXES='1')
    variants = []
    keys = []
    for index in range(2):
        print(f'Building private target identity image {index + 1}/2', flush=True)
        fixture = work / f'entropy-{index}.bin'
        with fixture.open('xb') as output: output.write(secrets.token_bytes(4096))
        authority = work / f'certificate-fixture-{index}'
        public_fixture = certificate.prepare(authority)
        out = work / f'image-{index}'
        env = dict(environment, NXRS_IDENTITY_ENTROPY_FIXTURE=str(fixture),
                   NXRS_IDENTITY_CERTIFICATE_FIXTURE=str(authority))
        command = ['bash', str(ROOT / 'tools/build-nuttx-std-app.sh'),
                   '--app-manifest', 'tests/nuttx-std/identity/Cargo.toml',
                   '--app-package', 'nxrs-identity-sdk-target', '--bin', 'identity-sdk-target',
                   '--command', 'identity_sdk', '--priority', '100', '--stack-size', '65536',
                   '--platform', 'esp32s3-identity-sdk', '--feature', 'qualification', '--out', str(out)]
        status, build_output = process.run(command, ROOT, env, work / 'logs', f'build-{index}', 1800)
        if status != 0:
            diagnostics(build_output)
            diagnostics((work / 'logs' / f'build-{index}.err').read_text(errors='replace'))
            cargo_diagnostics(out / 'cargo-messages.jsonl')
            print('NXRS_IDENTITY_TARGET_RESOURCE disk_free_bytes=' + str(shutil.disk_usage(work).free), file=sys.stderr)
        require(status == 0)
        print(f'Checking actual target build bindings {index + 1}/2', flush=True)
        build = record_build(out)
        require(public_fixture == certificate.public_bindings(authority) == build['sdk_build']['certificate_fixture'])
        execution = work / f'execution-{index}'
        # The CLI subprocess sources only the pinned tool bundle. Its inherited
        # process group lets the outer deadline retire QEMU and debugger too.
        status, _ = process.run(['bash', '-euc', 'source "$1"; exec "$2" "$3" --execute "$4" --output "$5" --certificate-fixture "$6"',
            'identity-target-execute', str(tools), sys.executable, str(Path(__file__).resolve()),
            str(out), str(execution), str(authority)], ROOT, environment, work / 'logs', f'execute-{index}', 150)
        require(status == 0 and digest(out / 'nuttx/nuttx.merged.bin') == build['flash_image_sha256'])
        results = json.loads((execution / 'results.json').read_bytes())
        require(len(results) == 1 and results[0]['success'] is True
                and results[0]['image_sha256'] == build['flash_image_sha256'])
        console = (execution / 'console-0-pass.log').read_text().replace('\r', '')
        status_lines = '\n'.join(line for line in console.splitlines() if line.startswith('NXRS_STATUS_'))
        parsed = transcript(console, status_lines, 'pass')
        public = work / f'public-{index}'; public.mkdir(mode=0o700)
        requests = parsed.pop('requests')
        for name, pem in zip(('camera-a.csr', 'camera-b.csr'), requests):
            (public / name).write_bytes(pem)
        issuer = certificate.verify_execution(authority, requests)
        require(public_fixture == certificate.public_bindings(authority))
        keys.extend(issuer['public_key_sha256'])
        variants.append(dict(build=build, issuer=issuer, controls=parsed,
                             entropy_fixture_sha256=digest(fixture), fresh_kernel=True,
                             entropy_source='private-fresh-host-os-fixture-only'))
    require(len(set(keys)) == 4)
    require(inputs == {name: digest(ROOT / name) for name in inputs}
            and checkout == git('rev-parse', 'HEAD') and not git('status', '--porcelain', '--untracked-files=normal'))
    report = dict(schema=1, scope='actual-xtensa-nuttx-identity-sdk-rust-runtime-with-host-entropy-fixtures',
        qualified=True, checkout=dict(commit=checkout, clean=True), inputs=inputs, variants=variants,
        fresh_images=2, independently_verified_csrs=4, distinct_public_keys=4,
        mcu_sdk_abi_runtime_qualified=True, returned_certificate_runtime_qualified=True,
        returned_certificates_bound=4, returned_certificate_refusals=52,
        monotonic_expiry_controls=4, poisoned_certificate_refusals=2,
        revocation_runtime_qualified=False,
        trusted_time_acquisition_qualified=False, protected_generation_qualified=False,
        uart_frame_max_bytes=128, certificate_der_max_bytes=2048,
        hardware_entropy_qualified=False,
        protected_device_key_storage_qualified=False, device_tls_qualified=False,
        native_advisory_closure=False, physical_qualified=False, fips_validation_claimed=False,
        firmware_release_qualified=False, deployment_permitted=False)
    (work / 'qualification.json').write_bytes(canonical(report) + b'\n')
    print('PASS: actual Xtensa/NuttX SDK ABI and issuer; onboard entropy/custody/TLS gates remain open')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--execute', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--certificate-fixture', type=Path)
    args = parser.parse_args(); os.umask(0o077)
    try:
        if args.execute:
            require(args.output is not None and args.work is None and args.certificate_fixture is not None)
            runner.run_cases(args.execute / 'nuttx/nuttx', args.output,
                [('pass', 'identity_sdk')], transcript, args.execute / 'nuttx/nuttx.merged.bin',
                interaction=certificate.Interaction(args.certificate_fixture))
        else:
            require(args.work is not None and args.output is None and args.certificate_fixture is None)
            qualify(args)
    except Exception:
        print('NuttX identity qualification refused; private diagnostics retained', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
