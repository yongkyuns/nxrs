#!/usr/bin/env python3
"""Check retained S8 kernel tables against the hash-pinned upstream generator."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'upstream/esp-dl'))
from prepare import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, default=ROOT / 'target/pico-sdk')
    args = parser.parse_args()
    verify(args.sdk)
    sdk = args.sdk / 'esp-dl'
    spec = importlib.util.spec_from_file_location('espdl_finalize', sdk / 'cmake/compile_finalize.py')
    upstream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upstream)
    conv = upstream.load_yaml(sdk / 'spec/select/Conv.yml')
    legal = upstream.load_yaml(sdk / 'spec/kernels.yml')
    actual = json.loads((ROOT / 'upstream/esp-dl/kernels.json').read_text())
    expected = {'kernel_abi': legal['kernel_abi']}
    symbols = upstream.kernel_name_set(legal)
    for name, target in [('scalar', 'host'), ('esp32s3', 'esp32s3')]:
        rows = [list(row) for row in upstream.conv_select_rows(conv, target) if row[0] & 3 == 0]
        for row in rows:
            for symbol in row[1:]:
                if symbol is not None and symbol not in symbols:
                    raise ValueError(f'unknown upstream kernel: {symbol}')
        expected[name] = rows
    if actual != expected:
        raise ValueError('PICO dispatch differs from the pinned upstream S8 tables')
    print(f'PICO S8 dispatch matches upstream: scalar {len(actual["scalar"])} rows, '
          f'ESP32-S3 {len(actual["esp32s3"])} rows')


if __name__ == '__main__':
    main()
