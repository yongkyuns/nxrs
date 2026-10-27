"""Separate host SDK allocator-site fault builds; production configs stay separate."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys

TESTS = ('native_allocator_refusal_and_pairing_are_real',
         'native_identity_and_csr_refusals_retire_sources_and_recover',
         'native_certificate_and_crl_refusals_preserve_trust',
         'native_tls_constructor_refusals_retire_io_before_lease',
         'native_tls_handshake_refusals_close_without_media_and_recover',
         'native_tls_application_io_refusals_preserve_plaintext_boundaries')
OPERATIONS = ('identity', 'csr', 'certificate', 'crl', 'tls-constructor', 'tls-handshake', 'tls-read', 'tls-write')


def require(value):
    if not value:
        raise ValueError('native allocator qualification refused')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_counts(directory):
    result = {}
    for operation in OPERATIONS:
        with (directory / ('native-' + operation + '.counts')).open('rb') as stream:
            raw = stream.read(97)
        require(len(raw) <= 96 and re.fullmatch(rb'[0-9]+(?: [0-9]+){5}\n', raw))
        calls, sites, failed, continued, attempts, recovered = map(int, raw.split())
        zero_io = operation in ('tls-read', 'tls-write') and calls == sites == failed == continued == attempts == recovered == 0
        require(zero_io or (1 <= sites <= 256 and sites <= calls <= 10_000_000))
        require(0 <= failed <= sites and 0 <= continued <= sites and failed + continued == sites)
        require(sites <= attempts <= sites * 8 and recovered == sites)
        result[operation] = dict(baseline_calls=calls, observed_sites=sites,
                                 refused_and_failed=failed, refused_and_valid=continued,
                                 attempted_fresh_cases=attempts, fresh_recovery_cases=recovered)
    return result


def qualify(root, source, work, environment, cmake, run, baseline_tests, interoperability, run_status, camera):
    variants = {}
    for name, sanitizer in [('normal', False), ('sanitized', True)]:
        label = 'allocator-' + name
        directory = work / label
        directory.mkdir(mode=0o700)
        public = directory / 'public'
        public.mkdir(mode=0o700)
        fixtures = directory / 'certificate-fixtures'
        fixtures.mkdir(mode=0o700)
        overlay = directory / 'crypto-config.h'
        overlay.write_text('#include ' + json.dumps(str(root / 'tools/device-identity/crypto_config.h')) + '\n'
                           '#include ' + json.dumps(str(root / 'tests/device-identity/native_allocator.h')) + '\n'
                           '#define MBEDTLS_PLATFORM_MEMORY\n'
                           '#define MBEDTLS_PLATFORM_CALLOC_MACRO(n,s) nxrs_test_di_calloc_at((n),(s),__FILE__,__LINE__)\n'
                           '#define MBEDTLS_PLATFORM_FREE_MACRO nxrs_test_di_free\n')
        build = directory / 'build'
        extra = ['-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer'] if sanitizer else []
        run([cmake, '-S', source, '-B', build, '-DENABLE_TESTING=OFF', '-DENABLE_PROGRAMS=OFF',
             '-DGEN_FILES=OFF', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
             '-DCMAKE_C_FLAGS=' + shlex.join(['-fno-lto', '-I' + str(root / 'driver/device-identity/src/ffi'), *extra]),
             '-DPython3_EXECUTABLE=' + sys.executable,
             '-DMBEDTLS_CONFIG_FILE=' + str(root / 'tools/device-identity/mbedtls_config.h'),
             '-DTF_PSA_CRYPTO_CONFIG_FILE=' + str(overlay)], label + '-configure')
        run([cmake, '--build', build, '-j', '4'], label + '-sdk-build')
        flags = ['-std=c11', '-Wall', '-Wextra', '-Werror', *extra,
                 '-I' + str(root / 'driver/device-identity/src/ffi'), '-I' + str(source / 'include'),
                 '-I' + str(source / 'tf-psa-crypto/include'),
                 '-I' + str(source / 'tf-psa-crypto/drivers/builtin/include'),
                 '-DMBEDTLS_CONFIG_FILE="' + str(root / 'tools/device-identity/mbedtls_config.h') + '"',
                 '-DTF_PSA_CRYPTO_CONFIG_FILE="' + str(overlay) + '"']
        for path, unit in [('driver/device-identity/src/ffi/sdk.c', 'sdk'),
                           ('tests/device-identity/export_control.c', 'control')]:
            run(['cc', *flags, '-c', root / path, '-o', directory / (unit + '.o')], label + '-' + unit)
        run(['ar', 'rcs', directory / 'libnxrs_device_identity_sdk.a', directory / 'sdk.o', directory / 'control.o'],
            label + '-archive')
        arguments = ['rustc', '+1.90.0', '--edition=2021', '-Dwarnings', '--test',
                     '--cfg', 'nxrs_device_identity_qualification',
                     '--cfg', 'nxrs_device_identity_native_allocator_qualification',
                     '--check-cfg', 'cfg(nxrs_device_identity_qualification)',
                     '--check-cfg', 'cfg(nxrs_device_identity_native_allocator_qualification)',
                     '--check-cfg', 'cfg(test)', root / 'driver/device-identity/src/lib.rs',
                     '-o', directory / 'sdk-tests', '-Ldependency=' + str(camera['directory'])]
        for crate in ['camera', 'stream_service', 'camera_stream']:
            arguments += ['--extern', crate + '=' + str(camera['libraries'][crate])]
        arguments += ['-Lnative=' + str(directory), '-Lnative=' + str(build / 'library'),
                      '-Lnative=' + str(build / 'tf-psa-crypto/core'), '-lstatic=nxrs_device_identity_sdk',
                      '-lstatic=mbedtls', '-lstatic=mbedx509', '-lstatic=tfpsacrypto']
        if sanitizer:
            arguments += ['-C', 'default-linker-libraries=yes', '-C', 'link-arg=-fsanitize=address,undefined']
        run(arguments, label + '-rust')
        env = dict(environment, NXRS_IDENTITY_PUBLIC_FIXTURE=str(public),
                   NXRS_IDENTITY_CERTIFICATE_FIXTURE=str(fixtures),
                   NXRS_IDENTITY_QUALIFICATION_PYTHON=sys.executable)
        if sanitizer:
            env.update(ASAN_OPTIONS='abort_on_error=1:detect_leaks=' + ('0' if sys.platform == 'darwin' else '1'),
                       UBSAN_OPTIONS='halt_on_error=1')
        text = run([directory / 'sdk-tests', '--test-threads=1', '--nocapture'], label + '-execution', 300, env)
        tests = [*baseline_tests, *('native_allocation::' + name for name in TESTS)]
        for test in tests:
            require(re.search(r'^test sdk_tests::' + re.escape(test) + r' \.\.\. ok$', text, re.M))
        require(re.search(r'^test result: ok\. ' + str(len(tests)) + r' passed; 0 failed; 0 ignored;', text, re.M))
        counts = read_counts(fixtures)
        negative = dict(performed=False, uninstrumented_sdk_refused=False)
        if not sanitizer:
            # The same Rust witness must reject an uninstrumented SDK instead
            # of treating its absence of observed allocation sites as a pass.
            binary = directory / 'sdk-tests-without-hooks'
            negative_args = [str(value) for value in arguments]
            negative_args[negative_args.index(str(directory / 'sdk-tests'))] = str(binary)
            for original, replacement in [(directory, work / 'normal'),
                                          (build / 'library', work / 'normal/build/library'),
                                          (build / 'tf-psa-crypto/core', work / 'normal/build/tf-psa-crypto/core')]:
                negative_args[negative_args.index('-Lnative=' + str(original))] = '-Lnative=' + str(replacement)
            run(negative_args, label + '-uninstrumented-rust')
            negative_env = dict(env)
            negative_fixtures = directory / 'uninstrumented-fixtures'
            negative_fixtures.mkdir(mode=0o700)
            negative_env['NXRS_IDENTITY_CERTIFICATE_FIXTURE'] = str(negative_fixtures)
            status, output = run_status([binary, '--test-threads=1',
                'native_allocation::native_identity_and_csr_refusals_retire_sources_and_recover'],
                label + '-uninstrumented-refusal', 60, negative_env)
            require(status == 101 and 'native_identity_and_csr_refusals_retire_sources_and_recover ... FAILED' in output
                    and 'sites.is_empty()' in output)
            negative = dict(performed=True, uninstrumented_sdk_refused=True)
        issuer = interoperability(public, directory / 'issuer')
        artifacts = {p.name: digest(p) for p in [directory / 'sdk-tests', directory / 'libnxrs_device_identity_sdk.a',
                     build / 'library/libmbedtls.a', build / 'library/libmbedx509.a',
                     build / 'tf-psa-crypto/core/libtfpsacrypto.a']}
        variants[name] = dict(qualified=True, scope='host-sdk-test-allocator-observed-callsite-failure-controls',
            required_native_tests=tests, native_c_asan_ubsan=sanitizer, rust_memory_sanitizer_qualified=False,
            production_allocator_configuration_qualified=False, physical_qualified=False,
            whole_device_exhaustion_qualified=False, deployment_permitted=False,
            allocator_overlay_sha256=digest(overlay), allocator_header_sha256=digest(root / 'tests/device-identity/native_allocator.h'),
            base_crypto_config_sha256=digest(root / 'tools/device-identity/crypto_config.h'),
            base_tls_config_sha256=digest(root / 'tools/device-identity/mbedtls_config.h'),
            sdk_compile_commands_sha256=digest(build / 'compile_commands.json'),
            sdk_compilation_units=len(json.loads((build / 'compile_commands.json').read_bytes())),
            observed_callsite_cases=counts, artifacts=artifacts, issuer=issuer,
            whole_owner_allocation_balance_verified=True, negative_control=negative)
        print('Separate host SDK allocator-site controls passed: ' + name, flush=True)
    return variants
