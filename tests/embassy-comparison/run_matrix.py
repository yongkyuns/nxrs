#!/usr/bin/env python3
"""Extend the existing rotated/restoring local matrix with Embassy cases."""
import hashlib
import json
from pathlib import Path
import sys

from measure import HERE, load

shared = load('embassy_matrix_shared', HERE.parent / 'zephyr-comparison' / 'run_matrix.py')
EMBASSY = {
    'embassy-wire-2': ('embassy', 'embassy-wire-2-v2', 'embassy', 'wire'),
    'embassy-packet-2': ('embassy', 'embassy-packet-2-v2', 'embassy', 'packet'),
    'embassy-packet-os': ('embassy', 'embassy-packet-os-v2', 'embassy', 'packet'),
    'embassy-packet-yield-2': ('embassy', 'embassy-packet-yield-2-v2', 'embassy', 'packet'),
    'embassy-baseline': ('embassy', 'embassy-baseline-2-v3', 'embassy', 'baseline'),
    'embassy-baseline-os': ('embassy', 'embassy-baseline-v2', 'embassy', 'baseline'),
}
shared.CASES.update(EMBASSY)
_original_image = shared.frozen_image
_original_command = shared.command


def frozen_image(directory, case):
    if case not in EMBASSY:
        return _original_image(directory, case)
    provenance = json.loads((directory / 'build-provenance.json').read_text())
    if provenance.get('status') != 'success' or provenance.get('failure') is not None:
        raise ValueError('incomplete Embassy build')
    hashes = provenance['artifacts']
    if not {'embassy.elf', 'embassy.bin'} <= hashes.keys():
        raise ValueError('missing Embassy artifacts')
    for name, expected in hashes.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('unsafe artifact path')
        if hashlib.sha256((directory / path).read_bytes()).hexdigest() != expected:
            raise ValueError(f'changed Embassy artifact: {name}')
    return directory / 'embassy.bin'


def command(case, directory, output, port, flasher, runs):
    if case not in EMBASSY:
        argv = _original_command(case, directory, output, port, flasher, runs)
        if shared.CASES[case][0] == 'nuttx':
            argv[1] = str(HERE / 'measure_native.py')
        return argv
    return [sys.executable, str(HERE / 'measure.py'), '--image', str(directory / 'embassy.bin'),
            '--mode', EMBASSY[case][3], '--out', str(output), '--port', port,
            '--flasher', flasher, '--runs', str(runs)]


shared.frozen_image = frozen_image
shared.command = command
CASES = shared.CASES
case_directory = shared.case_directory


if __name__ == '__main__':
    shared.main()
