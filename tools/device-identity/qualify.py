#!/usr/bin/env python3
"""Execute real device CSR/PSA/entropy ownership and offline issuer controls.

All source/build/authority/logs stay in a fresh private directory. The only
publishable artifact is qualification.json: hashes, counts and bounded scope.
This host SDK recipe does not qualify MCU entropy, protected key storage or MCU
TLS execution/resources. Actual host TLS controls are reported separately.
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
import shlex
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
          'driver/device-identity/src/certificate.rs', 'driver/device-identity/src/certificate/revocation.rs',
          'driver/device-identity/src/certificate/revocation/tls.rs', 'tests/device-identity/certificate_fixture.py', 'tests/device-identity/tls_fixture.py',
          'driver/device-identity/src/ffi/sdk.c', 'driver/device-identity/src/ffi/trusted_time.h',
          'tests/device-identity/sdk.rs',
          'tests/device-identity/export_control.c', 'tests/device-identity/frame.jpg',
          'hal/camera/src/api.rs', 'service/stream/src/lib.rs', 'service/stream/src/server.rs',
          'service/stream/tests/server.rs', 'service/stream/README.md',
          'service/stream/src/worker.rs', 'service/stream/tests/worker.rs',
          'service/camera/src/lib.rs', 'app/camera-stream/src/lib.rs',
          'tests/device-identity/camera_source.rs', 'gateway/tests/device_tls_camera.py',
          'gateway/tests/rust_camera.py', 'gateway/tests/ingest_reference.py',
          'gateway/tests/ingest_diagnostics.py', 'gateway/tests/test_ingest_diagnostics.py',
          '.github/workflows/monitoring-ingest.yml', 'tools/device-identity/qualify.py',
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
         'source_retirement_finishes_before_another_sdk_owner_is_admitted',
         'unsupported_sdk_entropy_request_closes_the_owner_permanently',
         'returned_certificate_binds_actual_owned_key_scope_issuer_and_time',
         'bound_certificate_expires_monotonically_and_closes_after_entropy_failure',
         'authenticated_crl_updates_invalidate_bindings_and_refuse_rollback',
         'malformed_and_unauthenticated_crls_cannot_mutate_accepted_state',
         'crl_expiry_equivocation_and_trust_regression_fail_closed',
         'actual_tls_server_accepts_scoped_client_in_tls12_and_tls13',
         'actual_tls_server_refuses_wrong_peer_roles_scope_dates_and_issuers',
         'actual_tls_server_retires_active_sessions_after_revocation_entropy_loss_and_expiry',
         'actual_tls_server_bounds_deadlines_cancellation_and_nonblocking_write_retries',
         'actual_tls_callbacks_refuse_foreign_retired_and_reused_contexts_without_dereference',
         'actual_tls_plaintext_is_cleared_on_post_read_cancellation_and_partial_record_deadline',
         'actual_tls_constructor_retires_io_before_lease_and_rejects_fresh_stale_time',
         'actual_current_binding_expires_after_a_fresher_trusted_clock_observation',
         'actual_tls_stream_reuses_http_framing_and_reconnects_without_cleartext',
         'actual_tls_refuses_legacy_protocol_ciphers_and_malformed_wire_then_recovers',
         'actual_tls_worker_preserves_capture_and_native_retirement_during_held_entropy')


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
                csr_sha256=[hashlib.sha256(value).hexdigest() for value in requests],
                public_key_sha256=[hashlib.sha256(value).hexdigest() for value in keys])


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
    # Compile the actual portable camera contract and production HTTP/MJPEG
    # implementation, not an alternative fixture writer or TLS proxy.
    for crate, source_path, dependencies in [
        ('camera', 'hal/camera/src/api.rs', []),
        ('camera_service', 'service/camera/src/lib.rs', ['camera']),
        ('stream_service', 'service/stream/src/lib.rs', ['camera']),
        ('camera_stream', 'app/camera-stream/src/lib.rs', ['camera','camera_service','stream_service'])]:
        arguments = ['rustc', '+1.90.0', '--edition=2021', '-Dwarnings', '--crate-type=rlib',
                     '--crate-name=' + crate, ROOT / source_path, '-Ldependency=' + str(work),
                     '-o', work / ('lib' + crate + '.rlib')]
        for dependency in dependencies:
            arguments += ['--extern', dependency + '=' + str(work / ('lib' + dependency + '.rlib'))]
        run(arguments, 'stream-library-' + crate)
    variants = {}
    for name, sanitizer in [('normal', False), ('sanitized', True)]:
        directory = work / name; directory.mkdir(mode=0o700); public = directory / 'public'; public.mkdir(mode=0o700)
        build = directory / 'build'; extra = ['-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer'] if sanitizer else []
        run([cmake, '-S', source, '-B', build, '-DENABLE_TESTING=OFF', '-DENABLE_PROGRAMS=OFF', '-DGEN_FILES=OFF',
             '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
             '-DCMAKE_C_FLAGS=' + shlex.join(['-fno-lto', '-I' + str(ROOT / 'driver/device-identity/src/ffi'), *extra]),
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
                 '-I' + str(ROOT / 'driver/device-identity/src/ffi'),
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
            ('dynamic-slots', '#define MBEDTLS_PSA_KEY_STORE_DYNAMIC\n'),
            ('ambient-clock', '#undef MBEDTLS_PLATFORM_TIME_MACRO\n')]:
            candidate = directory / ('refused-' + label + '.h')
            candidate.write_bytes((ROOT / 'tools/device-identity/crypto_config.h').read_bytes() + addition.encode())
            changed = [arg for arg in flags if not arg.startswith('-DTF_PSA_CRYPTO_CONFIG_FILE=')]
            changed += ['-DTF_PSA_CRYPTO_CONFIG_FILE="' + str(candidate) + '"']
            status, _ = process.run(['cc', *changed, '-c', str(ROOT / 'driver/device-identity/src/ffi/sdk.c'),
                '-o', str(directory / ('refused-' + label + '.o'))], ROOT, environment, work / 'logs', name + '-config-' + label, 60)
            require(status != 0 and 'Unsupported identity entropy, key storage or concurrency configuration' in
                (work / 'logs' / (name + '-config-' + label + '.err')).read_text())
        tls_refusals = [
            ('client-role', '#define MBEDTLS_SSL_CLI_C\n'),
            ('session-tickets', '#define MBEDTLS_SSL_SESSION_TICKETS\n'),
            ('ticket-module', '#define MBEDTLS_SSL_SESSION_TICKETS\n#define MBEDTLS_SSL_TICKET_C\n'),
            ('session-cache', '#define MBEDTLS_SSL_CACHE_C\n'),
            ('context-export', '#define MBEDTLS_SSL_CONTEXT_SERIALIZATION\n'),
            ('renegotiation', '#define MBEDTLS_SSL_RENEGOTIATION\n'),
            ('datagrams', '#define MBEDTLS_SSL_PROTO_DTLS\n'),
            ('early-data', '#define MBEDTLS_SSL_EARLY_DATA\n'),
            ('tls13-psk', '#define MBEDTLS_SSL_TLS1_3_KEY_EXCHANGE_MODE_PSK_ENABLED\n'),
            ('tls13-psk-ephemeral', '#define MBEDTLS_SSL_TLS1_3_KEY_EXCHANGE_MODE_PSK_EPHEMERAL_ENABLED\n'),
            ('discard-peer', '#undef MBEDTLS_SSL_KEEP_PEER_CERTIFICATE\n'),
            ('no-tls13', '#undef MBEDTLS_SSL_PROTO_TLS1_3\n'),
            ('no-ems', '#undef MBEDTLS_SSL_EXTENDED_MASTER_SECRET\n'),
            ('no-tls13-ephemeral', '#undef MBEDTLS_SSL_TLS1_3_KEY_EXCHANGE_MODE_EPHEMERAL_ENABLED\n'),
            ('no-ecdhe-ecdsa', '#undef MBEDTLS_KEY_EXCHANGE_ECDHE_ECDSA_ENABLED\n'),
            ('input-bound', '#undef MBEDTLS_SSL_IN_CONTENT_LEN\n#define MBEDTLS_SSL_IN_CONTENT_LEN 8192\n'),
            ('output-bound', '#undef MBEDTLS_SSL_OUT_CONTENT_LEN\n#define MBEDTLS_SSL_OUT_CONTENT_LEN 8192\n')]
        for label, addition in tls_refusals:
            candidate = directory / ('refused-tls-' + label + '.h')
            candidate.write_bytes((ROOT / 'tools/device-identity/mbedtls_config.h').read_bytes() + addition.encode())
            changed = [arg for arg in flags if not arg.startswith('-DMBEDTLS_CONFIG_FILE=')]
            changed += ['-DMBEDTLS_CONFIG_FILE="' + str(candidate) + '"']
            status, _ = process.run(['cc', *changed, '-c', str(ROOT / 'driver/device-identity/src/ffi/sdk.c'),
                '-o', str(directory / ('refused-tls-' + label + '.o'))], ROOT, environment,
                work / 'logs', name + '-tls-config-' + label, 60)
            diagnostic = {'input-bound': 'TLS input bound', 'output-bound': 'TLS output bound'}.get(
                label, 'Unsupported device TLS role, protocol or resumption configuration')
            require(status != 0 and diagnostic in (work / 'logs' / (name + '-tls-config-' + label + '.err')).read_text())
        link = ['-Lnative=' + str(directory), '-Lnative=' + str(build / 'library'), '-Lnative=' + str(build / 'tf-psa-crypto/core'),
                '-lstatic=nxrs_device_identity_sdk', '-lstatic=mbedtls', '-lstatic=mbedx509', '-lstatic=tfpsacrypto']
        if sanitizer: link += ['-C', 'default-linker-libraries=yes', '-C', 'link-arg=-fsanitize=address,undefined']
        run(['rustc', '+1.90.0', '--edition=2021', '--test', '--cfg', 'nxrs_device_identity_qualification',
             '--check-cfg', 'cfg(nxrs_device_identity_qualification)', '--check-cfg', 'cfg(test)',
             ROOT / 'driver/device-identity/src/lib.rs', '-o', directory / 'sdk-tests',
             '-Ldependency=' + str(work), '--extern', 'camera=' + str(work / 'libcamera.rlib'),
             '--extern', 'stream_service=' + str(work / 'libstream_service.rlib'),
             '--extern', 'camera_stream=' + str(work / 'libcamera_stream.rlib'), *link], name + '-rust')
        certificate_fixtures = directory / 'certificate-fixtures'; certificate_fixtures.mkdir(mode=0o700)
        env = dict(environment, NXRS_IDENTITY_PUBLIC_FIXTURE=str(public),
                   NXRS_IDENTITY_CERTIFICATE_FIXTURE=str(certificate_fixtures),
                   NXRS_IDENTITY_QUALIFICATION_PYTHON=sys.executable)
        if sanitizer: env.update(ASAN_OPTIONS='abort_on_error=1:detect_leaks=' + ('0' if sys.platform == 'darwin' else '1'), UBSAN_OPTIONS='halt_on_error=1')
        text = run([directory / 'sdk-tests', '--test-threads=1', '--nocapture'], name + '-execution', 180, env)
        for test in TESTS: require(re.search(r'^test sdk_tests::' + test + r' \.\.\. ok$', text, re.M))
        require(re.search(r'^test result: ok\. ' + str(len(TESTS)) + r' passed; 0 failed; 0 ignored;', text, re.M))
        authority = directory / 'issuer'
        interoperability = issuer_interoperability(public, authority)
        variants[name] = dict(qualified=True, required_native_tests=list(TESTS), native_c_asan_ubsan=sanitizer, compiled_unsafe_config_refusals=4, compiled_unsafe_tls_config_refusals=len(tls_refusals),
            rust_memory_sanitizer_qualified=False, issuer=interoperability,
            sdk_compilation_units=len(json.loads((build / 'compile_commands.json').read_bytes())), compiler=compiler,
            artifacts={p.name: digest(p) for p in [directory / 'sdk-tests', directory / 'libnxrs_device_identity_sdk.a',
                       build / 'library/libmbedtls.a', build / 'library/libmbedx509.a', build / 'tf-psa-crypto/core/libtfpsacrypto.a']})
        print('Actual device identity SDK/issuer qualification passed: ' + name, flush=True)
    # Independent processes must not restart into repeated identity keys.
    public_keys = [value for variant in variants.values() for value in variant['issuer']['public_key_sha256']]
    require(len(public_keys) == len(set(public_keys)) == 4)
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
    # The returned certificate cannot outlive or cross-thread its live key.
    positive = work / 'binding-positive.rs'
    positive.write_text("fn main(){let _:Option<device_identity::CertificateBinding<'static,'static>>=None;}\n")
    run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=binding_positive', positive,
         '--extern', 'device_identity=' + str(library), '-o', work / 'binding-positive.rmeta'], 'binding-positive')
    for bound in ['Send', 'Sync']:
        probe = work / ('binding-' + bound.lower() + '.rs')
        probe.write_text("fn require_bound<T:" + bound + ">(){} fn main(){require_bound::<device_identity::CertificateBinding<'static,'static>>();}\n")
        status, _ = process.run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=binding_refusal', str(probe),
            '--extern', 'device_identity=' + str(library), '-o', str(work / 'binding-refused.rmeta')], ROOT, environment,
            work / 'logs', 'binding-' + bound.lower(), 60)
        require(status != 0 and 'CertificateBinding' in (work / 'logs' / ('binding-' + bound.lower() + '.err')).read_text())
    probe = work / 'binding-retirement.rs'
    probe.write_text("fn retire(identity:device_identity::Identity<'static>,leaf:&[u8],root:&[u8],policy:device_identity::CameraCertificatePolicy<'_>){let binding=identity.bind_certificate(leaf,root,policy).unwrap();drop(identity);let _=binding.certificate_der();}fn main(){}\n")
    status, _ = process.run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=binding_retirement', str(probe),
        '--extern', 'device_identity=' + str(library), '-o', str(work / 'binding-retirement.rmeta')], ROOT, environment,
        work / 'logs', 'binding-retirement', 60)
    require(status != 0 and 'E0505' in (work / 'logs/binding-retirement.err').read_text())
    # Current admission borrows both the live key and its revocation state.
    positive = work / 'current-binding-positive.rs'
    positive.write_text("fn use_binding(identity:device_identity::Identity<'static>,state:device_identity::RevocationState,leaf:&[u8],root:&[u8],crl:&[u8],policy:device_identity::CameraCertificatePolicy<'_>){let binding=identity.bind_current_certificate(leaf,root,policy,&state,crl).unwrap();let _=binding.certificate_der();}fn main(){let _:Option<device_identity::RevocationState>=None;let _:Option<device_identity::CurrentCertificateBinding<'static,'static,'static>>=None;}\n")
    run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=current_binding_positive', positive,
         '--extern', 'device_identity=' + str(library), '-o', work / 'current-binding-positive.rmeta'], 'current-binding-positive')
    for kind in ['RevocationState', "CurrentCertificateBinding<'static,'static,'static>"]:
        for bound in ['Send', 'Sync']:
            label = ('revocation-state' if kind == 'RevocationState' else 'current-binding') + '-' + bound.lower()
            probe = work / (label + '.rs')
            probe.write_text("fn require_bound<T:" + bound + ">(){} fn main(){require_bound::<device_identity::" + kind + ">();}\n")
            status, _ = process.run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=revocation_refusal', str(probe),
                '--extern', 'device_identity=' + str(library), '-o', str(work / (label + '.rmeta'))], ROOT, environment,
                work / 'logs', label, 60)
            errors = (work / 'logs' / (label + '.err')).read_text()
            require(status != 0 and 'E0277' in errors and kind.split('<')[0] in errors)
    for retired in ['identity', 'state']:
        label = 'current-binding-retire-' + retired
        probe = work / (label + '.rs')
        probe.write_text("fn retire(identity:device_identity::Identity<'static>,state:device_identity::RevocationState,leaf:&[u8],root:&[u8],crl:&[u8],policy:device_identity::CameraCertificatePolicy<'_>){let binding=identity.bind_current_certificate(leaf,root,policy,&state,crl).unwrap();drop(" + retired + ");let _=binding.certificate_der();}fn main(){}\n")
        status, _ = process.run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=current_binding_retirement', str(probe),
            '--extern', 'device_identity=' + str(library), '-o', str(work / (label + '.rmeta'))], ROOT, environment,
            work / 'logs', label, 60)
        require(status != 0 and 'E0505' in (work / 'logs' / (label + '.err')).read_text())
    # The production TLS type keeps key, revocation state and I/O on its
    # owning thread. Positive API compilation prevents unrelated errors from
    # satisfying the negative Send/Sync and borrowed-owner retirement controls.
    positive = work / 'tls-positive.rs'
    positive.write_text("use device_identity::*; use std::io::{Read,Write}; fn use_tls<'k,'e,'s>(binding:CurrentCertificateBinding<'k,'e,'s>,socket:std::net::TcpStream,root:&[u8],policy:TlsPolicy<'_>){let mut session=binding.tls_server_tcp(socket,root,policy,TlsCancellation::default()).unwrap();let _=session.poll_handshake();let _=session.read(&mut [0;32]);let _=session.write(&[1,2,3]);let _=session.remaining_validity();session.close();} fn main(){}\n")
    run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=tls_positive', positive,
         '--extern', 'device_identity=' + str(library), '-o', work / 'tls-positive.rmeta'], 'tls-positive')
    for bound in ['Send', 'Sync']:
        label = 'tls-' + bound.lower(); probe = work / (label + '.rs')
        probe.write_text("fn require_bound<T:" + bound + ">(){} fn main(){require_bound::<device_identity::TlsServerSession<'static,'static,'static,std::net::TcpStream>>();}\n")
        status, _ = process.run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=tls_refusal', str(probe),
            '--extern', 'device_identity=' + str(library), '-o', str(work / (label + '.rmeta'))], ROOT, environment,
            work / 'logs', label, 60)
        errors = (work / 'logs' / (label + '.err')).read_text()
        require(status != 0 and 'E0277' in errors and 'TlsServerSession' in errors)
    for retired in ['identity', 'state']:
        label = 'tls-retire-' + retired; probe = work / (label + '.rs')
        probe.write_text("use device_identity::*; fn retire(identity:Identity<'static>,state:RevocationState,leaf:&[u8],root:&[u8],crl:&[u8],camera:CameraCertificatePolicy<'_>,tls:TlsPolicy<'_>,socket:std::net::TcpStream){let binding=identity.bind_current_certificate(leaf,root,camera,&state,crl).unwrap();let session=binding.tls_server_tcp(socket,root,tls,TlsCancellation::default()).unwrap();drop(" + retired + ");let _=session.remaining_validity();}fn main(){}\n")
        status, _ = process.run(['rustc', '+1.90.0', '--edition=2021', '--emit=metadata', '--crate-name=tls_retirement', str(probe),
            '--extern', 'device_identity=' + str(library), '-o', str(work / (label + '.rmeta'))], ROOT, environment,
            work / 'logs', label, 60)
        require(status != 0 and 'E0505' in (work / 'logs' / (label + '.err')).read_text())
    # Public-API process endpoint for the separate full gateway/media witness.
    run(['rustc', '+1.90.0', '--edition=2021', '-Dwarnings', '--crate-name=sdk_camera_source',
         ROOT / 'tests/device-identity/camera_source.rs', '-o', work / 'sdk-camera-source',
         '-Ldependency=' + str(work), '--extern', 'device_identity=' + str(library),
         '--extern', 'camera=' + str(work / 'libcamera.rlib'),
         '--extern', 'camera_stream=' + str(work / 'libcamera_stream.rlib'),
         '--extern', 'stream_service=' + str(work / 'libstream_service.rlib'),
         '-Lnative=' + str(work / 'normal'), '-Lnative=' + str(work / 'normal/build/library'),
         '-Lnative=' + str(work / 'normal/build/tf-psa-crypto/core'),
         '-lstatic=nxrs_device_identity_sdk', '-lstatic=mbedtls', '-lstatic=mbedx509', '-lstatic=tfpsacrypto'],
         'sdk-camera-source-build')
    require(inputs == {name: digest(ROOT / name) for name in INPUTS})
    head = run(['git', 'rev-parse', 'HEAD'], 'repository-head').strip()
    dirty = bool(run(['git', 'status', '--porcelain'], 'repository-status').strip())
    from cryptography.hazmat.backends import default_backend
    runtime = dict(python=sys.version.split()[0], python_ssl=ssl.OPENSSL_VERSION,
                   cryptography=cryptography.__version__, cryptography_ssl=default_backend().openssl_version_text(),
                   host_os=platform.system(), host_machine=platform.machine())
    report = dict(schema=1, scope='actual-volatile-device-identity-host-sdk-and-issuer', qualified=True,
        repository_head=head, repository_has_changes=dirty, inputs=inputs, sdk=sdk, variants=variants,
        runtime=runtime, rust_version=rust_version, native_sanitizer_refusal_controls=True, send_and_sync_refused=True, cross_process_key_uniqueness=True,
        hardware_entropy_qualified=False, protected_device_key_storage_qualified=False,
        mcu_abi_qualified=False, returned_certificate_host_qualified=True,
        returned_certificate_send_sync_refused=True, returned_certificate_owner_retirement_refused=True,
        returned_certificate_runtime_qualified=False,
        revocation_host_qualified=True, revocation_runtime_qualified=False,
        revocation_send_sync_refused=True, revocation_owner_state_retirement_refused=True,
        trusted_time_acquisition_qualified=False, protected_generation_qualified=False,
        device_tls_host_qualified=True, device_tls_capture_worker_host_qualified=True,
        device_tls_camera_fixture_sha256=digest(work / 'sdk-camera-source'), device_tls_send_sync_refused=True, device_tls_owner_state_retirement_refused=True,
        device_tls_qualified=False, native_advisory_closure=False,
        trusted_release_attestation=False, physical_qualified=False, fips_validated=False, deployment_permitted=False)
    with (work / 'qualification.json').open('xb') as stream: stream.write(canonical(report) + b'\n')
    print('Host identity/TLS controls passed; MCU entropy/custody/TLS runtime and physical gates remain open')


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
