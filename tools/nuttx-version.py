#!/usr/bin/env python3
"""Compatibility for Rust's revision-checked NuttX release stamping."""
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from platform_config import _query


def release_version(revision):
    return _query('release-version',{},revision=revision)


def stamp(source,revision):
    return _query('stamp-version',{},source=str(Path(source).resolve()),revision=revision)


if __name__=='__main__':
    raise SystemExit(subprocess.call(['cargo','nxrs','stamp-version',*sys.argv[1:]],
                                   cwd=Path(__file__).resolve().parents[1]))
