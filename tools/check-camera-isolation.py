#!/usr/bin/env python3
"""Camera qualification entry point; shared platform/build audit implementation."""
import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('hal_isolation', ROOT / 'tools/check-hal-isolation.py')
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)
packages, check, self_test = EVIDENCE.packages, EVIDENCE.check, EVIDENCE.self_test

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--out', type=Path, default=ROOT / 'target/camera-isolation')
    args = parser.parse_args()
    self_test() if args.self_test else EVIDENCE.run(args.out, ['camera'])
