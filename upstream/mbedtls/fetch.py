"""Compatibility entry for Rust's verified source fetcher; Cargo build scripts never fetch."""
from pathlib import Path
import subprocess
import sys

if __name__=='__main__':
    raise SystemExit(subprocess.call(['cargo','nxrs','fetch','tls',*sys.argv[1:]],
                                   cwd=Path(__file__).resolve().parents[2]))
