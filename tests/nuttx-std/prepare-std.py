#!/usr/bin/env python3
"""Compatibility entry for Rust's private NuttX SDK preparation."""
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from platform_config import _frontend, _cli
LIBC_DIRECTORY_PATCH=ROOT/'upstream/libc/patches/0002-nuttx-directory-inode-abi.patch'
LIBC_POLL_PATCH=ROOT/'upstream/libc/patches/0001-nuttx-pollfd-abi.patch'
STD_POLL_PATCH=ROOT/'upstream/rust-std/patches/0001-native-pollfd-initialization.patch'


def apply_dependency_patch(source,patch_file,output,name):
    result=subprocess.run([str(_frontend()),'--workspace-root',str(ROOT),'patch-file','--source',str(source),'--patch',str(patch_file),
                           '--out',str(output),'--name',name],text=True,capture_output=True,timeout=120)
    if result.returncode:
        raise AssertionError('dependency patch rejected: '+result.stderr)
    return name


if __name__=='__main__':
    if sys.argv[1:]==['--self-test']:
        command=['cargo','test','--locked','-p','nxrs-build','sdk::tests']
    else:
        raise SystemExit(_cli('prepare-std', sys.argv[1:]))
    raise SystemExit(subprocess.call(command,cwd=ROOT))
