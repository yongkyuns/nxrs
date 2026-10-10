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
CASES = {**shared.CASES, **EMBASSY}


def frozen_image(directory, case, cases=None):
    if case not in EMBASSY:
        return shared.frozen_image(directory, case, cases=CASES)
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


def command(case, directory, output, port, flasher, runs, cases=None):
    if case not in EMBASSY:
        argv = shared.command(case, directory, output, port, flasher, runs, cases=CASES)
        if CASES[case][0] == 'nuttx':
            argv[1] = str(HERE / 'measure_native.py')
        return argv
    return [sys.executable, str(HERE / 'measure.py'), '--image', str(directory / 'embassy.bin'),
            '--mode', EMBASSY[case][3], '--out', str(output), '--port', port,
            '--flasher', flasher, '--runs', str(runs)]


def case_directory(root, case, cases=None):
    return shared.case_directory(root, case, cases=CASES)


if __name__ == '__main__':
    shared.main(cases=CASES, image_validator=frozen_image, command_builder=command)
