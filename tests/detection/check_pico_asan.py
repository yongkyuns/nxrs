#!/usr/bin/env python3
"""Compile the pinned scalar SDK with ASan and exercise independent ownership."""
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, default=ROOT / 'target/pico-sdk')
    args = parser.parse_args()
    compiler = shutil.which('clang++')
    if compiler is None:
        raise RuntimeError('Clang with AddressSanitizer is required')
    target = platform.machine() + ('-apple-darwin' if sys.platform == 'darwin' else '-unknown-linux-gnu')
    with tempfile.TemporaryDirectory(prefix='nxrs-pico-asan-') as directory:
        work = Path(directory)
        wrapper = work / 'asan-cxx'
        wrapper.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                           f'os.execv({compiler!r},[{compiler!r},"-fsanitize=address",'
                           '"-fno-omit-frame-pointer",*sys.argv[1:]])\n')
        wrapper.chmod(0o700)
        env = dict(os.environ, CXX=str(wrapper))
        output = work / 'sdk'
        subprocess.run([sys.executable, str(ROOT / 'hal/detector/src/platform/std/ffi/build.py'),
                        '--sdk', str(args.sdk.resolve()), '--out', str(output), '--target', target],
                       env=env, check=True)
        binary = work / 'check'
        subprocess.run([compiler, '-std=c++17', '-fsanitize=address', '-fno-omit-frame-pointer',
                        str(ROOT / 'tests/detection/pico_asan.cpp'), str(output / 'libnxrs_pico.a'),
                        '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
