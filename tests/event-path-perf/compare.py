#!/usr/bin/env python3
"""Compare three explicit, matched P2 captures without pooling or qualification.

Each input must independently replay as a complete capture. Only the seven
named groups below are supported; this module never discovers or executes runs.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("event_path_comparison_replay", HERE / "replay.py")
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)
capture = replay.capture
require = capture.require

GROUPS = {
    "ready-try": tuple(f"ready-{backend}-try" for backend in ("std", "messaging", "crossbeam")),
    "ready-send": tuple(f"ready-{backend}-send" for backend in ("std", "messaging", "crossbeam")),
    "blocked-try": tuple(f"blocked-{backend}-try" for backend in ("std", "messaging", "crossbeam")),
    "blocked-send": tuple(f"blocked-{backend}-send" for backend in ("std", "messaging", "crossbeam")),
    "full-rejection": tuple(f"full-{backend}" for backend in ("std", "messaging", "crossbeam")),
    "ready-selection": ("ready-crossbeam-try", "ready-select-one", "ready-select-three"),
    "blocked-selection": ("blocked-crossbeam-try", "blocked-select-one", "blocked-select-three"),
}

CONDITION_FIELDS = (
    "family", "admission", "payload", "payload_bytes", "payload_alignment", "capacity",
    "provider_count", "samples", "warmup", "counts_include_warmup", "runtime_kind", "build_id",
    "clock_hz", "clock_source", "counter_bits", "counter_native_bits", "monotonic_resolution_ns",
    "clock_source_request", "crossbeam_version", "witness_hooks", "worker_stack", "blocked_state_name",
    "active_arm", "release_schedule", "release_period_ns", "warmup_scope", "timer_delay_ns",
)
LEGACY_DEFAULTS = {
    "release_period_ns": 0, "warmup_scope": "sequential-prefix", "timer_delay_ns": 0,
}
SCHEDULING_FIELDS = (
    "sender_policy", "sender_priority", "receiver_policy", "receiver_priority",
    "single_core_verified", "peer_ready_verified",
)
PHYSICAL_CONTEXT_FIELDS = (
    "board", "board_serial", "active_cores", "cpu_clock_hz", "cache_state", "memory_placement",
    "power_management", "external_load", "flash_verification",
)
CALIBRATION_FIELDS = (
    "qualified", "status", "method", "reference_type", "reference_duration_ns",
    "reference_accuracy_ppm", "measured_cpu_hz", "configured_cpu_hz",
    "frequency_difference_ppm", "evidence_sha256",
)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def read_member(directory: Path, artifact_root: Path | None):
    manifest_path = replay.contained(directory, "evidence.json")
    evidence_sha256 = capture.digest(manifest_path)
    verified = replay.replay(directory, artifact_root)
    require(verified["evidence_valid"] is True and verified["capture_complete"] is True
            and verified["stored_analysis_verified"] is True,
            "comparison requires a complete, independently replayed capture")
    evidence = replay.read_json(manifest_path)
    require(capture.digest(manifest_path) == evidence_sha256,
            "evidence manifest changed during comparison")

    def retained(name):
        path = replay.contained(directory, name)
        digest = evidence["files_sha256"].get(name)
        require(capture.digest(path) == digest, "comparison file identity mismatch: " + name)
        record = replay.read_json(path)
        require(capture.digest(path) == digest, "comparison file changed while reading: " + name)
        return record

    report = retained("capture.json")
    analysis = retained("analysis.json")
    runtime = report["runtime_kind"]
    receipt = retained("build.json") if runtime != "host" else None
    session = retained("session.json") if runtime != "host" else None
    return {
        "directory": directory, "evidence_sha256": evidence_sha256, "evidence": evidence,
        "verified": verified, "report": report, "analysis": analysis,
        "receipt": receipt, "session": session,
    }


def identity(member):
    report, receipt = member["report"], member["receipt"]
    if receipt is None:
        return {"kind": "host-executable", "binary_sha256": report["binary_sha256"]}
    result = {
        "kind": "firmware-build", "build_receipt_sha256": report["build_receipt_sha256"],
        **{key: receipt[key] for key in (
            "build_id", "image_sha256", "elf_sha256", "config_sha256", "platform", "target", "sdk",
            "compiler_version", "optimization", "std_array_sha256", "std_library_tree_sha256",
        )},
    }
    if "boot_image_profile" in receipt:
        result["boot_image_profile"] = receipt["boot_image_profile"]
    return result


def context(member):
    runtime = member["report"]["runtime_kind"]
    if runtime == "host":
        return {"runtime_kind": runtime, "host_identity": "not-recorded"}
    session = member["session"]
    if runtime == "physical":
        return {"runtime_kind": runtime,
                **{key: session[key] for key in PHYSICAL_CONTEXT_FIELDS}}
    require(runtime == "qemu", "unsupported comparison runtime")
    argv = member["report"].get("emulator_command")
    require(isinstance(argv, list) and len(argv) == 14
            and all(isinstance(value, str) and value for value in argv)
            and argv[1:4] == ["-machine", "mps2-an521", "-kernel"]
            and argv[5:] == ["-display", "none", "-serial", "stdio", "-monitor", "none",
                            "-nic", "none", "-no-reboot"]
            and session.get("board") == "mps2-an521", "unexpected declared QEMU context")
    # The original kernel pathname is not a portable identity. Replay already
    # verified the retained ELF; preserve all other declared command choices.
    normalized = list(argv)
    normalized[4] = "sha256:" + member["receipt"]["elf_sha256"]
    return {
        "runtime_kind": runtime, "board": session["board"],
        "emulator_program_name": Path(argv[0]).name,
        "emulator_command_sha256": canonical_hash(normalized),
    }


def conditions(member):
    metadata = member["analysis"]["metadata"]
    return {key: metadata[key] if key in metadata else LEGACY_DEFAULTS[key]
            for key in CONDITION_FIELDS}


def scheduling(member):
    observed = member["analysis"]["scheduling"]
    return {**{key: observed[key] for key in SCHEDULING_FIELDS},
            "same_thread": observed["sender_tid"] == observed["receiver_tid"]}


def match(label, values):
    reference = values[0]
    for value in values[1:]:
        require(value.keys() == reference.keys(), "unmatched " + label + " fields")
        for key in reference:
            require(value[key] == reference[key] and type(value[key]) is type(reference[key]),
                    "unmatched " + label + ": " + key)
    return reference


def member_result(member):
    analysis, report, evidence = member["analysis"], member["report"], member["evidence"]
    metadata = analysis["metadata"]
    result = analysis["result"]
    clock = report.get("clock_calibration", {"qualified": False, "status": "not-applicable"})
    return {
        "case": metadata["case"], "run_id": analysis["run_id"],
        "evidence_sha256": member["evidence_sha256"],
        **{name + "_sha256": evidence["files_sha256"][name + ".json"]
           for name in ("capture", "analysis")},
        "console_sha256": report["console_sha256"],
        "session_sha256": report.get("session_sha256"),
        "measurement_session": member["session"].get("measurement_session") if member["session"] else None,
        **{key: metadata[key] for key in ("transport", "admission", "receive_api")},
        "topology": {key: metadata[key] for key in (
            "provider_count", "selector_arms", "capacity", "stop_capacity", "important_capacity", "active_arm",
        )},
        "counts": {key: result[key] for key in (
            "sample_count", "attempted", "admitted", "rejected", "handled", "checksum", "retained_checksum",
        )},
        "raw_ticks": analysis["raw_ticks"],
        "qualification": {
            "structural_valid": True, "physical_execution_verified": False,
            "fresh_boot_observed": report.get("fresh_boot_observed"),
            "boot_rom_digest_warning_observed": report.get("boot_rom_digest_warning_observed"),
            "clock_calibration": {key: clock[key] for key in CALIBRATION_FIELDS if key in clock},
            "calibration_retention_status": evidence["calibration"]["status"],
            "calibration_trace_sha256": evidence["calibration"]["actual_sha256"],
            "timing_qualified": False, "physical_timing_qualified": False,
            "priority_progress_qualified": False,
            "timing_qualification_blockers": report.get("timing_qualification_blockers", []),
        },
    }


def compare(group: str, directories: list[Path], artifact_root: Path | None = None) -> dict:
    require(isinstance(group, str) and group in GROUPS, "unknown fixed comparison group")
    require(isinstance(directories, (list, tuple)) and len(directories) == 3,
            "comparison requires exactly three explicit captures")
    paths = [Path(directory).resolve(strict=True) for directory in directories]
    require(len(set(paths)) == 3, "comparison contains duplicate capture directories")
    members = [read_member(directory, artifact_root) for directory in paths]
    by_case = {member["analysis"]["metadata"]["case"]: member for member in members}
    require(len(by_case) == 3 and set(by_case) == set(GROUPS[group]),
            "comparison has missing, duplicate or unexpected named cases")
    members = [by_case[case] for case in GROUPS[group]]
    runtimes = {member["report"]["runtime_kind"] for member in members}
    require(len(runtimes) == 1, "comparison mixes runtime kinds")
    matched_identity = match("executable identity", [identity(member) for member in members])
    matched_context = match("declared context", [context(member) for member in members])
    matched_conditions = match("experiment conditions", [conditions(member) for member in members])
    matched_scheduling = match("logical scheduling", [scheduling(member) for member in members])
    for member in members:
        require(capture.digest(replay.contained(member["directory"], "evidence.json"))
                == member["evidence_sha256"], "evidence manifest changed during comparison")
    selection = group.endswith("-selection")
    return {
        "schema": "event-path-comparison-v1", "group": group, "matched": True,
        "runtime_kind": members[0]["report"]["runtime_kind"],
        "identity": matched_identity, "context": matched_context,
        "conditions": matched_conditions, "scheduling": matched_scheduling,
        "members": [member_result(member) for member in members],
        "comparison_kind": "selection-topology" if selection else "transport-call",
        "interpretation": (
            "One-arm select_biased! expands to direct recv in pinned Crossbeam. "
            "Three-arm selection adds connected idle stop/important queues; its difference is not selector-only cost."
            if selection else
            "Send admits into nonfull queues; this group does not measure a blocked producer's release."
            if group.endswith("-send") else
            "Full rejection times only the rejected try_send; validation drains are untimed."
            if group == "full-rejection" else
            "Compare the same accepted try_send operation through raw std, the actual messaging facade and pinned Crossbeam."
        ),
        "scope": (
            "Three individual finite-case raw-tick distributions with matching recorded conditions. "
            "Samples and quantiles are not pooled or subtracted; no speedup, ranking or production acceptance follows. "
            "Host environment, emulator binary identity and physical execution location are not independently verified. "
            "Per-capture clock calibration remains separate; P0 physical timing and P1 no-repair progress gates remain open."
        ),
        "execution_context_verified": False, "physical_execution_verified": False,
        "timing_qualified": False, "physical_timing_qualified": False,
        "priority_progress_qualified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=GROUPS)
    parser.add_argument("captures", metavar="CAPTURE", type=Path, nargs=3)
    parser.add_argument("--artifact-root", type=Path,
                        help="explicit archive root for captures with a shared build or executable")
    args = parser.parse_args()
    try:
        result = compare(args.group, args.captures, args.artifact_root)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"event-path comparison refused: {error}\n")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
