#!/usr/bin/env python3
"""Compatibility entry for Rust's verified Pico radio asset preparation."""
from pathlib import Path
import subprocess
import sys

if __name__=='__main__':
    raise SystemExit(subprocess.call(['cargo','nxrs','prepare-radio',*sys.argv[1:]],
                                   cwd=Path(__file__).resolve().parents[1]))
