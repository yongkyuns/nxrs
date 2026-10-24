#!/usr/bin/env python3
"""Build the reviewed MediaMTX artifact; never fall back to an unaudited binary."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from media_engine import main

if __name__=='__main__':main()
