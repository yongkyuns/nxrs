#!/usr/bin/env python3
"""Compatibility entry for Rust's private browser thread-local cleanup preparation."""
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[2]
if __name__=='__main__':
    command=(['cargo','+1.90.0','test','--locked','-p','nxrs-build','web::tests']
             if sys.argv[1:]==['--self-test'] else ['cargo','+1.90.0','nxrs','prepare-browser-std',*sys.argv[1:]])
    raise SystemExit(subprocess.call(command,cwd=ROOT))
