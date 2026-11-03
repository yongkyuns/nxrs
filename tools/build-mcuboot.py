#!/usr/bin/env python3
"""Qualification compatibility for the Rust signature-enforcing bootloader tools."""
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from platform_config import _query


def sign_rom(source, output, key, root):
    return _query('rom-sign', {}, input=str(source), output=str(output), key=str(key),
                  **{'key-root':str(root)})


if __name__ == '__main__':
    arguments = ['--profile' if arg == '--platform' else arg for arg in sys.argv[1:]]
    raise SystemExit(subprocess.call(['cargo', 'nxrs', 'build-loader', *arguments],
                                   cwd=Path(__file__).resolve().parents[1]))
