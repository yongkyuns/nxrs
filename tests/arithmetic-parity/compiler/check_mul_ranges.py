#!/usr/bin/env python3
"""Execute the x86 i128 range fixture against Python's exact integer product."""
import argparse
import ctypes
import hashlib
import itertools
import json
from pathlib import Path
import platform
import random
import subprocess


def oracle(a, b, bits):
    exact = a * b
    raw = exact & ((1 << bits) - 1)
    wrapped = raw - (1 << bits) if raw >> (bits - 1) else raw
    return wrapped, not -(1 << (bits - 1)) <= exact < 1 << (bits - 1)


def edges(bits):
    values = {0, (1 << bits) - 1}
    for bit in range(bits):
        values.update(v for v in ((1 << bit) - 1, 1 << bit, (1 << bit) + 1)
                      if v < 1 << bits)
    return sorted(values)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qualify(candidate, control, source, out):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('native qualification requires Linux x86_64')
    out.mkdir(parents=True, exist_ok=False)
    rng = random.Random(0x52414E4745)
    # Last two integers name the effective operand masks and output width.
    cases = {'range_64_64': (64, 64, 128),
             'range_40_88': (40, 88, 128),
             'range_65_64': (65, 64, 128),
             'legal_32_32': (32, 32, 64)}
    pairs = {}
    for name, (abits, bbits, _) in cases.items():
        pairs[name] = list(itertools.product(edges(abits), edges(bbits)))
        pairs[name] += [(rng.getrandbits(abits), rng.getrandbits(bbits))
                        for _ in range(10000)]
        # Masked pointer inputs also exercise arbitrary bits outside the masks.
        if name in ('range_40_88', 'range_65_64'):
            pairs[name] += [(rng.getrandbits(128), rng.getrandbits(
                128 if name == 'range_40_88' else 64)) for _ in range(10000)]
    words = ctypes.c_uint64 * 2
    ptr = ctypes.POINTER(ctypes.c_uint64)
    results = {}
    for label, compiler in (('candidate', candidate), ('control', control)):
        obj, shared = (out / (label + suffix) for suffix in ('.o', '.so'))
        subprocess.run([str(compiler), '-mtriple=x86_64-unknown-linux-gnu',
                        '-mattr=+lzcnt', '-O2', '-verify-machineinstrs',
                        '-relocation-model=pic', '-filetype=obj', str(source),
                        '-o', str(obj)], check=True)
        subprocess.run(['cc', '-shared', str(obj), '-o', str(shared)], check=True)
        library = ctypes.CDLL(str(shared))
        checks, overflow_cases = {}, {}
        for name, (abits, bbits, bits) in cases.items():
            fn = getattr(library, name)
            fn.restype = None
            a_pointer = name in ('range_40_88', 'range_65_64')
            b_pointer = name == 'range_40_88'
            scalar = ctypes.c_uint32 if bits == 64 else ctypes.c_uint64
            fn.argtypes = [ptr if a_pointer else scalar,
                           ptr if b_pointer else scalar,
                           ptr, ctypes.POINTER(ctypes.c_uint8)]
            checks[name] = overflow_cases[name] = 0
            for a, b in pairs[name]:
                expected, overflow = oracle(a & ((1 << abits) - 1),
                                            b & ((1 << bbits) - 1), bits)
                av, bv = words(a & ((1 << 64) - 1), a >> 64), words(
                    b & ((1 << 64) - 1), b >> 64)
                product, flag = words(0xdeadbeef, 0xdeadbeef), ctypes.c_uint8(255)
                fn(av if a_pointer else a, bv if b_pointer else b,
                   product, ctypes.byref(flag))
                raw = product[0] | (product[1] << 64 if bits == 128 else 0)
                actual = raw - (1 << bits) if raw >> (bits - 1) else raw
                if (actual, flag.value) != (expected, overflow):
                    raise ValueError((label, name, a, b, actual, flag.value,
                                      expected, overflow))
                checks[name] += 1
                overflow_cases[name] += overflow
        if not all(overflow_cases.values()):
            raise ValueError('boundary corpus did not exercise every overflow flag')
        results[label] = dict(llc_sha256=digest(compiler),
                              object_sha256=digest(obj), checks=checks,
                              overflow_cases=overflow_cases, mismatches=0)
    record = dict(architecture='x86_64', source_ir_sha256=digest(source),
                  validator_sha256=digest(Path(__file__)), results=results,
                  limitation='Defined integer inputs; native i128 execution and legal-width/rejected-range controls, not Xtensa timing or a formal poison proof')
    (out / 'host-execution.json').write_text(json.dumps(record, indent=2) + '\n')
    print('NATIVE_MUL_RANGES_PASS', sum(results['candidate']['checks'].values()),
          'checks per compiler', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('candidate', 'control', 'source', 'out'):
        parser.add_argument('--' + option, type=Path, required=True)
    args = parser.parse_args()
    qualify(*(getattr(args, name).resolve() for name in
              ('candidate', 'control', 'source', 'out')))
