#!/usr/bin/env python3
"""Build the pinned identity SDK against the actual configured NuttX headers.

This is product build wiring, not an entropy provider. Children inherit the
backend's process group so its qualification deadline retires all compilers.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import platform_config
# Import the existing source-release validator by a unique module name. Its
# private child-run helper remains the boot qualifier, never this SDK recipe.
spec = importlib.util.spec_from_file_location('identity_release_tools', Path(__file__).with_name('qualify.py'))
release = importlib.util.module_from_spec(spec); spec.loader.exec_module(release)


def require(value):
    if not value: raise ValueError('NuttX identity SDK refused')


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(args):
    profile = platform_config.resolve(args.platform)
    require(platform_config.identity_crypto(profile))
    out = args.out.resolve(strict=True); nuttx = out / 'nuttx'
    configuration = nuttx / '.config'; require(configuration.is_file())
    config = configuration.read_text()
    required = ['CONFIG_ARCH_XTENSA=y', 'CONFIG_ARCH_CHIP_ESP32S3=y', 'CONFIG_BUILD_FLAT=y',
                'CONFIG_TLS_GLOBAL_KEYS=y', 'CONFIG_ESP32S3_QEMU_IMAGE=y']
    require(all(line in config.splitlines() for line in required))
    require(not any(line in config.splitlines() for line in
        ['CONFIG_CRYPTO_MBEDTLS=y', 'CONFIG_ESPRESSIF_WIFI=y', 'CONFIG_ESPRESSIF_BLE=y',
         'CONFIG_ESPRESSIF_ADC=y', 'CONFIG_SMP=y']))
    compiler = Path(shutil.which(str(args.compiler)) or str(args.compiler)).absolute()
    require(compiler.is_file() and args.launcher.is_file())
    directory = out / 'identity-crypto'; require(not directory.exists())
    directory.mkdir(mode=0o700); logs = directory / 'logs'; logs.mkdir(mode=0o700)
    environment = {key: os.environ[key] for key in ['PATH', 'HOME', 'TMPDIR'] if key in os.environ}
    environment['PYTHONNOUSERSITE'] = '1'
    pins = json.loads((ROOT / 'security/device-identity-dependencies.json').read_bytes())['mbedtls']
    archive = directory / 'sdk.tar.bz2'
    with urllib.request.urlopen(pins['url'], timeout=60) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    require(len(raw) <= 16 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == pins['sha256'])
    with archive.open('xb') as stream: stream.write(raw)
    source = release.extract(archive, directory)
    cmake = Path(sys.executable).parent / 'cmake'
    if not cmake.is_file(): cmake = Path(shutil.which('cmake') or '')
    require(cmake.is_file())
    version = subprocess.check_output([str(cmake), '--version'], env=environment, text=True, timeout=10)
    require(version.startswith('cmake version ' + json.loads((ROOT / 'security/device-identity-dependencies.json').read_bytes())['cmake'] + '\n'))
    def run(command, name):
        global FAILURE_STAGE, FAILURE_LOG
        FAILURE_STAGE = name; FAILURE_LOG = logs / (name + '.err')
        with (logs / (name + '.out')).open('xb') as stdout, (logs / (name + '.err')).open('xb') as stderr:
            subprocess.run([str(value) for value in command], env=environment, cwd=ROOT,
                           stdout=stdout, stderr=stderr, check=True)
    flags = ['-mlongcalls', '-mtext-section-literals', '-ffunction-sections', '-fdata-sections',
             '-fno-common', '-fno-strict-aliasing', '-fno-lto', '-D__NuttX__', '-I' + str(nuttx / 'include'),
             '-I' + str(ROOT / 'driver/device-identity/src/ffi')]
    build = directory / 'build'
    run([cmake, '-S', source, '-B', build, '-DCMAKE_SYSTEM_NAME=Generic',
         '-DCMAKE_SYSTEM_PROCESSOR=xtensa', '-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY',
         '-DCMAKE_C_COMPILER=' + str(compiler), '-DCMAKE_C_FLAGS=' + shlex.join(flags),
         '-DENABLE_TESTING=OFF', '-DENABLE_PROGRAMS=OFF', '-DGEN_FILES=OFF',
         '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_FLAGS_RELEASE=-O2 -DNDEBUG', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
         '-DPython3_EXECUTABLE=' + sys.executable,
         '-DMBEDTLS_CONFIG_FILE=' + str(ROOT / 'tools/device-identity/mbedtls_config.h'),
         '-DTF_PSA_CRYPTO_CONFIG_FILE=' + str(ROOT / 'tools/device-identity/crypto_config.h')], 'configure')
    # Configure real compiler probes before enabling capture. Only actual SDK
    # compilation receives the launcher; temporary compiler probes are not units.
    run([cmake, '-S', source, '-B', build, '-DCMAKE_C_COMPILER_LAUNCHER=' + str(args.launcher)], 'launcher')
    run([cmake, '--build', build, '-j', '4'], 'sdk')
    helper = directory / 'sdk.o'
    run([args.launcher, compiler, *flags, '-std=c11', '-I' + str(source / 'include'),
         '-I' + str(source / 'tf-psa-crypto/include'), '-I' + str(source / 'tf-psa-crypto/drivers/builtin/include'),
         '-DMBEDTLS_CONFIG_FILE="' + str(ROOT / 'tools/device-identity/mbedtls_config.h') + '"',
         '-DTF_PSA_CRYPTO_CONFIG_FILE="' + str(ROOT / 'tools/device-identity/crypto_config.h') + '"',
         '-c', ROOT / 'driver/device-identity/src/ffi/sdk.c', '-o', helper], 'helper')
    archiver = compiler.with_name(compiler.name.removesuffix('gcc') + 'ar')
    require(archiver.is_file())
    run([archiver, 'rcs', directory / 'libnxrs_identity_sdk.a', helper], 'archive')
    staging = out / 'apps/staging'; staging.mkdir(exist_ok=True)
    inputs = [directory / 'libnxrs_identity_sdk.a', build / 'library/libmbedx509.a',
              build / 'tf-psa-crypto/core/libtfpsacrypto.a']
    libraries = {}
    nm = compiler.with_name(compiler.name.removesuffix('gcc') + 'nm')
    require(nm.is_file())
    required = {'libnxrs_identity_sdk.a': ['nxrs_di_attributes_size'],
                'libmbedx509.a': ['mbedtls_x509write_csr_pem'],
                'libtfpsacrypto.a': ['psa_crypto_init', 'psa_generate_key', 'psa_random_reseed']}
    for path in inputs:
        name = 'symbols-' + path.stem
        run([nm, '--print-armap', path], name)
        symbols = (logs / (name + '.out')).read_text()
        require('Archive index:' in symbols)
        require('file format not recognized' not in (logs / (name + '.err')).read_text())
        for symbol in required[path.name]:
            require(re.search(r' [TtWw] ' + re.escape(symbol) + r'$', symbols, re.M))
        if path.name == 'libtfpsacrypto.a':
            require(re.search(r'\sU mbedtls_platform_get_entropy$', symbols, re.M))
        destination = staging / path.name; require(not destination.exists())
        shutil.copyfile(path, destination); require(digest(path) == digest(destination))
        libraries[path.name] = digest(path)
    report = dict(schema=1, scope='identity-sdk-actual-nuttx-header-build-only', qualified=True,
        target=profile['target'], sdk=pins, resolved_config_sha256=digest(configuration),
        compiler_sha256=digest(compiler), libraries=libraries,
        sdk_compilation_units=len(json.loads((build / 'compile_commands.json').read_bytes())),
        archive_indexes_and_required_symbols_verified=True, entropy_callback_import_verified=True,
        rust_api_sha256=digest(ROOT / 'driver/device-identity/src/lib.rs'),
        abi_glue_sha256=digest(ROOT / 'driver/device-identity/src/ffi/sdk.c'),
        hardware_entropy_qualified=False, protected_device_key_storage_qualified=False,
        runtime_qualified=False, native_advisory_closure=False, physical_qualified=False, deployment_permitted=False)
    with (out / 'identity-sdk-build.json').open('xb') as stream: stream.write(release.canonical(report) + b'\n')
    print('Identity SDK compiled against exact NuttX headers; runtime and hardware gates remain open')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True); parser.add_argument('--platform', required=True)
    parser.add_argument('--compiler', required=True); parser.add_argument('--launcher', type=Path, required=True)
    args = parser.parse_args(); os.umask(0o077)
    try: build(args)
    except Exception:
        stage = globals().get('FAILURE_STAGE', 'validation')
        category = 'build-refusal'; token = ''
        log = globals().get('FAILURE_LOG')
        if log and log.is_file():
            stderr = log.read_text(errors='replace')[-65536:]
            for pattern, label in [(r'fatal error: ([A-Za-z0-9_.-]{1,128}): No such file', 'missing-header'),
                                   (r'[‘\']([A-Za-z_][A-Za-z0-9_]{0,127})[’\'] undeclared', 'undeclared-identifier')]:
                match = re.search(pattern, stderr)
                if match: category = label; token = ' token=' + match[1]; break
        print('NXRS_IDENTITY_SDK_FAILURE stage=' + stage + ' category=' + category + token, file=sys.stderr)
        print('NuttX identity SDK build refused; private stage logs retained', file=sys.stderr); return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
