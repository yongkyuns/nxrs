#!/usr/bin/env python3
"""Qualification compatibility for the Rust signature-enforcing bootloader tools."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from platform_config import _query, _cli


def sign_rom(source, output, key, root):
    source, output, key, root = (Path(p).absolute() for p in (source, output, key, root))
    return _query('rom-sign', {}, input=str(source), output=str(output), key=str(key),
                  **{'key-root':str(root)})


if __name__ == '__main__':
    arguments = ['--profile' if arg == '--platform' else arg for arg in sys.argv[1:]]
    raise SystemExit(_cli('build-loader', arguments))
