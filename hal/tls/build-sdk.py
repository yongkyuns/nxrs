"""Build the pinned SDK offline, with the same headers/config as its private ABI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def run(command):
    subprocess.run([str(value) for value in command], check=True, stdout=subprocess.DEVNULL)


def build(out, target):
    pin = json.loads((ROOT/'upstream/mbedtls/source.json').read_text())
    archive = Path(os.environ.get('NXRS_TLS_ARCHIVE', ROOT/'target/mbedtls-4.2.0.tar.bz2')).resolve()
    if not archive.is_file() or archive.stat().st_size != pin['size'] \
            or hashlib.sha256(archive.read_bytes()).hexdigest() != pin['sha256']:
        raise ValueError('Verified TLS source archive required; run upstream/mbedtls/fetch.py')
    print('cargo:rerun-if-changed='+str(archive))
    out.mkdir(parents=True, exist_ok=True)
    source = out/('mbedtls-'+pin['version'])
    if not source.exists():
        with tarfile.open(archive) as release:
            members = release.getmembers()
            if len(members) > 10000 or sum(m.size for m in members) > 128*1024*1024:
                raise ValueError('TLS release exceeds source bound')
            # data_filter rejects escaping paths/links and special files.
            release.extractall(out, filter='data')
    ffi = HERE/'src/sdk/ffi'
    cmake = os.environ.get('CMAKE', 'cmake')
    compiler, archiver = os.environ.get('CC', 'cc'), os.environ.get('AR', 'ar')
    # Let the final link discard unused SDK/ABI functions and static data.
    flags = [*shlex.split(os.environ.get('CFLAGS', '')),
             '-ffunction-sections', '-fdata-sections']
    cross = []
    if 'nuttx' in target:
        prefix, cpu = (('arm-none-eabi-', ['-mcpu=cortex-m33', '-mthumb', '-mfloat-abi=soft'])
                       if target.startswith('thumbv8m.main-') else
                       ('xtensa-esp32s3-elf-', ['-mlongcalls']) if target.startswith('xtensa-esp32s3-') else (None, []))
        if prefix is None:
            raise ValueError('Unsupported NuttX TLS target')
        compiler, archiver = os.environ.get('CC', prefix+'gcc'), os.environ.get('AR', prefix+'ar')
        include = Path(os.environ['NXRS_NUTTX_INCLUDE']).resolve()
        if not (include/'nuttx/config.h').is_file():
            raise ValueError('Configured NuttX target headers required')
        flags += [*cpu, '-D__NuttX__', '-I'+str(include)]
        cross = ['-DCMAKE_SYSTEM_NAME=Generic', '-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY']
    compiler, archiver = shutil.which(compiler), shutil.which(archiver)
    if compiler is None or archiver is None:
        raise ValueError('Target C compiler and archiver required')
    directory = out/'sdk'
    # Cargo reruns this only for native inputs. CMake caches its initial AR in
    # compiler metadata; discard that metadata when reconfiguring the toolchain.
    (directory/'CMakeCache.txt').unlink(missing_ok=True)
    shutil.rmtree(directory/'CMakeFiles', ignore_errors=True)
    config = [f'-DMBEDTLS_CONFIG_FILE="{ffi}/mbedtls_config.h"',
              f'-DTF_PSA_CRYPTO_CONFIG_FILE="{ffi}/crypto_config.h"']
    run([cmake, '-S', source, '-B', directory, '-DENABLE_TESTING=OFF', '-DENABLE_PROGRAMS=OFF',
         '-DGEN_FILES=OFF', '-DCMAKE_BUILD_TYPE=MinSizeRel', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
         '-DCMAKE_C_COMPILER='+compiler, '-DCMAKE_AR='+archiver,
         '-DCMAKE_C_FLAGS='+shlex.join(flags), *cross,
         '-DMBEDTLS_CONFIG_FILE='+str(ffi/'mbedtls_config.h'),
         '-DTF_PSA_CRYPTO_CONFIG_FILE='+str(ffi/'crypto_config.h')])
    run([cmake, '--build', directory, '--target', 'mbedtls', '-j', '4'])
    includes = [source/'include', source/'tf-psa-crypto/include', source/'tf-psa-crypto/drivers/builtin/include']
    run([compiler, *flags, '-std=c11', '-Wall', '-Wextra', '-Werror',
         *['-I'+str(p) for p in includes], *config, '-c', ffi/'sdk.c', '-o', out/'sdk.o'])
    native = out/'libtls_abi.a'
    native.unlink(missing_ok=True)
    objects = [out/'sdk.o']
    if os.environ.get('CARGO_FEATURE_PROBE'):
        run([compiler, *flags, '-std=c11', '-Wall', '-Wextra', '-Werror',
             *['-I'+str(p) for p in includes], *config, '-c', ROOT/'tests/tls/ffi/control.c', '-o', out/'control.o'])
        objects.append(out/'control.o')
    run([archiver, 'rcs', native, *objects])
    for path in [out, directory/'library', directory/'tf-psa-crypto/core']:
        print('cargo:rustc-link-search=native='+str(path))
    for name in ['tls_abi', 'mbedtls', 'mbedx509', 'tfpsacrypto']:
        print('cargo:rustc-link-lib=static='+name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    build(args.out, args.target)
