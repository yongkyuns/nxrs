#!/usr/bin/env python3
"""Compatibility entry for Rust's hash-pinned numerical SDK preparation."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
LEDGER=Path(__file__).with_name("source.json")
sys.path.insert(0,str(ROOT/"tools"))
from platform_config import _query, _cli

def verify(destination):
    _query("verify-inventory",{},ledger=str(LEDGER),destination=str(destination))


if __name__ == '__main__':
    raise SystemExit(_cli('fetch', ['detector', *sys.argv[1:]]))
