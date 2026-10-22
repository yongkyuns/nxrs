#!/usr/bin/env python3
"""Build the reviewed backup-test artifact; kept as the qualification entry point."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from backup_engine import main
if __name__=='__main__':main()
