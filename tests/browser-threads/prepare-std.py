#!/usr/bin/env python3
"""Compatibility entry for Rust's private browser thread-local cleanup preparation."""
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from platform_config import _cli
if __name__=='__main__':
    if sys.argv[1:] != ['--self-test']:
        raise SystemExit(_cli('prepare-browser-std', sys.argv[1:]))
    command=['cargo','+1.90.0','test','--locked','-p','nxrs-build','web::tests']
    raise SystemExit(subprocess.call(command,cwd=ROOT))
