#!/usr/bin/env python3
"""Compatibility for legacy qualification; the normal build invokes Rust directly."""
from pathlib import Path
import os
import sys
ROOT=Path(__file__).resolve().parents[2]


def arguments(args):
    sys.path.insert(0,str(ROOT/'tools'))
    from platform_config import _query
    return _query('link-arguments',{},args=args)


def prune_discarded_undefined(binary,prefix,evidence):
    sys.path.insert(0,str(ROOT/'tools'))
    from platform_config import _query
    _query('link-prune',{},binary=str(binary),prefix=prefix,evidence=str(evidence))


if __name__=='__main__':
    binary=ROOT/'target/debug/nxrs-build'
    if not binary.is_file():
        raise SystemExit('Build the host tool with cargo +1.90.0 build -p nxrs-build before invoking the qualification linker')
    os.execv(binary,[str(binary),'nuttx-link',*sys.argv[1:]])
