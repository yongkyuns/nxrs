#!/usr/bin/env python3
"""Qualification compatibility for Rust's offline factory packager. Never provisions."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from platform_config import _query, _cli


def prepare(profile,boot_build,application,rom_root,app_root,flash_key,output):
    boot_build, application, rom_root, app_root, flash_key, output = (
        Path(p).absolute() for p in (boot_build, application, rom_root, app_root, flash_key, output))
    _query('factory',profile,**{'boot-build':str(boot_build),'application':str(application),
                             'rom-root':str(rom_root),'app-root':str(app_root),
                             'flash-key':str(flash_key),'output':str(output)})


if __name__=='__main__':
    arguments=['--profile' if arg=='--platform' else arg for arg in sys.argv[1:]]
    raise SystemExit(_cli('factory', arguments))
