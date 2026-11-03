#!/usr/bin/env python3
"""Compatibility entry for Rust's verified Pico radio asset preparation."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from platform_config import _cli

if __name__=='__main__':
    raise SystemExit(_cli('prepare-radio', sys.argv[1:]))
