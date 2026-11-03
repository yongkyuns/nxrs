#!/usr/bin/env python3
"""Compatibility for Rust's revision-checked NuttX release stamping."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from platform_config import _query, _cli


def release_version(revision):
    return _query('release-version',{},revision=revision)


def stamp(source,revision):
    return _query('stamp-version',{},source=str(Path(source).resolve()),revision=revision)


if __name__=='__main__':
    raise SystemExit(_cli('stamp-version', sys.argv[1:]))
