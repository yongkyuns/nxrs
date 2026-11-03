#!/usr/bin/env python3
"""Validate the complete six-case Crossbeam progress contract as one cohort.

Publication controls alone cannot satisfy this gate. Every required case is
replayed from its raw capture and source-bound firmware. A valid diagnostic
cohort may demonstrate failed progress; its negative report is retained before
an optional no-repair acceptance check refuses it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

import analyze as evidence

SCHEMA = "channel-progress-cohort-v1"
CHANNEL_SCHEMA = "channel-progress-v1"
MODES = (
    "direct-reserved", "direct-published", "select-reserved", "select-published",
    "send-reserved", "send-reclaimed",
)
CONTROLS = ("direct-published", "select-published", "send-reclaimed")
IDENTITY_FIELDS = (
    "source_revision", "source_sha256", "platform", "target", "sdk",
    "kernel_revision", "apps_revision", "compiler_identity", "std_library_tree_sha256",
    "std_array_original_sha256", "std_array_sha256", "resolved_configuration", "config_sha256",
    "staged_inputs_sha256",
)
CAPTURE_FILES = ("console.log", "build.json", "capture.json", "session.json", "analysis.json")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FAULT = re.compile(r"\bPANIC\b|ASSERTION|HardFault|UsageFault|BusFault")
BOOT_BANNERS = ("NuttShell", "*** Booting NuttX ***")
MAX_CONSOLE_BYTES = 2 * 1024 * 1024


def require(condition, message):
    evidence.require(condition, message)


def directory(root, relative):
    path = root / relative
    require(not path.is_symlink() and path.is_dir(), f"missing or linked case directory: {relative}")
    resolved = path.resolve()
    require(resolved.is_relative_to(root), f"case directory escapes cohort: {relative}")
    return resolved


def capture_structure(raw, mode):
    """Check a fresh boot, sole echoed command, two records and returned prompt.

    This validates capture structure, not an independently authenticated physical
    board. ANSI line-clearing sequences are permitted around the shell echo.
    """
    require(len(raw) <= MAX_CONSOLE_BYTES, "console exceeds capture size bound")
    text = raw.decode("utf-8", errors="strict").replace("\r", "")
    require(not FAULT.search(text), "fault in raw capture")
    matches = list(re.finditer(r"(?m)^CHANNEL_PROGRESS [^\n]*(?:\n|$)", text))
    require(len(matches) == 2, "capture needs exactly two Crossbeam records")
    shell_text = ANSI.sub("", text)
    prefix = ANSI.sub("", text[:matches[0].start()])
    suffix = ANSI.sub("", text[matches[-1].end():]).strip()
    commands = list(re.finditer(r"(?m)^nsh>[ \t]*(\S[^\n]*)$", shell_text))
    require(len(commands) == 1, "capture must contain exactly one echoed shell command")
    require(commands[0].group(1).strip() == f"channel_progress {mode}",
            "raw command differs from required case")
    require(commands[0].end() <= len(prefix), "command was not observed before metadata")
    before_command = prefix[:commands[0].start()]
    require(any(banner in before_command for banner in BOOT_BANNERS),
            "raw boot banner is missing before command")
    require(all(shell_text.count(banner) <= 1 for banner in BOOT_BANNERS),
            "capture contains multiple boots")
    require(suffix == "nsh>", "capture did not return to its final shell prompt")
    return {"fresh_boot_observed": True,
            "boot_rom_digest_warning_observed": "SHA-256 comparison failed:" in before_command}


def _stored_analysis(stored, replay):
    """Compare original core observations; ignore only the old physical overclaim."""
    require(isinstance(stored, dict) and stored.get("schema") == "channel-progress-analysis-v1",
            "unknown stored Crossbeam analysis")
    for key in ("run_id", "mode", "fixture_valid", "outcome", "completed_without_repair",
                "progress_contract_satisfied", "build_bound", "runtime_kind",
                "metadata", "result", "build", "session"):
        require(key in stored and evidence.canonical(stored[key]) == evidence.canonical(replay.get(key)),
                f"stored analysis disagrees with replay: {key}")
    for key in ("priority_progress_qualified", "physical_timing_qualified", "timing_qualified",
                "hard_realtime_bound"):
        require(evidence.boolean(stored, key) is False, f"stored analysis makes an unsupported claim: {key}")
    claimed_physical = evidence.boolean(stored, "physical_execution_verified")
    require(not claimed_physical or replay["runtime_kind"] == "physical",
            "virtual capture falsely labelled physically verified")
    if "execution_evidence" in stored:
        require(stored["execution_evidence"] == replay["execution_evidence"],
                "stored execution evidence differs from replay")
    return claimed_physical


def replay_case(run, mode):
    files = {name: evidence.checked_file(run, name) for name in CAPTURE_FILES}
    raw = files["console.log"].read_bytes()
    structure = capture_structure(raw, mode)
    summary = evidence.analyze(raw.decode("utf-8", errors="strict"))
    metadata = summary["metadata"]
    require(metadata.get("schema") == CHANNEL_SCHEMA and metadata.get("transport") == "crossbeam-channel"
            and summary.get("mode") == mode, "cohort only accepts its named Crossbeam experiment")
    receipt = evidence.build_receipt(files["build.json"], mode)
    require(receipt.get("source_dirty") is False, "cohort requires clean frozen source")
    summary = evidence.bind_build(summary, receipt)
    session = evidence.read_json(files["session.json"])
    summary = evidence.attach_execution(summary, session, fresh_boot_observed=True)
    capture = evidence.read_json(files["capture.json"])
    require(isinstance(capture, dict) and capture.get("schema") == "channel-progress-capture-v1",
            "unknown capture record")
    for key in ("capture_complete", "one_command_per_boot", "fresh_boot_observed"):
        require(evidence.boolean(capture, key), f"incomplete capture: {key}")
    require("failure_stage" in capture and capture["failure_stage"] is None
            and "error" in capture and capture["error"] is None
            and "serial_restore_error" not in capture, "capture recorded failure or incomplete cleanup")
    for key in ("build_id", "image_sha256"):
        require(capture.get(key) == receipt[key] == session.get(key),
                f"capture/session/build identity differs: {key}")
    require(capture.get("mode") == mode
            and capture.get("command") == f"channel_progress {mode}",
            "capture describes a different command or mode")
    require(capture.get("runtime_kind") == summary["runtime_kind"],
            "capture and session execution kinds differ")
    require(capture.get("outcome") == summary["outcome"]
            and evidence.boolean(capture, "progress_contract_satisfied")
            == summary["progress_contract_satisfied"], "capture outcome differs from raw replay")
    require(evidence.boolean(capture, "boot_rom_digest_warning_observed")
            == structure["boot_rom_digest_warning_observed"], "boot warning differs from raw capture")
    for key in ("physical_timing_qualified", "priority_progress_qualified"):
        require(evidence.boolean(capture, key) is False, f"unsupported capture qualification: {key}")
    for key in ("physical_execution_verified", "physical_progress_qualified", "timing_qualified",
                "hard_realtime_bound"):
        if key in capture:
            require(evidence.boolean(capture, key) is False,
                    f"unsupported capture qualification: {key}")
    if summary["runtime_kind"] == "qemu":
        require("emulator_returncode_before_cleanup" in capture
                and capture["emulator_returncode_before_cleanup"] is None,
                "emulator exited before capture cleanup")
        require(receipt["platform"] == "mps2-an521-mock"
                and receipt["target"] == "thumbv8m.main-nuttx-eabi"
                and session.get("board") == "mps2-an521", "virtual capture target identity differs")
        command = capture.get("emulator_command")
        require(isinstance(command, list) and all(isinstance(value, str) for value in command),
                "missing emulator launch record")
        require(command.count("-machine") == command.count("-kernel") == 1,
                "ambiguous emulator machine or image")
        machine_index, image_index = command.index("-machine") + 1, command.index("-kernel") + 1
        require(machine_index < len(command) and image_index < len(command)
                and command[machine_index] == "mps2-an521"
                and Path(command[image_index]).as_posix().endswith("/" + receipt["elf_file"]),
                "emulator launch does not name the expected target image")
    stored = evidence.read_json(files["analysis.json"])
    legacy_physical_claim = _stored_analysis(stored, summary)
    result = summary["result"]
    case = {
        "mode": mode, "run_id": summary["run_id"], "build_id": receipt["build_id"],
        "image_sha256": receipt["image_sha256"], "hook_stage": metadata["hook_stage"],
        "outcome": summary["outcome"], "completed_without_repair": summary["completed_without_repair"],
        "progress_contract_satisfied": summary["progress_contract_satisfied"],
        "recovery_applied": result["recovery_applied"],
        "threads_joined": result["threads_joined"], "watchdog_joined": result["watchdog_joined"],
        "messages_sent": result["messages_sent"], "messages_received": result["messages_received"],
        "initial_select_wait_verified": result["initial_select_wait_verified"],
        "selection_registered_arms": result["selection_registered_arms"],
        "independent_wake_consumed": result["independent_wake_consumed"],
        "evidence_sha256": {name: evidence.sha256(path) for name, path in files.items()},
        "legacy_operator_physical_claim_ignored": legacy_physical_claim,
        "physical_execution_verified": False,
    }
    identity = {key: receipt[key] for key in IDENTITY_FIELDS}
    identity["crossbeam_originals"] = {
        name: {key: item[key] for key in ("version", "archive_sha256", "original_tree_sha256")}
        for name, item in receipt["crossbeam_receipt"]["crates"].items()
    }
    identity.update(runtime_kind=summary["runtime_kind"], execution_evidence=summary["execution_evidence"],
                    board=session["board"])
    if summary["runtime_kind"] == "physical":
        identity.update(board_serial=session["board_serial"], active_cores=session["active_cores"])
    return case, identity


def analyze_cohort(root):
    root = Path(root).resolve(strict=True)
    require(root.is_dir(), "cohort root is not a directory")
    names = {path.name for path in root.iterdir() if path.is_dir() or path.is_symlink()}
    missing, extra = set(MODES) - names, names - set(MODES)
    require(not missing and not extra,
            f"cohort requires exactly six Crossbeam cases; missing={sorted(missing)}, unexpected={sorted(extra)}")
    cases, identity, paths, builds = [], None, set(), set()
    for mode in MODES:
        case_directory = directory(root, mode)
        run = directory(case_directory, "run")
        require(run not in paths, "duplicate case capture directory")
        paths.add(run)
        case, current = replay_case(run, mode)
        require(case["build_id"] not in builds, "duplicate firmware build across required cases")
        builds.add(case["build_id"])
        if identity is None:
            identity = current
        else:
            differing = [key for key in set(identity) | set(current) if identity.get(key) != current.get(key)]
            require(not differing, f"mixed cohort at {mode}: {', '.join(sorted(differing))}")
        case["directory"] = f"{mode}/run"
        cases.append(case)
    blocking = [case["mode"] for case in cases if not case["completed_without_repair"]]
    controls = all(case["completed_without_repair"] for case in cases if case["mode"] in CONTROLS)
    result = {
        "schema": SCHEMA, "contract": "crossbeam-0.5.17-fixed-priority-six-cases-v1",
        "cohort_complete": True, "diagnostic_valid": True, "matched_identity": identity,
        "matched_identity_sha256": evidence.canonical(identity), "required_modes": list(MODES),
        "cases": cases, "blocking_cases": blocking,
        "controls_completed_without_repair": controls,
        "all_cases_completed_without_repair": not blocking,
        "progress_contract_satisfied": not blocking,
        "outcome": "complete-without-repair" if not blocking else "complete-with-progress-failures",
        "runtime_kind": identity["runtime_kind"], "execution_evidence": identity["execution_evidence"],
        "physical_execution_verified": False, "physical_progress_qualified": False,
        "priority_progress_qualified": False, "physical_timing_qualified": False,
        "timing_qualified": False, "hard_realtime_bound": False,
        "scope": "Six named Crossbeam interleavings only; no production adoption, timing or WCET qualification.",
    }
    if identity["runtime_kind"] == "physical":
        result["physical_evidence_scope"] = (
            "Operator-declared physical cohort; device identity, flash/readback and exclusive "
            "ownership are not independently verified.")
    return result


def require_no_repair(summary):
    require(summary.get("schema") == SCHEMA and summary.get("cohort_complete") is True
            and summary.get("diagnostic_valid") is True
            and summary.get("all_cases_completed_without_repair") is True,
            "cohort no-repair gate refused: every required Crossbeam case must complete without repair")


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
        result = analyze_cohort(args.cohort_directory)
    except (ValueError, OSError, KeyError) as error:
        print(f"Crossbeam cohort rejected: {error}", file=sys.stderr)
        return 1
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    try:
        if args.require_no_repair:
            require_no_repair(result)
        if args.require_physical:
            require_physical(result)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
