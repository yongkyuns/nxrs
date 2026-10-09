#!/usr/bin/env python3
"""Execute two LLVM backends' saturating casts against Python's IEEE oracle.

Host execution qualifies generic lowering, not ESP32-S3 timing. The generated
IR stores its result through a pointer so i128 does not depend on a host ABI.
"""
import argparse
import ctypes
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import struct
import subprocess


def inputs(source):
    fraction_bits, max_exponent = (23, 255) if source == 32 else (52, 2047)
    fractions = (0, 1, (1 << (fraction_bits - 1)) - 1,
                 1 << (fraction_bits - 1), (1 << fraction_bits) - 2,
                 (1 << fraction_bits) - 1)
    edges = [sign << (source - 1) | exponent << fraction_bits | fraction
             for sign in (0, 1) for exponent in range(max_exponent + 1)
             for fraction in fractions]
    rng = random.Random(0x534154 + source)
    return edges + [rng.getrandbits(source) for _ in range(20000)]


def oracle(raw, source, bits, signed):
    value = struct.unpack('<f' if source == 32 else '<d', raw.to_bytes(source // 8, 'little'))[0]
    low = -(1 << (bits - 1)) if signed else 0
    high = (1 << (bits - int(signed))) - 1
    if math.isnan(value):
        result = 0
    elif math.isinf(value):
        result = low if value < 0 else high
    else:
        result = min(high, max(low, int(value)))
    return result & ((1 << bits) - 1)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qualify(candidate, control, cc, out):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('native qualification requires Linux x86_64')
    out.mkdir(parents=True, exist_ok=False)
    declarations, functions, families = [], [], []
    for source in (32, 64):
        for bits in (64, 128):
            for signed in (False, True):
                name = f'f{source}_{"i" if signed else "u"}{bits}'
                intrinsic = f'llvm.fpto{"si" if signed else "ui"}.sat.i{bits}.f{source}'
                fp = 'float' if source == 32 else 'double'
                declarations.append(f'declare i{bits} @{intrinsic}({fp})')
                functions.append(f'''define void @{name}(i{source} %raw, ptr %out) {{
  %x = bitcast i{source} %raw to {fp}
  %r = call i{bits} @{intrinsic}({fp} %x)
  store i{bits} %r, ptr %out, align 8
  ret void
}}''')
                families.append((name, source, bits, signed))
    ir = out / 'casts.ll'
    ir.write_text('\n'.join(declarations + functions) + '\n')
    results = {}
    for label, llc in (('candidate', candidate), ('control', control)):
        obj, shared, asm = (out / (label + suffix) for suffix in ('.o', '.so', '.s'))
        args = [str(llc), '-mtriple=x86_64-unknown-linux-gnu', '-O2', '-mattr=+lzcnt',
                '-verify-machineinstrs', '-relocation-model=pic', str(ir)]
        subprocess.run([*args, '-filetype=obj', '-o', str(obj)], check=True)
        subprocess.run([*args, '-o', str(asm)], check=True)
        subprocess.run([cc, '-shared', str(obj), '-o', str(shared)], check=True)
        library = ctypes.CDLL(str(shared))
        checks = {}
        for name, source, bits, signed in families:
            fn = getattr(library, name)
            fn.argtypes = [ctypes.c_uint32 if source == 32 else ctypes.c_uint64,
                           ctypes.POINTER(ctypes.c_uint64)]
            fn.restype = None
            values = inputs(source)
            for raw in values:
                output = (ctypes.c_uint64 * 2)(0, 0)
                fn(raw, output)
                actual = output[0] | output[1] << 64
                if actual != oracle(raw, source, bits, signed):
                    raise ValueError(f'{label}: {name} mismatch for {raw:#x}')
            checks[name] = len(values)
        results[label] = dict(checks=checks, mismatches=0, llc_sha256=digest(llc),
            object_sha256=digest(obj), assembly_sha256=digest(asm))
    record = dict(architecture='x86_64', source_ir_sha256=digest(ir), results=results,
        validator_sha256=digest(Path(__file__)),
        limitation='Native generic conversion execution; not Xtensa timing')
    (out / 'host-execution.json').write_text(json.dumps(record, indent=2) + '\n')
    print('NATIVE_FP_CASTS_PASS', sum(results['candidate']['checks'].values()), 'checks per compiler')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'control', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--cc', default='cc')
    args = parser.parse_args()
    qualify(args.candidate.resolve(), args.control.resolve(), args.cc, args.out.resolve())
