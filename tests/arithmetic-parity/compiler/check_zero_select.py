#!/usr/bin/env python3
"""Execute the unmodified scalar LLVM fixture against independent expectations."""
import argparse
import ctypes
import hashlib
import itertools
import json
from pathlib import Path
import platform
import random
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_predicate_result(x, v):
    """None denotes poison selected by the source, not a testable result."""
    signed = ctypes.c_int32(v).value
    if x != 0 and signed == 0x7fffffff:
        return None
    return 0 if x == 0 or signed + 1 < 0 else x


def shift_predicate_result(x, v, amount):
    if x != 0 and amount >= 32:
        return None
    return 0 if x == 0 or (v << amount) & 0x80000000 else x


def qualify(candidate, control, source, out):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('native qualification requires Linux x86_64')
    out.mkdir(parents=True, exist_ok=False)
    values = list(range(256)) + [0xffffffff, 0x80000000, 0x7fffffff,
                               0xaaaaaaaa, 0x55555555]
    pairs = list(itertools.product(values, repeat=2))
    rng = random.Random(0x5A45524F)
    pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(10000)]
    edges = {0, 1, -1}
    for bit in range(64):
        for sign in (-1, 1):
            for offset in (-1, 0, 1):
                v = sign * (1 << bit) + offset
                if -(1 << 63) <= v < 1 << 63:
                    edges.add(v)
    mul_pairs = list(itertools.product(sorted(edges), repeat=2))
    mul_pairs += [(ctypes.c_int64(rng.getrandbits(64)).value,
                   ctypes.c_int64(rng.getrandbits(64)).value) for _ in range(10000)]
    names = ('zero_direct', 'zero_or_left', 'zero_or_right', 'zero_on_false',
             'zero_ne', 'zero_two_words', 'shared_flag', 'negative_and',
             'negative_other_result', 'negative_nonzero_result',
             'negative_nonzero_compare', 'negative_ordered_compare')
    results = {}
    for label, llc in (('candidate', candidate), ('control', control)):
        obj, shared = (out / (label + suffix) for suffix in ('.o', '.so'))
        subprocess.run([str(llc), '-mtriple=x86_64-unknown-linux-gnu', '-O2',
                        '-verify-machineinstrs', '-relocation-model=pic',
                        '-filetype=obj', str(source), '-o', str(obj)], check=True)
        subprocess.run(['cc', '-shared', str(obj), '-o', str(shared)], check=True)
        library = ctypes.CDLL(str(shared))
        checks = dict.fromkeys(names, 0)
        functions = {}
        for name in names:
            fn = getattr(library, name)
            fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_uint32 if name == 'zero_two_words' else ctypes.c_uint8,
                           ctypes.c_uint32 if name == 'zero_two_words' else ctypes.c_uint8]
            if name == 'shared_flag':
                fn.argtypes += [ctypes.POINTER(ctypes.c_uint8)]
            if name == 'negative_other_result':
                fn.argtypes += [ctypes.c_uint32]
            fn.restype = ctypes.c_uint64 if name == 'zero_two_words' else ctypes.c_uint32
            functions[name] = fn
        for x, y in pairs:
            for a, b in itertools.product((0, 1), repeat=2):
                for name, fn in functions.items():
                    c = (x | y) == 0
                    value, sentinel = x, 0
                    if name == 'zero_direct' or name == 'negative_nonzero_result':
                        c = x == 0
                    elif name == 'zero_ne':
                        c = not c
                    elif name == 'negative_and':
                        c = (x & y) == 0
                    elif name == 'negative_nonzero_compare':
                        c = x == 1
                    elif name == 'negative_ordered_compare':
                        c = bool(x >> 31)
                    if name == 'zero_or_right':
                        value = y
                    elif name == 'zero_two_words':
                        value = x | y << 32
                    elif name == 'negative_other_result':
                        value = (x * 17 + y + 1) & 0xffffffff
                    elif name == 'negative_nonzero_result':
                        sentinel = 1
                    selected = a if c else b
                    expected = sentinel if selected else value
                    if name == 'zero_on_false':
                        expected = value if selected else sentinel
                    args = [x, y, a, b]
                    flag = ctypes.c_uint8(255)
                    if name == 'shared_flag':
                        args.append(ctypes.byref(flag))
                    if name == 'negative_other_result':
                        args.append(value)
                    actual = fn(*args)
                    if actual != expected or (name == 'shared_flag' and flag.value != selected):
                        raise ValueError((label, name, x, y, a, b, actual, expected, flag.value))
                    checks[name] += 1
        for name in ('checked_zero', 'checked_shared_flag', 'checked_nonzero_default'):
            fn = getattr(library, name)
            fn.argtypes = [ctypes.c_int64, ctypes.c_int64]
            if name == 'checked_shared_flag':
                fn.argtypes += [ctypes.POINTER(ctypes.c_uint8)]
            fn.restype = ctypes.c_int64
            checks[name] = 0
            for a, b in mul_pairs:
                exact = a * b
                overflow = not -(1 << 63) <= exact < 1 << 63
                sentinel = int(name == 'checked_nonzero_default')
                expected = sentinel if overflow else exact
                args = [a, b]
                flag = ctypes.c_uint8(255)
                if name == 'checked_shared_flag':
                    args.append(ctypes.byref(flag))
                actual = fn(*args)
                if actual != expected or (name == 'checked_shared_flag' and flag.value != overflow):
                    raise ValueError((label, name, a, b, actual, expected, flag.value))
                checks[name] += 1
        # Exercise defined predicates and zero-result inputs that mask poison.
        # Skip poison-producing predicates only when the source selects them.
        fn = library.zero_dynamic_poison
        fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint8]
        fn.restype = ctypes.c_uint32
        checks['zero_dynamic_poison'] = 0
        for x, v, a in itertools.product(values, values, (0, 1)):
            expected = add_predicate_result(x, v)
            if expected is None:
                continue
            actual = fn(x, v, a)
            if actual != expected:
                raise ValueError((label, 'zero_dynamic_poison', x, v, a, actual, expected))
            checks['zero_dynamic_poison'] += 1
        fn = library.zero_dynamic_poison_ne
        fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint8]
        fn.restype = ctypes.c_uint32
        checks['zero_dynamic_poison_ne'] = 0
        amounts = list(range(32)) + [32, 64, 255, 0xffffffff]
        for x, v, amount, a in itertools.product((0, 1, 0xffffffff), values, amounts, (0, 1)):
            expected = shift_predicate_result(x, v, amount)
            if expected is None:
                continue
            actual = fn(x, v, amount, a)
            if actual != expected:
                raise ValueError((label, 'zero_dynamic_poison_ne', x, v, amount, a, actual, expected))
            checks['zero_dynamic_poison_ne'] += 1
        results[label] = dict(llc_sha256=digest(llc), object_sha256=digest(obj),
                              checks=checks, mismatches=0)
    record = dict(architecture='x86_64', source_ir_sha256=digest(source),
                  validator_sha256=digest(Path(__file__)), results=results,
                  limitation='Defined source behavior, including masked poison-producing predicates; no formal poison-semantics proof or Xtensa timing')
    (out / 'host-execution.json').write_text(json.dumps(record, indent=2) + '\n')
    print('NATIVE_ZERO_SELECT_PASS', sum(results['candidate']['checks'].values()),
          'checks per compiler', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('candidate', 'control', 'source', 'out'):
        parser.add_argument('--' + option, type=Path, required=True)
    args = parser.parse_args()
    qualify(*(getattr(args, name).resolve() for name in ('candidate', 'control', 'source', 'out')))
