#!/usr/bin/env python3
"""Execute the generated native host kernels against the independent oracle.

Host execution is a generator/semantics regression, not Xtensa qualification.
Timing is deliberately not reported here.
"""
import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import sys

import integers

HERE = Path(__file__).resolve().parent


class Input(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("a", "b", "c", "ah", "bh", "ch")]


class Result(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint64), ("hi", ctypes.c_uint64),
                ("flag", ctypes.c_uint32), ("reserved", ctypes.c_uint32)]


def result_tuple(value):
    return value.lo, value.hi, value.flag, value.reserved


def matches(actual, expected, case):
    if not case.get("float_bits", case["bits"] if case["kind"]=="float" else 0):
        return actual == expected
    if actual[1:] != expected[1:]: return False
    bits = case["bits"]
    # An f32 occupies only the low word of the 64-bit result ABI slot.
    if bits == 32 and (actual[0] >> 32 or expected[0] >> 32): return False
    fraction = 23 if bits == 32 else 52
    sign = 1 << (bits - 1)
    mask = (1 << bits) - 1
    exponent = mask ^ sign ^ ((1 << fraction) - 1)
    a, e = actual[0], expected[0]
    nan = lambda v: v & exponent == exponent and v & ((1 << fraction) - 1) != 0
    if nan(e): return nan(a)
    if e & exponent == exponent: return a == e
    if e & (sign - 1) == 0:
        return a==e if case.get("zero_sign_required",True) else a & (sign-1)==0
    if a & exponent == exponent: return False
    ordered = lambda v: (~v & mask) if v & sign else v | sign
    return abs(ordered(a) - ordered(e)) <= case["ulp_limit"]


def case_failures(case, actual, expected):
    """Never let zip truncate away missing results or oracle entries."""
    if len(actual) != len(expected):
        raise ValueError(f'{case["name"]}: result count {len(actual)} != expected {len(expected)}')
    return [(i, value, tuple(reference)) for i, (value, reference) in
            enumerate(zip(actual, expected)) if not matches(value, tuple(reference), case)]


def execute(function, vectors):
    inputs = (Input * len(vectors))(*(Input(*row) for row in vectors))
    output = (Result * len(vectors))(*(Result((1 << 64)-1, (1 << 64)-1, 0xffffffff, 0xffffffff)
                                     for _ in vectors))
    function.argtypes = [ctypes.POINTER(Input), ctypes.POINTER(Result), ctypes.c_uint32]
    function.restype = None
    function(inputs, output, len(vectors))
    return [result_tuple(value) for value in output]


def verify(directory, exhaustive=False, compiler="cc", rustc="rustc"):
    proof = json.loads((directory / "coverage.json").read_text())
    for case in proof["cases"]:
        if (len(case["inputs"]) != proof["samples_per_case"] or
                len(case["expected"]) != proof["samples_per_case"]):
            raise ValueError(f'{case["name"]}: vector count differs from samples_per_case')
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    c_lib, r_lib = directory / ("libaq_c" + suffix), directory / ("libaq_rust" + suffix)
    with (directory / "host-c-build.log").open("w") as log:
        subprocess.run([compiler, "-std=c11", "-O2", "-ffp-contract=off", "-fno-fast-math", "-fno-math-errno",
                        "-shared", "-fPIC", "-I", str(HERE), str(directory / "kernels.c"),
                        "-o", str(c_lib), "-lm"], check=True, stdout=log, stderr=subprocess.STDOUT)
    with (directory / "host-rust-build.log").open("w") as log:
        subprocess.run([rustc, "--edition=2024", "--crate-type=cdylib", "-Copt-level=2",
                        "-Cpanic=abort", str(directory / "kernels.rs"), "-o", str(r_lib)], check=True,
                       stdout=log, stderr=subprocess.STDOUT)
    libs = {"c": ctypes.CDLL(str(c_lib.resolve())), "rust": ctypes.CDLL(str(r_lib.resolve()))}
    errors, comparisons, case_errors = [], 0, {}
    for case in proof["cases"]:
        for backend in ("c", "rust"):
            symbol = case["c_symbol" if backend == "c" else "rust_symbol"]
            if symbol is None: continue
            actual = execute(getattr(libs[backend], symbol), case["inputs"])
            failures = case_failures(case, actual, case["expected"])
            if failures:
                case_errors[f'{backend}:{case["name"]}'] = len(failures)
                errors += [dict(case=case["name"], backend=backend, sample=i,
                                actual=value, expected=expected) for i, value, expected in failures[:4]]
            comparisons += len(actual)
    exhaustive_count = 0
    if exhaustive:
        selected = {"wrapping_add", "wrapping_sub", "wrapping_mul", "checked_div", "checked_rem",
                    "wrapping_shl", "wrapping_shr", "rotate_left", "rotate_right", "and", "or", "xor"}
        for case in proof["cases"]:
            if case["kind"] != "integer" or case["type"] not in ("u8", "i8"):
                continue
            if case["name"][3:] not in selected: continue
            vectors = [(a, b, 0, 0, 0, 0) for a in range(256) for b in range(256)]
            expected = [integers.expected(case, row) for row in vectors]
            for backend in ("c", "rust"):
                actual = execute(getattr(libs[backend], case["c_symbol" if backend == "c" else "rust_symbol"]), vectors)
                failures = case_failures(case, actual, expected)
                if failures:
                    case_errors[f'exhaustive:{backend}:{case["name"]}'] = len(failures)
                    errors += [dict(case=case["name"], backend=backend, sample=i,
                                    actual=value, expected=target) for i, value, target in failures[:4]]
                comparisons += len(actual)
                exhaustive_count += len(actual)
    report = dict(schema=1, kind="host-only arithmetic generator verification",
                  passed=not errors, cases=len(proof["cases"]), comparisons=comparisons,
                  exhaustive_8bit_comparisons=exhaustive_count, case_errors=case_errors,
                  errors=errors, limitations="Host correctness does not qualify Xtensa code generation or speed")
    (directory / "host-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "errors"}))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated", type=Path, required=True)
    parser.add_argument("--exhaustive-8", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.generated, args.exhaustive_8)["passed"] else 1)
