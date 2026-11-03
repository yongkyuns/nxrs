#!/usr/bin/env python3
"""Qualification compatibility for the Rust image packager. No device access."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_config import _query


def image_class(platform):
    return _query('image-class', platform)


def layout(config):
    return _query('layout', {'mcuboot': config})


def sign(source, output, config, key, root, version, counter, *, class_id=None, confirm=False):
    data = {'mcuboot': config}
    if class_id is not None:
        data['image-class'] = class_id
    _query('sign', data, input=str(source), output=str(output), key=str(key),
           version=version, counter=counter, confirm=confirm, **{'key-root': str(root)})


if __name__ == '__main__':
    import subprocess
    arguments = ['--profile' if arg == '--platform' else arg for arg in sys.argv[1:]]
    raise SystemExit(subprocess.call(['cargo', 'nxrs', 'sign', *arguments], cwd=Path(__file__).resolve().parents[1]))
