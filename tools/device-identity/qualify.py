#!/usr/bin/env python3
"""Execute real device CSR/PSA/entropy ownership and offline issuer controls.

All source/build/authority/logs stay in a fresh private directory. The only
publishable artifact is qualification.json: hashes, counts and bounded scope.
This host SDK recipe does not qualify MCU entropy, protected key storage or TLS.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
import platform
import ssl
from pathlib import Path, PurePosixPath
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/boot'))
import qualify as process

INPUTS = ('Cargo.toml', 'Cargo.lock', 'driver/device-identity/Cargo.toml',
          'driver/device-identity/build.rs', 'driver/device-identity/src/lib.rs',
          'driver/device-identity/src/ffi/sdk.c', 'tests/device-identity/sdk.rs',
          'tests/device-identity/export_control.c', 'tools/device-identity/qualify.py',
          'tools/device-identity/crypto_config.h', 'tools/device-identity/mbedtls_config.h',
          'tools/enrollment/issuer.py', 'security/device-identity-dependencies.json', 'security/dependencies.md',
          'tests/boot/admission/sanitizer_probe.c', 'tests/host/test_device_identity_tools.py',
          '.github/workflows/device-identity.yml', 'docs/device-identity.md')
TESTS = ('actual_key_csr_and_exclusive_owner',
         'actual_psa_policy_denies_private_export_and_control_permits_it',
         'initial_entropy_failure_has_no_fallback_and_fresh_owner_recovers',
         'actual_drbg_reseed_failure_clears_request_and_stays_latched',
         'invalid_identity_is_refused_before_sdk_and_capacity_is_bounded',
         'foreign_and_late_sdk_callbacks_cannot_borrow_or_reuse_owner_storage',
         'partial_entropy_error_clears_actual_callback_output_and_estimate',
         'explicit_refresh_failure_cannot_continue_with_cached_sdk_randomness',
         'source_retirement_finishes_before_another_sdk_owner_is_admitted')


def require(value):
    if not value: raise ValueError('identity qualification refused')


def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract(archive, destination):
    # Official 4.1 releases include internal links for PQ test/example trees.
    # Validate every member/link and let data_filter enforce resolved containment.
    with tarfile.open(archive) as source:
        members = source.getmembers(); seen = set()
        require(0 < len(members) <= 10000 and sum(m.size for m in members) <= 128 * 1024 * 1024)
        for member in members:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and '..' not in path.parts
                    and path.parts and path.parts[0] == 'mbedtls-4.1.1'
                    and path.as_posix() == member.name.rstrip('/') and member.name not in seen
                    and (member.isfile() or member.isdir() or member.issym()))
            seen.add(member.name)
            if member.issym():
                link = PurePosixPath(member.linkname)
                target = PurePosixPath(posixpath.normpath(str(path.parent / link)))
                require(not link.is_absolute() and target.parts and target.parts[0] == 'mbedtls-4.1.1' and '..' not in target.parts)
        source.extractall(destination, filter='data')
    return destination / 'mbedtls-4.1.1'


def issuer_interoperability(public, directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    spec = importlib.util.spec_from_file_location('nxrs_identity_issuer', ROOT / 'tools/enrollment/issuer.py')
    issuer = importlib.util.module_from_spec(spec); spec.loader.exec_module(issuer)
    requests = [(public / name).read_bytes() for name in ['camera-a.csr', 'camera-b.csr']]
    keys = []
    for raw in requests:
        csr = x509.load_pem_x509_csr(raw)
        require(csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == 'cam_A')
        require(csr.is_signature_valid and isinstance(csr.public_key().curve, ec.SECP256R1)
                and csr.signature_hash_algorithm.name == 'sha256')
        keys.append(csr.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    require(keys[0] != keys[1])
    password = secrets.token_hex(48).encode('ascii'); now = issuer.instant()
    pin = issuer.initialize(directory, 'site_a', password, now)
    catalog = {'cameras': [{'camera_id': 'cam_a', 'gateway_id': 'gw_a', 'site_id': 'site_a'}]}
    serials = [issuer.issue(directory, pin, password, request, catalog, 'cam_a', 'camera', '127.0.0.1', now=now)
               for request in requests]
    state = issuer.parse((directory / issuer.STATE).read_bytes())
    require(len(set(serials)) == len(state['entries']) == 2)
    for index, entry in enumerate(state['entries']):
        cert = x509.load_pem_x509_certificate(entry['certificate'].encode())
        require(cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == keys[index])
        require(cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == 'cam_a')
        require('urn:nxrs:camera:site_a:cam_a' in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.UniformResourceIdentifier))
    before = digest(directory / issuer.STATE)
    csr = x509.load_pem_x509_csr(requests[0]); raw = bytearray(csr.public_bytes(serialization.Encoding.DER)); raw[-1] ^= 1
    require(not x509.load_der_x509_csr(bytes(raw)).is_signature_valid)
    encoded = base64.b64encode(raw)
    corrupted = b'-----BEGIN CERTIFICATE REQUEST-----\n' + b'\n'.join(encoded[i:i+64] for i in range(0, len(encoded), 64)) + b'\n-----END CERTIFICATE REQUEST-----\n'
    for request in [requests[0], corrupted]:
        refused = False
        try: issuer.issue(directory, pin, password, request, catalog, 'cam_a', 'camera', '127.0.0.1', now=now)
        except ValueError: refused = True
        require(refused and digest(directory / issuer.STATE) == before)
    del password
    return dict(qualified=True, independently_verified_csrs=2, unique_public_keys=True,
                actual_issuer_accepted=True, catalog_identity_enforced=True,
                repeated_key_refused=True, bad_signature_refused=True,
                csr_sha256=[hashlib.sha256(value).hexdigest() for value in requests])


def qualify(args):
    import cryptography
    require(sys.version_info >= (3, 12) and cryptography.__version__ == '50.0.2')
    pins = json.loads((ROOT / 'security/device-identity-dependencies.json').read_bytes())
    work = args.work.absolute(); require(not work.exists() and not work.is_symlink())
    work.mkdir(mode=0o700); (work / 'logs').mkdir(mode=0o700); (work / 'home').mkdir(mode=0o700)
    inputs = {name: digest(ROOT / name) for name in INPUTS}
    sdk = pins['mbedtls']; archive = work / 'sdk.tar.bz2'
    if args.archive:
        require(args.archive.is_file() and args.archive.stat().st_size <= 16 * 1024 * 1024)
        shutil.copyfile(args.archive, archive)
    else:
        with urllib.request.urlopen(sdk['url'], timeout=60) as source, archive.open('xb') as output:
            raw = source.read(16 * 1024 * 1024 + 1); require(len(raw) <= 16 * 1024 * 1024); output.write(raw)
    require(digest(archive) == sdk['sha256'])
    source = extract(archive, work)
    environment = {key: os.environ[key] for key in ['PATH', 'RUSTUP_HOME', 'CARGO_HOME'] if key in os.environ}
    environment.update(HOME=str(work / 'home'), PYTHONNOUSERSITE='1')
    # Keep rustup lookup explicit while the SDK gets a private HOME.
    environment.setdefault('RUSTUP_HOME', str(Path.home() / '.rustup'))
    environment.setdefault('CARGO_HOME', str(Path.home() / '.cargo'))
    cmake = Path(sys.executable).parent / 'cmake'
    if not cmake.exists(): cmake = Path(shutil.which('cmake') or '')
    require(cmake.is_file())
    def run(command, name, timeout=300, env=None):
        status, output = process.run([str(value) for value in command], ROOT, env or environment, work / 'logs', name, timeout)
        require(status == 0); return output
    require(run([cmake, '--version'], 'cmake-version').startswith('cmake version ' + pins['cmake'] + '\n'))
    rust_version = run(['rustc', '+1.90.0', '--version'], 'rust-version').strip()
    require(re.fullmatch(r'rustc 1[.]90[.]0 [(][a-f0-9]{7,40} [0-9-]{10}[)]', rust_version))
    variants = {}
    for name, sanitizer in [('normal', False), ('sanitized', True)]:
        directory = work / name; directory.mkdir(mode=0o700); public = directory / 'public'; public.mkdir(mode=0o700)
        build = directory / 'build'; extra = ['-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer'] if sanitizer else []
        run([cmake, '-S', source, '-B', build, '-DENABLE_TESTING=OFF', '-DENABLE_PROGRAMS=OFF', '-DGEN_FILES=OFF',
             '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
             '-DCMAKE_C_FLAGS=' + ' '.join(['-fno-lto', *extra]),
             '-DPython3_EXECUTABLE=' + sys.executable,
             '-DMBEDTLS_CONFIG_FILE=' + str(ROOT / 'tools/device-identity/mbedtls_config.h'),
             '-DTF_PSA_CRYPTO_CONFIG_FILE=' + str(ROOT / 'tools/device-identity/crypto_config.h')], name + '-configure')
        run([cmake, '--build', build, '-j', '4'], name + '-sdk-build')
        compiler_matches = re.findall(r'^CMAKE_C_COMPILER:(?:FILEPATH|STRING)=(.+)$', (build / 'CMakeCache.txt').read_text(), re.M)
        require(len(compiler_matches) == 1 and Path(compiler_matches[0]).is_file())
        compiler_version = run([compiler_matches[0], '--version'], name + '-compiler-version')
        compiler = dict(sdk_driver_sha256=digest(compiler_matches[0]),
                        sdk_version_text_sha256=hashlib.sha256(compiler_version.encode()).hexdigest(),
                        helper_driver_sha256=digest(shutil.which('cc')))
        flags = ['-std=c11', '-Wall', '-Wextra', '-Werror', *extra,
                 '-I' + str(source / 'include'), '-I' + str(source / 'tf-psa-crypto/include'),
                 '-I' + str(source / 'tf-psa-crypto/drivers/builtin/include'),
                 '-DMBEDTLS_CONFIG_FILE="' + str(ROOT / 'tools/device-identity/mbedtls_config.h') + '"',
                 '-DTF_PSA_CRYPTO_CONFIG_FILE="' + str(ROOT / 'tools/device-identity/crypto_config.h') + '"']
        for path, unit in [('driver/device-identity/src/ffi/sdk.c', 'sdk'), ('tests/device-identity/export_control.c', 'control')]:
            run(['cc', *flags, '-c', ROOT / path, '-o', directory / (unit + '.o')], name + '-' + unit)
        run(['ar', 'rcs', directory / 'libnxrs_device_identity_sdk.a', directory / 'sdk.o', directory / 'control.o'], name + '-archive')
        if sanitizer:
            probe_env = dict(environment, ASAN_OPTIONS='abort_on_error=1:detect_leaks=0', UBSAN_OPTIONS='halt_on_error=1')
            for kind, arguments, diagnostic, disable in [
                ('address', ['overflow'], 'AddressSanitizer: heap-buffer-overflow', 'undefined'),
                ('undefined', [], 'runtime error: shift exponent 32', 'address')]:
                probe = directory / ('probe-' + kind)
                run(['cc', *extra, '-fno-sanitize=' + disable, ROOT / 'tests/boot/admission/sanitizer_probe.c', '-o', probe], name + '-probe-build-' + kind)
                status, _ = process.run([str(probe), *arguments], ROOT, probe_env, work / 'logs', name + '-probe-execute-' + kind, 30)
                require(status != 0 and diagnostic in (work / 'logs' / (name + '-probe-execute-' + kind + '.err')).read_text())
        for label, addition in [
            ('builtin-source', '#define MBEDTLS_PSA_BUILTIN_GET_ENTROPY\n'),
            ('file-storage', '#define MBEDTLS_PSA_CRYPTO_STORAGE_C\n'),
            ('dynamic-slots', '#define MBEDTLS_PSA_KEY_STORE_DYNAMIC\n')]:
            candidate = directory / ('refused-' + label + '.h')
            candidate.write_bytes((ROOT / 'tools/device-identity/crypto_config.h').read_bytes() + addition.encode())
            changed = [arg for arg in flags if not arg.startswith('-DTF_PSA_CRYPTO_CONFIG_FILE=')]
            changed += ['-DTF_PSA_CRYPTO_CONFIG_FILE="' + str(candidate) + '"']
            status, _ = process.run(['cc', *changed, '-c', str(ROOT / 'driver/device-identity/src/ffi/sdk.c'),
                '-o', str(directory / ('refused-' + label + '.o'))], ROOT, environment, work / 'logs', name + '-config-' + label, 60)
            require(status != 0 and 'Unsupported identity entropy, key storage or concurrency configuration' in
                (work / 'logs' / (name + '-config-' + label + '.err')).read_text())
        link = ['-Lnative=' + str(directory), '-Lnative=' + str(build / 'library'), '-Lnative=' + str(build / 'tf-psa-crypto/core'),
                '-lstatic=nxrs_device_identity_sdk', '-lstatic=mbedx509', '-lstatic=tfpsacrypto']
        if sanitizer: link += ['-C', 'default-linker-libraries=yes', '-C', 'link-arg=-fsanitize=address,undefined']
        run(['rustc', '+1.90.0', '--edition=2021', '--test', '--cfg', 'nxrs_device_identity_qualification',
             '--check-cfg', 'cfg(nxrs_device_identity_qualification)', '--check-cfg', 'cfg(test)',
             ROOT / 'driver/device-identity/src/lib.rs', '-o', directory / 'sdk-tests', *link], name + '-rust')
        env = dict(environment, NXRS_IDENTITY_PUBLIC_FIXTURE=str(public))
        if sanitizer: env.update(ASAN_OPTIONS='abort_on_error=1:detect_leaks=' + ('0' if sys.platform == 'darwin' else '1'), UBSAN_OPTIONS='halt_on_error=1')
        text = run([directory / 'sdk-tests', '--test-threads=1', '--nocapture'], name + '-execution', 180, env)
        for test in TESTS: require(re.search(r'^test sdk_tests::' + test + r' \.\.\. ok$', text, re.M))
        require(re.search(r'^test result: ok\. 9 passed; 0 failed; 0 ignored;', text, re.M))
        authority = directory / 'issuer'
        interoperability = issuer_interoperability(public, authority)
        variants[name] = dict(qualified=True, required_native_tests=list(TESTS), native_c_asan_ubsan=sanitizer, compiled_unsafe_config_refusals=3,
            rust_memory_sanitizer_qualified=False, issuer=interoperability,
            sdk_compilation_units=len(json.loads((build / 'compile_commands.json').read_bytes())), compiler=compiler,
            artifacts={p.name: digest(p) for p in [directory / 'sdk-tests', directory / 'libnxrs_device_identity_sdk.a',
                       build / 'library/libmbedx509.a', build / 'tf-psa-crypto/core/libtfpsacrypto.a']})
        print('Actual device identity SDK/issuer qualification passed: ' + name, flush=True)
    # Compile the actual public type: accidental Send/Sync admission must fail
    # before linking, while the same real dependency admits ordinary type use.
    library = work / 'libdevice_identity.rlib'
    run(['rustc', '+1.90.0', '--edition=2021', '--crate-type=rlib', '--crate-name=device_identity',
         ROOT / 'driver/device-identity/src/lib.rs', '-o', library], 'ownership-library')
    positive = work / 'ownership-positive.rs'; positive.write_text("fn main(){let _:Option<device_identity::Identity<'static>>=None;}\n")
    run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=identity_ownership_positive', positive, '--extern',
         'device_identity=' + str(library), '-o', work / 'positive.rmeta'], 'ownership-positive')
    for bound in ['Send', 'Sync']:
        probe = work / ('ownership-' + bound.lower() + '.rs')
        probe.write_text("fn require_bound<T:" + bound + ">(){} fn main(){require_bound::<device_identity::Identity<'static>>();}\n")
        status, _ = process.run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=identity_ownership_refusal', str(probe), '--extern',
            'device_identity=' + str(library), '-o', str(work / 'refused.rmeta')], ROOT, environment, work / 'logs', 'ownership-' + bound.lower(), 60)
        require(status != 0 and 'Identity' in (work / 'logs' / ('ownership-' + bound.lower() + '.err')).read_text())
    require(inputs == {name: digest(ROOT / name) for name in INPUTS})
    head = run(['git', 'rev-parse', 'HEAD'], 'repository-head').strip()
    dirty = bool(run(['git', 'status', '--porcelain'], 'repository-status').strip())
    from cryptography.hazmat.backends import default_backend
    runtime = dict(python=sys.version.split()[0], python_ssl=ssl.OPENSSL_VERSION,
                   cryptography=cryptography.__version__, cryptography_ssl=default_backend().openssl_version_text(),
                   host_os=platform.system(), host_machine=platform.machine())
    report = dict(schema=1, scope='actual-volatile-device-identity-host-sdk-and-issuer', qualified=True,
        repository_head=head, repository_has_changes=dirty, inputs=inputs, sdk=sdk, variants=variants,
        runtime=runtime, rust_version=rust_version, native_sanitizer_refusal_controls=True, send_and_sync_refused=True,
        hardware_entropy_qualified=False, protected_device_key_storage_qualified=False,
        mcu_abi_qualified=False, device_tls_qualified=False, native_advisory_closure=False,
        trusted_release_attestation=False, physical_qualified=False, fips_validated=False, deployment_permitted=False)
    with (work / 'qualification.json').open('xb') as stream: stream.write(canonical(report) + b'\n')
    print('Host identity qualification passed; MCU entropy/custody/TLS/physical gates remain open')


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--archive', type=Path); args = parser.parse_args()
    os.umask(0o077)
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try: qualify(args)
    except Exception as error:
        import traceback
        frames = [frame for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).resolve() == Path(__file__).resolve()]
        if frames: print('Device identity qualification refused at qualify.py:' + str(frames[-2 if len(frames)>1 and frames[-1].name=='require' else -1].lineno), file=sys.stderr)
        print('Device identity qualification failed; private stage logs retained', file=sys.stderr); return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
