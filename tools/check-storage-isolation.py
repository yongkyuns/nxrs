#!/usr/bin/env python3
"""Check selected storage packages and decode real output independently of Rust."""
import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('hal_isolation', ROOT / 'tools/check-hal-isolation.py')
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)


def check_records(directory):
    paths = sorted(directory.iterdir())
    assert [p.name for p in paths] == [f'{i:020}.rcam' for i in (1, 2)], 'unexpected output files'
    for sequence, value, path in zip((1, 2), (7, 9), paths):
        raw = path.read_bytes()
        # Independent wire oracle: do not reuse the Rust encoder or decoder.
        header = (b'RCAMREC1' + (2).to_bytes(2, 'little') * 2 + bytes(4)
                  + sequence.to_bytes(8, 'little') + (sequence * 10).to_bytes(8, 'little')
                  + (4).to_bytes(8, 'little'))
        assert raw == header + bytes([value]) * 4, f'wrong committed record: {path}'


def run(out):
    EVIDENCE.run(out, ['storage'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'target/storage-isolation')
    args = parser.parse_args()
    run(args.out)
