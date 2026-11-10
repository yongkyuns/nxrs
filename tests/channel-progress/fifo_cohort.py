#!/usr/bin/env python3
"""Replay all six equal-FIFO cases before admitting their narrow candidate.

Each capture must identify its own runtime mode while using the same authenticated
suite image. The gate covers the stated reservation/reclamation observations;
it does not admit the full selected owner, production use, hardware or timing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import analyze as evidence
import cohort as capture_checks
import fifo_analyze as cases

SCHEMA = "fifo-channel-cohort-v1"
MODES = cases.MODES


def require(condition, message):
    evidence.require(condition, message)


def replay_case(run, mode):
    summary, receipt, files = capture_checks.replay_capture(run, mode, schema=cases.SCHEMA)
    require(summary.get("scope") == cases.SCOPE and summary.get("contract") == cases.CONTRACT,
            "FIFO cohort requires its named reservation-progress scope")
    require(receipt.get("mode") == cases.BUILD_MODE and receipt.get("hook_stage") == 5,
            "FIFO cohort requires its own suite source recipe")
    stored = evidence.read_json(files["analysis.json"])
    # This new family has no historical physical-overclaim compatibility. The
    # complete replayed result, including all false qualification flags, must agree.
    require(isinstance(stored, dict) and evidence.canonical(stored) == evidence.canonical(summary),
            "stored FIFO analysis disagrees with independently replayed evidence")
    for field in ("priority_progress_qualified", "production_transport_qualified",
                  "physical_execution_verified", "physical_timing_qualified",
                  "timing_qualified", "hard_realtime_bound"):
        require(evidence.boolean(summary, field) is False, f"unsupported FIFO qualification: {field}")
    session = summary["session"]
    environment = {
        "runtime_kind": summary["runtime_kind"], "execution_evidence": summary["execution_evidence"],
        "board": session["board"],
    }
    if summary["runtime_kind"] == "physical":
        environment.update(board_serial=session["board_serial"], active_cores=session["active_cores"])
    case = {
        "mode": mode, "run_id": summary["run_id"], "build_id": receipt["build_id"],
        "image_sha256": receipt["image_sha256"], "outcome": summary["outcome"],
        "completed_without_repair": summary["completed_without_repair"],
        "progress_contract_satisfied": summary["progress_contract_satisfied"],
        "evidence_sha256": {name: evidence.sha256(path) for name, path in files.items()},
        "observation": summary["result"],
    }
    return case, receipt, environment


def analyze_cohort(root):
    root = Path(root).resolve(strict=True)
    require(root.is_dir(), "FIFO cohort root is not a directory")
    names = {path.name for path in root.iterdir() if path.is_dir() or path.is_symlink()}
    missing, extra = set(MODES) - names, names - set(MODES)
    require(not missing and not extra,
            f"FIFO cohort requires exactly six named cases; missing={sorted(missing)}, unexpected={sorted(extra)}")
    results, shared_build, environment, paths = [], None, None, set()
    for mode in MODES:
        directory = capture_checks.directory(root, mode)
        run = capture_checks.directory(directory, "run")
        require(run not in paths, "duplicate FIFO capture directory")
        paths.add(run)
        case, receipt, execution = replay_case(run, mode)
        if shared_build is None:
            shared_build, environment = receipt, execution
        else:
            require(evidence.canonical(receipt) == evidence.canonical(shared_build),
                    f"mixed FIFO suite build/image/source/configuration at {mode}")
            require(evidence.canonical(execution) == evidence.canonical(environment),
                    f"mixed FIFO execution environment at {mode}")
        case["directory"] = f"{mode}/run"
        results.append(case)
    blocking = [case["mode"] for case in results
                if not case["completed_without_repair"] or not case["progress_contract_satisfied"]]
    identity = {key: shared_build[key] for key in capture_checks.common_identity_fields(shared_build)}
    identity.update(build_id=shared_build["build_id"], image_sha256=shared_build["image_sha256"],
                    elf_sha256=shared_build["elf_sha256"], mode=shared_build["mode"],
                    hook_stage=shared_build["hook_stage"],
                    fifo_kernel_source_sha256=shared_build["fifo_kernel_source_sha256"],
                    crossbeam_receipt=shared_build["crossbeam_receipt"])
    report = {
        "schema": SCHEMA, "contract": cases.CONTRACT, "scope": cases.SCOPE,
        "cohort_complete": True, "diagnostic_valid": True, "required_modes": list(MODES),
        "cases": results, "blocking_cases": blocking,
        "candidate_contract_satisfied": not blocking,
        "all_cases_completed_without_repair": not blocking,
        "progress_contract_satisfied": not blocking,
        "outcome": "candidate-cases-satisfied" if not blocking else "complete-with-progress-failures",
        "matched_identity": identity, "matched_identity_sha256": evidence.canonical(identity),
        "shared_build_receipt_sha256": evidence.canonical(shared_build),
        "execution_environment": environment, "runtime_kind": environment["runtime_kind"],
        "execution_evidence": environment["execution_evidence"],
        "physical_execution_verified": False, "physical_progress_qualified": False,
        "priority_progress_qualified": False, "production_transport_qualified": False,
        "physical_timing_qualified": False, "timing_qualified": False, "hard_realtime_bound": False,
        "scope_limits": (
            "Six named reservation/reclamation observations under the admitted single-core equal-FIFO "
            "run-to-block contract and finite native interference. This does not qualify arbitrary "
            "method completion, the full selected owner, production adoption, physical execution or time."
        ),
    }
    if environment["runtime_kind"] == "physical":
        report["physical_evidence_scope"] = (
            "Operator-declared physical cohort; device identity, flash/readback and exclusive "
            "ownership are not independently verified.")
    return report


def require_no_repair(summary):
    require(summary.get("schema") == SCHEMA and summary.get("contract") == cases.CONTRACT
            and summary.get("scope") == cases.SCOPE and summary.get("cohort_complete") is True
            and summary.get("diagnostic_valid") is True
            and summary.get("required_modes") == list(MODES)
            and summary.get("candidate_contract_satisfied") is True
            and summary.get("all_cases_completed_without_repair") is True,
            "FIFO no-repair gate refused: all six authenticated candidate cases must pass in the named scope")


def require_physical(summary):
    require(summary.get("physical_execution_verified") is True
            and summary.get("physical_progress_qualified") is True,
            "physical gate refused: operator session labels are not verified device/flash/ownership evidence")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort_directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-no-repair", action="store_true")
    parser.add_argument("--require-physical", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = analyze_cohort(args.cohort_directory)
    except (ValueError, OSError, KeyError) as error:
        print(f"FIFO cohort rejected: {error}", file=sys.stderr)
        return 1
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    try:
        if args.require_no_repair:
            require_no_repair(report)
        if args.require_physical:
            require_physical(report)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
