#!/usr/bin/env python3
"""Generate matched kernels and immutable independently computed vectors."""
import argparse
import hashlib
import json
from pathlib import Path

import integers

HERE = Path(__file__).resolve().parent
R_HEADER = '''// Generated from the coverage catalog. Do not hand-edit.
#![allow(unused_variables, unused_mut, unused_assignments, dead_code, private_interfaces)]
#[repr(C)] struct Input {a:u64,b:u64,c:u64,ah:u64,bh:u64,ch:u64}
#[repr(C)] struct Result {lo:u64,hi:u64,flag:u32,reserved:u32}
#[cfg(target_pointer_width="32")] type AqUsize=usize;
#[cfg(target_pointer_width="32")] type AqIsize=isize;
#[cfg(not(target_pointer_width="32"))] type AqUsize=u32;
#[cfg(not(target_pointer_width="32"))] type AqIsize=i32;
unsafe extern "C" {fn aq_driver_main(argc:i32,argv:*mut *mut u8)->i32;}
fn main(){unsafe {aq_driver_main(0,std::ptr::null_mut());}}
'''
C_HEADER = '''/* Generated from the coverage catalog. Do not hand-edit. */
#include "contract.h"
#include <limits.h>
#include <string.h>
#include <math.h>
static inline float aq_from_f32(uint64_t raw){uint32_t bits=(uint32_t)raw; float f;memcpy(&f,&bits,4);return f;}
static inline double aq_from_f64(uint64_t raw){double f;memcpy(&f,&raw,8);return f;}
static inline uint64_t aq_bits_f32(float f){uint32_t bits;memcpy(&bits,&f,4);return bits;}
static inline uint64_t aq_bits_f64(double f){uint64_t bits;memcpy(&bits,&f,8);return bits;}
'''


def catalog(include_float=True):
    cases = integers.operations()
    if include_float:
        import floating
        cases += floating.operations()
        import conversions
        cases += conversions.operations()
    for index, case in enumerate(cases):
        case.update(id=index, c_available=case.get("c_available", True))
    return cases


def generate(out, include_float=True, selected=None):
    cases = catalog(include_float)
    if selected:
        cases = [case for case in cases if any(part in case["name"] for part in selected)]
        for index, case in enumerate(cases): case["id"] = index
    if not cases:
        raise ValueError("no selected arithmetic cases")
    out.mkdir(parents=True, exist_ok=False)
    c_source, r_source, table, entries, symbols = [C_HEADER], [R_HEADER], ['#include "contract.h"'], [], []
    manifest_cases = []
    for case in cases:
        module = integers
        if case["kind"] == "float":
            import floating
            module = floating
        elif case["kind"] == "conversion":
            import conversions
            module = conversions
        index = case["id"]
        inputs = [module.vector(case, sample) for sample in range(64)]
        expected = [module.expected(case, value) for value in inputs]
        cname, rname = f"aq_c_{index:04d}", f"aq_r_{index:04d}"
        if case["c_available"]:
            c_source += [f"__attribute__((noinline)) void {cname}(const struct aq_input *inputs, struct aq_result *out, uint32_t n){{\nfor(uint32_t i=0;i<n;++i){{const struct aq_input *p=&inputs[i];struct aq_result *q=&out[i];\n{module.c_body(case)}\n}}\n}}\n"]
            table += [f"extern void {cname}(const struct aq_input*,struct aq_result*,uint32_t);"]
        r_source += [f"#[unsafe(no_mangle)] #[inline(never)] pub unsafe extern \"C\" fn {rname}(inputs:*const Input,out:*mut Result,n:u32){{\nfor i in 0..n as usize {{unsafe{{let p=&*inputs.add(i);let q=&mut*out.add(i);\n{module.r_body(case)}\n}}}}\n}}\n"]
        symbols.append(rname)
        table += [f"extern void {rname}(const struct aq_input*,struct aq_result*,uint32_t);"]
        table += [f"static const struct aq_input inputs_{index}[]={{" + ",".join("{" + ",".join(f"UINT64_C({v})" for v in row) + "}" for row in inputs) + "};"]
        table += [f"static const struct aq_result expected_{index}[]={{" + ",".join("{" + ",".join(f"UINT64_C({v})" for v in row[:2]) + f",{row[2]},{row[3]}" + "}" for row in expected) + "};"]
        float_bits = case.get("float_bits", case["bits"] if case["kind"] == "float" else 0)
        entries.append(f'{{"{case["name"]}",{cname if case["c_available"] else "NULL"},{rname},inputs_{index},expected_{index},64,{float_bits},{case["ulp_limit"]},{int(case.get("zero_sign_required",True))}}}')
        case["float_bits"] = float_bits
        manifest_cases.append({**case, "inputs": inputs, "expected": expected,
                               "c_symbol": cname if case["c_available"] else None, "rust_symbol": rname})
    table += ["const struct aq_case aq_cases[]={" + ",\n".join(entries) + "};", f"const uint32_t aq_case_count={len(cases)};"]
    files = {"kernels.c": "\n".join(c_source), "kernels.rs": "\n".join(r_source),
             "vectors.c": "\n".join(table), "keep-symbols.json": json.dumps(symbols),
             "Cargo.toml": '[package]\nname="nxrs-arithmetic-parity"\nversion="0.1.0"\nedition="2024"\npublish=false\n[workspace]\n[[bin]]\nname="arithmetic-parity"\npath="kernels.rs"\n[profile.release]\nopt-level="z"\nlto="fat"\ncodegen-units=1\npanic="abort"\ndebug=0\noverflow-checks=false\n[profile.release.package.nxrs-arithmetic-parity]\nopt-level=2\n',
             "Cargo.lock": 'version = 4\n[[package]]\nname = "nxrs-arithmetic-parity"\nversion = "0.1.0"\n'}
    for name, text in files.items(): (out / name).write_text(text + "\n")
    source_names = ["generate.py", "integers.py", "contract.h", "driver.c"]
    if include_float: source_names += ["floating.py", "conversions.py", "requirements-test.txt"]
    sources = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in source_names}
    proof = dict(schema=1, target="ESP32-S3, 32-bit pointer widths", samples_per_case=64,
                 sources=sources, cases=manifest_cases,
                 artifacts={name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in files},
                 comparison="identical kernels ABI/input data, no C undefined signed overflow",
                 limitations=["128-bit integer entries have no native C counterpart on Xtensa GCC",
                              "fixed directed/random vectors do not exhaust every possible input/program",
                              "host pointer-sized cases simulate the 32-bit target"])
    (out / "coverage.json").write_text(json.dumps(proof, separators=(",", ":")) + "\n")
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--integers-only", action="store_true")
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    report = generate(args.out, not args.integers_only, args.case)
    print(f'ARITHMETIC_GENERATE_PASS cases={len(report["cases"])} vectors={len(report["cases"])*64}')
