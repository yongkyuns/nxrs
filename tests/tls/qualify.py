"""Actual host SDK, native sanitizer and independent CSR controls; no device claim."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
FLAGS = ['-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer']


def qualify(platform, archive):
    with tempfile.TemporaryDirectory(prefix='nxrs-tls-') as temporary:
        work = Path(temporary)
        environment = {**os.environ, 'NXRS_PLATFORM': platform+'-tls',
            'NXRS_TLS_ARCHIVE': str(archive.resolve()), 'NXRS_TLS_PUBLIC_DIR': str(work),
            'ASAN_OPTIONS': 'abort_on_error=1:detect_leaks=0', 'UBSAN_OPTIONS': 'halt_on_error=1'}
        for name, arguments, diagnostic, disabled in [
            ('address', ['overflow'], b'AddressSanitizer: heap-buffer-overflow', 'undefined'),
            ('undefined', [], b'runtime error: shift exponent 32', 'address')]:
            binary = work/name
            subprocess.run([os.environ.get('CC', 'cc'), *FLAGS, '-fno-sanitize='+disabled,
                str(ROOT/'tests/tls/ffi/sanitizer-probe.c'), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary), *arguments], env=environment, capture_output=True, timeout=10)
            if result.returncode == 0 or diagnostic not in result.stderr:
                raise ValueError('Native sanitizer positive control failed')
        # Exercise the short SDK enum representation used by ARM GCC too.
        environment['CFLAGS'] = shlex.join([*shlex.split(environment.get('CFLAGS', '')), '-fshort-enums', *FLAGS])
        # Rust normally suppresses the compiler driver's default libraries.
        # Let that driver select its matching sanitizer runtimes for this test.
        environment['RUSTFLAGS'] = environment.get('RUSTFLAGS', '')+' -C default-linker-libraries=yes -C link-arg=-fsanitize=address,undefined'
        subprocess.run(['cargo', 'test', '--locked', '-p', 'nxrs-tls', '--features', 'probe'],
                       cwd=ROOT, env=environment, check=True, timeout=180)
        keys = []
        for name in ['first.csr', 'second.csr']:
            path = work/name
            subprocess.run(['openssl', 'req', '-in', str(path), '-noout', '-verify'],
                           check=True, capture_output=True, timeout=10)
            keys.append(subprocess.check_output(['openssl', 'req', '-in', str(path), '-noout', '-pubkey'], timeout=10))
        if keys[0] == keys[1]:
            raise ValueError('Fresh SDK owners reused a public key')
    print('PASS: host SDK controls, native sanitizers and independent CSR signatures/unique public keys')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=['linux', 'macos'])
    parser.add_argument('--archive', type=Path, default=ROOT/'target/mbedtls-4.2.0.tar.bz2')
    args = parser.parse_args()
    qualify(args.platform, args.archive)
