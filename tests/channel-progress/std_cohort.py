#!/usr/bin/env python3
"""Replay the seven prewarmed std primitive cases as one clean-image cohort.

Each case retains its own ownership and progress meaning. In particular, Busy
completes one try_lock attempt without accepting or delivering a payload. This
gate does not qualify a production transport, physical execution or timing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import analyze as evidence
import cohort as capture_checks
import mutex_analyze

SCHEMA = "std-transfer-cohort-v1"
SCOPE = "std-transfer-primitives"
CONTRACT = "std-prewarmed-single-item-v1"
MODES = mutex_analyze.MODES
QUALIFICATION_FLAGS = (
    "physical_execution_verified", "physical_progress_qualified",
    "priority_progress_qualified", "production_transport_qualified",
    "physical_timing_qualified", "timing_qualified", "hard_realtime_bound",
)


def require(condition, message):
    evidence.require(condition, message)


def replay_case(run, mode):
    run = Path(run)
    summary, receipt, files = capture_checks.replay_capture(run, mode, schema=mutex_analyze.SCHEMA)
    require(summary.get("scope") == SCOPE, "std cohort requires its named primitive scope")
    require(receipt.get("mode") == mutex_analyze.BUILD_MODE and receipt.get("hook_stage") == 0,
            "std cohort requires its clean stage-zero suite source recipe")
    stored = evidence.read_json(files["analysis.json"])
    require(isinstance(stored, dict) and evidence.canonical(stored) == evidence.canonical(summary),
            "stored std analysis disagrees with independently replayed evidence")
    # capture.py records execution-attached analysis; the separate acceptance
    # command records only raw analysis bound to the retained build receipt.
    files["acceptance.json"] = evidence.checked_file(run, "acceptance.json")
    acceptance = evidence.read_json(files["acceptance.json"])
    expected_acceptance = evidence.bind_build(
        evidence.analyze(files["console.log"].read_bytes().decode("utf-8", errors="strict")), receipt)
    evidence.require_no_repair(expected_acceptance)
    require(isinstance(acceptance, dict)
            and evidence.canonical(acceptance) == evidence.canonical(expected_acceptance),
            "stored std acceptance disagrees with independently replayed evidence")
    for field in QUALIFICATION_FLAGS:
        if field != "physical_progress_qualified":
            require(evidence.boolean(summary, field) is False,
                    f"unsupported std qualification: {field}")
    # Session labels remain operator assertions even if a stored summary repeats
    # them consistently. Extra claim fields cannot smuggle broader admission.
    capture = evidence.read_json(files["capture.json"])
    for name, record in (("metadata", summary["metadata"]), ("result", summary["result"]),
                         ("session", summary["session"]), ("capture", capture)):
        for field in QUALIFICATION_FLAGS + ("physical_measurement", "production_qualified"):
            if field in record:
                require(evidence.boolean(record, field) is False,
                        f"unsupported std {name} qualification: {field}")
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
        "primitive_contract_satisfied": summary["primitive_contract_satisfied"],
        "evidence_sha256": {name: evidence.sha256(path) for name, path in files.items()},
        "observation": summary["result"],
    }
    return case, receipt, environment


def analyze_cohort(root):
    root = Path(root).resolve(strict=True)
    require(root.is_dir(), "std cohort root is not a directory")
    names = {path.name for path in root.iterdir() if path.is_dir() or path.is_symlink()}
    missing, extra = set(MODES) - names, names - set(MODES)
    require(not missing and not extra,
            f"std cohort requires exactly seven named cases; missing={sorted(missing)}, unexpected={sorted(extra)}")
    results, shared_build, environment, paths = [], None, None, set()
    for mode in MODES:
        directory = capture_checks.directory(root, mode)
        run = capture_checks.directory(directory, "run")
        require(run not in paths, "duplicate std capture directory")
        paths.add(run)
        case, receipt, execution = replay_case(run, mode)
        if shared_build is None:
            shared_build, environment = receipt, execution
        else:
            require(evidence.canonical(receipt) == evidence.canonical(shared_build),
                    f"mixed std suite build/image/source/configuration at {mode}")
            require(evidence.canonical(execution) == evidence.canonical(environment),
                    f"mixed std execution environment at {mode}")
        case["directory"] = f"{mode}/run"
        results.append(case)
    blocking = [case["mode"] for case in results if not case["completed_without_repair"]
                or not case["progress_contract_satisfied"] or not case["primitive_contract_satisfied"]]
    identity = {key: shared_build[key] for key in capture_checks.common_identity_fields(shared_build)}
    identity.update(build_id=shared_build["build_id"], image_sha256=shared_build["image_sha256"],
                    elf_sha256=shared_build["elf_sha256"], mode=shared_build["mode"],
                    hook_stage=shared_build["hook_stage"], crossbeam_receipt=shared_build["crossbeam_receipt"])
    report = {
        "schema": SCHEMA, "contract": CONTRACT, "scope": SCOPE,
        "cohort_complete": True, "diagnostic_valid": True, "required_modes": list(MODES),
        "cases": results, "blocking_cases": blocking,
        "primitive_contract_satisfied": not blocking,
        "all_cases_completed_without_repair": all(case["completed_without_repair"] for case in results),
        "progress_contract_satisfied": not blocking,
        "outcome": "primitive-cases-satisfied" if not blocking else "complete-with-progress-failures",
        "matched_identity": identity, "matched_identity_sha256": evidence.canonical(identity),
        "shared_build_receipt_sha256": evidence.canonical(shared_build),
        "execution_environment": environment, "runtime_kind": environment["runtime_kind"],
        "execution_evidence": environment["execution_evidence"],
        **dict.fromkeys(QUALIFICATION_FLAGS, False),
        "scope_limits": (
            "Seven named prewarmed single-item std Mutex/Condvar observations. Busy completes one "
            "try_lock attempt while retaining the original or protected value; it does not prove "
            "admission or delivery. The cohort does not qualify a complete owner protocol, arbitrary "
            "method progress, production transport, physical execution or timing."
        ),
    }
    if environment["runtime_kind"] == "physical":
        report["physical_evidence_scope"] = (
            "Operator-declared physical cohort; device identity, flash/readback and exclusive "
            "ownership are not independently verified.")
    return report


def require_no_repair(summary):
    require(summary.get("schema") == SCHEMA and summary.get("contract") == CONTRACT
            and summary.get("scope") == SCOPE and summary.get("cohort_complete") is True
            and summary.get("diagnostic_valid") is True and summary.get("required_modes") == list(MODES)
            and summary.get("primitive_contract_satisfied") is True
            and summary.get("progress_contract_satisfied") is True
            and summary.get("all_cases_completed_without_repair") is True,
            "std no-repair gate refused: all seven authenticated primitive cases must pass in the named scope")


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
        print(f"std cohort rejected: {error}", file=sys.stderr)
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
