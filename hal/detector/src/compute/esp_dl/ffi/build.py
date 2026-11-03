#!/usr/bin/env python3
"""Qualification compatibility; the detector HAL owns its Rust SDK adapter."""
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[6]
sys.path.insert(0,str(ROOT/'tools'))
from platform_config import _query


def qualify_compute_context(header):
    _query('detector-context',{},include=str(header))


if __name__=='__main__':
    raise SystemExit(subprocess.call(['cargo','nxrs','detector-sdk',*sys.argv[1:]],cwd=ROOT))
