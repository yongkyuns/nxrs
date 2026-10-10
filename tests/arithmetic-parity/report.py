#!/usr/bin/env python3
"""Export compact numerical evidence only after reparsing every private capture."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

import build
import measure


def summarize(generated, linked, captured, recover_ansi_prompt=False):
    coverage = build.validate_generated(generated)
    provenance = json.loads((linked/"build-provenance.json").read_text())
    saved = json.loads((captured/"summary.json").read_text())
    recovered=False
    if recover_ansi_prompt and saved["failure"] == "ValueError":
        # Recover only this known parser defect, never arbitrary failed runs.
        # Original proof/summary files remain untouched and their rejection is reported.
        proof=json.loads((captured/"proof.json").read_text())
        if proof["failure_detail"] != "ValueError: final NSH prompt must follow AQ_DONE":
            raise ValueError("not the recoverable ANSI prompt parser failure")
        if saved["runs"] or not proof["restoration"]["verified"]:
            raise ValueError("unexpected recovery cohort or unverified restoration")
        aggregate=(captured/"serial.raw").read_bytes()
        raw=(captured/"run-001.raw").read_bytes()
        if not raw or aggregate.count(raw) != 1:
            raise ValueError("recovered complete run is not unique in aggregate capture")
        parsed=measure.parse(raw,coverage)
        parsed.update(raw_capture=1,raw_bytes=len(raw),raw_sha256=hashlib.sha256(raw).hexdigest(),
                      aggregate_offset=aggregate.find(raw))
        saved={**saved,"failure":None,"runs":[parsed]}
        recovered=True
    identity = build.digest(generated/"coverage.json")
    if identity != saved["coverage_sha256"] or identity != provenance["coverage_sha256"]:
        raise ValueError("coverage identities differ")
    for name, sha in provenance["artifacts"].items():
        if build.digest(linked/name) != sha:
            raise ValueError(f"frozen linked artifact changed: {name}")
    if saved["image_sha256"] != provenance["artifacts"]["image.bin"]:
        raise ValueError("measured image differs from linked image")
    if saved["failure"] or not saved["restoration"]["verified"] or not saved["runs"]:
        raise ValueError("incomplete measurement or unverified firmware restoration")
    aggregate = (captured/"serial.raw").read_bytes()
    if hashlib.sha256(aggregate).hexdigest() != saved["aggregate_raw_sha256"]:
        raise ValueError("aggregate raw capture changed")
    runs=[]
    for index, row in enumerate(saved["runs"], start=1):
        raw=(captured/f"run-{index:03d}.raw").read_bytes()
        offset=row["aggregate_offset"]
        if (row["raw_capture"] != index or len(raw) != row["raw_bytes"] or
                hashlib.sha256(raw).hexdigest() != row["raw_sha256"] or
                aggregate[offset:offset+len(raw)] != raw):
            raise ValueError("private run capture changed")
        parsed=measure.parse(raw,coverage)
        if parsed["cases"] != row["cases"] or parsed["passed"] != row["passed"]:
            raise ValueError("saved numerical results differ from independently reparsed capture")
        runs.append(parsed)
    cases=[]
    for case in coverage["cases"]:
        ident=case["id"]
        record={key:case[key] for key in
                ("id","name","kind","bits","float_bits","ulp_limit","c_available")}
        record["zero_sign_required"]=case.get("zero_sign_required",True)
        record["correctness"]=[{key:run["cases"][ident][key] for key in
                               ("c_errors","rust_errors","pair_diff","c_max_ulp","rust_max_ulp")}
                              for run in runs]
        record["failures"]=[run["cases"][ident]["failures"] for run in runs]
        record["function_bytes"]={backend:(provenance["symbols"][symbol]["function_bytes"]
                                          if symbol else None)
                                  for backend,symbol in (("c",case["c_symbol"]),("rust",case["rust_symbol"]))}
        record["cycles"]={mode:{backend:[[value[backend+"_cycles"] for value in
                                         run["cases"][ident]["timings"][mode]] for run in runs]
                                for backend in ("c","rust")}
                          for mode in ("masked","normal")}
        masked=record["cycles"]["masked"]
        c=statistics.median(value for row in masked["c"] for value in row)
        r=statistics.median(value for row in masked["rust"] for value in row)
        record["masked_median_cycles"]={"c":c if case["c_available"] else None,"rust":r}
        record["rust_c_ratio"]=r/c if case["c_available"] else None
        cases.append(record)
    compiler=provenance["compiler_input"]
    result = dict(schema=1,board="ESP32-S3",cpu_mhz=240,samples_per_case=64,
                repeats_per_mode_per_run=5,runs=len(runs),
                passed=all(row["passed"] for row in runs),
                totals=dict(c_errors=sum(row["correctness"]["c_errors"] for row in runs),
                            rust_errors=sum(row["correctness"]["rust_errors"] for row in runs),
                            raw_pair_differences=sum(row["correctness"]["pair_diff"] for row in runs)),
                coverage_sha256=identity,source_sha256=coverage["sources"],
                capture_sha256=[row["raw_sha256"] for row in saved["runs"]],
                collection=[dict(runs=len(runs),recovered_ansi_prompt=recovered,
                                 original_collector_failure="ValueError" if recovered else None)],
                restoration_verified=True,
                build=dict(artifacts=provenance["artifacts"],kernel_config_sha256=provenance["kernel_config_sha256"],
                           c_compiler_version=provenance["c_compiler_version"],
                           c_compiler_sha256=provenance["c_compiler_sha256"],c_flags=provenance["c_flags"],
                           rust_compiler_sha256=compiler["compiler_sha256"],
                           rust_driver_libraries_sha256=compiler["compiler_libraries_sha256"],
                           compiler_package_provenance_sha256=compiler["compiler_package_provenance_sha256"],
                           target_sha256=compiler["target_sha256"],std_inventory_sha256=compiler["std_inventory_sha256"],
                           std_features=compiler["std_features"],application_opt_level=compiler["application_opt_level"],
                           patch_ledger=compiler["patch_ledger"],size_scope=provenance["size_scope"]),
                limitations=coverage["limitations"]+[
                    "Floating libm tolerances are qualification budgets, not Rust accuracy guarantees",
                    "Timing includes calls, input loads and result stores; corpus includes edge/error cases",
                    "Warm flash layout is one placement; interrupts/preemption remain in normal samples",
                    "Function sizes omit literals/shared helpers; this is not a final firmware size comparison"],
                cases=cases)
    if "std_proposal_ledger" in compiler:
        result["build"]["std_proposal_ledger"] = compiler["std_proposal_ledger"]
    if "kernel_libraries_sha256" in provenance:
        result["build"]["kernel_libraries_inventory_sha256"] = hashlib.sha256(
            json.dumps(provenance["kernel_libraries_sha256"], sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("generated","linked","out"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--captured",type=Path,action="append",required=True)
    parser.add_argument("--recover-ansi-prompt",action="store_true")
    args=parser.parse_args()
    cohorts=[summarize(args.generated,args.linked,path,args.recover_ansi_prompt) for path in args.captured]
    report=cohorts[0]
    for cohort in cohorts[1:]:
        if (cohort["coverage_sha256"] != report["coverage_sha256"] or
                cohort["build"] != report["build"]):
            raise ValueError("cannot pool different build/coverage cohorts")
        report["runs"]+=cohort["runs"]
        report["passed"] &= cohort["passed"]
        report["collection"]+=cohort["collection"]
        report["capture_sha256"]+=cohort["capture_sha256"]
        for key in report["totals"]:report["totals"][key]+=cohort["totals"][key]
        for target,row in zip(report["cases"],cohort["cases"],strict=True):
            if target["id"] != row["id"]:raise ValueError("case identities differ")
            target["correctness"]+=row["correctness"]
            target["failures"]+=row["failures"]
            for mode in ("masked","normal"):
                for backend in ("c","rust"):
                    target["cycles"][mode][backend]+=row["cycles"][mode][backend]
            c=statistics.median(v for run in target["cycles"]["masked"]["c"] for v in run)
            r=statistics.median(v for run in target["cycles"]["masked"]["rust"] for v in run)
            target["masked_median_cycles"]={"c":c if target["c_available"] else None,"rust":r}
            target["rust_c_ratio"]=r/c if target["c_available"] else None
    # One case per line keeps all repeated measurements without a megabyte of indentation.
    cases=report.pop("cases")
    header=json.dumps(report,indent=2)
    payload=header[:-2]+',\n  "cases": [\n'+",\n".join(
        "    "+json.dumps(row,separators=(",",":")) for row in cases)+'\n  ]\n}\n'
    with args.out.open("x") as stream:
        stream.write(payload)
    print("ARITHMETIC_REPORT",len(cases),"cases",report["totals"],"restored=true")


if __name__=="__main__":main()
