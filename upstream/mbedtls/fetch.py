"""Compatibility entry for Rust's verified source fetcher; Cargo build scripts never fetch."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
from platform_config import _cli

if __name__=='__main__':
    raise SystemExit(_cli('fetch', ['tls', *sys.argv[1:]]))
