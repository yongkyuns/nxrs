#!/usr/bin/env python3
"""Revalidate retained P2 evidence without executing a binary or contacting a board.

Exit zero means the archived evidence is internally consistent. A retained
capture failure stays a failure; acceptance policy belongs to the calling corpus.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("event_path_replay_capture", HERE / "capture.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)
require = capture.require


def read_json(path):
    def invalid_constant(value):
        raise ValueError("invalid JSON constant: " + value)
    return json.loads(capture.bounded_bytes(path, capture.MAX_RECEIPT_BYTES),
                      object_pairs_hook=capture.analysis.unique_keys,
                      parse_constant=invalid_constant)


def hash_string(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
            "invalid retained SHA256")
    return value


def contained(root, name, *, directory=False):
    capture.portable_path(name, directory=directory)
    path = (root / name).resolve(strict=True)
    require(path.is_relative_to(root) and (path.is_dir() if directory else path.is_file()),
            "retained path escapes its artifact root or has the wrong type")
    return path


def storage_root(item, directory, artifact_root):
    mode = item.get("mode")
    require(mode in {"standalone", "shared"}, "unknown retained artifact mode")
    if mode == "standalone":
        require(item.get("capture_directory") == ".", "standalone capture directory mismatch")
        return directory
    require(artifact_root is not None, "shared evidence requires an explicit artifact root")
    root = Path(artifact_root).resolve(strict=True)
    require(root.is_dir() and directory.is_relative_to(root), "capture is outside shared artifact root")
    require(contained(root, item.get("capture_directory"), directory=True) == directory,
            "shared capture directory mismatch")
    return root


def check_calibration(evidence, session, files, directory):
    require(isinstance(evidence, dict) and set(evidence) == {
        "status", "original_file", "retained_file", "actual_sha256", "claimed_sha256", "error",
    }, "invalid retained calibration manifest")
    status = evidence["status"]
    require(status in {"not-applicable", "not-supplied", "retained", "unavailable"},
            "unknown calibration retention status")
    if status == "not-applicable":
        require(all(evidence[key] is None for key in evidence if key != "status"),
                "inapplicable calibration has evidence claims")
        return
    require(isinstance(session, dict), "calibration has no original session")
    original = session.get("clock_calibration")
    if status == "not-supplied":
        require(original is None and all(evidence[key] is None for key in evidence if key != "status"),
                "missing calibration disagrees with original session")
        return
    require(original is not None, "calibration retention has no supplied record")
    require(evidence["original_file"] == (original.get("evidence_file") if isinstance(original, dict) else None)
            and evidence["claimed_sha256"] == (original.get("evidence_sha256") if isinstance(original, dict) else None),
            "retained calibration mapping differs from original claims")
    if status == "retained":
        actual = hash_string(evidence["actual_sha256"])
        name = evidence["retained_file"]
        require(name == "calibration/evidence/" + actual and files.get(name) == actual
                and evidence["error"] is None, "calibration trace is not bound by the manifest")
        require(contained(directory, name).stat().st_size <= capture.MAX_CALIBRATION_BYTES,
                "retained calibration exceeds its size bound")
    else:
        require(evidence["retained_file"] is None and evidence["actual_sha256"] is None
                and isinstance(evidence["error"], str) and evidence["error"],
                "unavailable calibration has inconsistent retention claims")
        require(not (directory / "calibration/unavailable-evidence").exists(),
                "unavailable calibration path unexpectedly exists")


def replay(directory: Path, artifact_root: Path | None = None) -> dict:
    directory = Path(directory).resolve(strict=True)
    require(directory.is_dir(), "capture directory is missing")
    manifest = read_json(contained(directory, "evidence.json"))
    require(isinstance(manifest, dict) and manifest.get("schema") == "event-path-evidence-v1"
            and set(manifest) == {"schema", "files_sha256", "build", "host_binary", "calibration"},
            "unknown evidence manifest")
    files = manifest["files_sha256"]
    require(isinstance(files, dict) and 2 <= len(files) <= 16
            and {"console.log", "capture.json"} <= files.keys(), "incomplete capture file inventory")
    for name, expected in files.items():
        path = contained(directory, name)
        require(path.stat().st_size <= capture.MAX_ARTIFACT_BYTES, "retained file exceeds its size bound")
        require(capture.digest(path) == hash_string(expected), "retained file hash mismatch: " + name)
    for name in ("analysis.json", "session.json", "build.json"):
        require((directory / name).exists() == (name in files), "unbound capture file: " + name)

    report = read_json(directory / "capture.json")
    require(isinstance(report, dict) and report.get("schema") == "event-path-capture-v1",
            "unknown capture receipt")
    complete = report.get("capture_complete")
    require(type(complete) is bool, "invalid capture completion status")
    require(report.get("evidence_manifest") == "evidence.json", "capture refers to a different evidence manifest")
    require(report.get("console_sha256") == files["console.log"], "capture console identity mismatch")
    for key in ("physical_timing_qualified", "priority_progress_qualified"):
        require(report.get(key) is False, "retained capture attempted qualification promotion")
    require(report.get("physical_execution_verified", False) is False,
            "retained capture attempted physical-execution promotion")
    if complete:
        require(report.get("failure_stage") is None and report.get("error") is None
                and not report.get("cleanup_errors") and "analysis.json" in files,
                "complete capture contains failure or lacks its analysis")
    else:
        require(isinstance(report.get("failure_stage"), str) and report["failure_stage"]
                and isinstance(report.get("error"), str) and report["error"],
                "failed capture lacks its failure record")

    receipt = None
    build = manifest["build"]
    if build is not None:
        require(isinstance(build, dict) and set(build) == {
            "mode", "directory", "capture_directory", "receipt_sha256",
        }, "invalid retained build manifest")
        base = storage_root(build, directory, artifact_root)
        if build["mode"] == "standalone":
            require(build["directory"] == "build", "unexpected standalone build location")
        build_directory = contained(base, build["directory"], directory=True)
        expected = hash_string(build["receipt_sha256"])
        require(capture.digest(contained(build_directory, "build.json")) == expected
                and files.get("build.json") == expected
                and report.get("build_receipt_sha256") == expected,
                "retained build receipt identity mismatch")
        receipt = capture.build_receipt(build_directory)
        require(report.get("image_sha256") == receipt["image_sha256"], "capture image identity mismatch")

    binary = manifest["host_binary"]
    if binary is not None:
        require(isinstance(binary, dict) and set(binary) == {
            "mode", "file", "capture_directory", "sha256", "bytes",
        }, "invalid retained executable manifest")
        base = storage_root(binary, directory, artifact_root)
        path = contained(base, binary["file"])
        expected = hash_string(binary["sha256"])
        require(type(binary["bytes"]) is int and 0 <= binary["bytes"] <= capture.MAX_ARTIFACT_BYTES
                and path.stat().st_size == binary["bytes"] and capture.digest(path) == expected
                and report.get("binary_sha256") == expected, "retained executable identity mismatch")
        if binary["mode"] == "standalone":
            require(binary["file"] == "binary/event-path-perf" and files.get(binary["file"]) == expected,
                    "standalone executable absent from file inventory")
    runtime = report.get("runtime_kind")
    require(runtime in {"host", "qemu", "physical", "serial"}, "unknown retained runtime")
    require((runtime == "host" and build is None) or (runtime != "host" and binary is None),
            "executable evidence disagrees with capture runtime")
    if complete:
        require((runtime == "host" and binary is not None and report.get("returncode") == 0)
                or (runtime in {"qemu", "physical"} and receipt is not None),
                "complete capture has no matching executable")

    session = None
    if "session.json" in files:
        require(report.get("session_sha256") == files["session.json"], "original session hash mismatch")
        try:
            session = read_json(directory / "session.json")
            require(isinstance(session, dict), "original session is not an object")
        except (ValueError, TypeError):
            if complete or "analysis.json" in files:
                raise
            session = None
    check_calibration(manifest["calibration"], session, files, directory)
    if manifest["calibration"]["status"] == "not-applicable":
        require(runtime in {"host", "qemu"} or (not complete and report.get("failure_stage") == "setup"),
                "physical capture skipped its calibration retention status")

    raw = capture.bounded_bytes(directory / "console.log", capture.console.MAX_LOG_BYTES)
    summary = facts = None
    analysis_error = None
    try:
        summary = capture.analysis.analyze(raw.decode("utf-8", errors="strict"))
        metadata = summary["metadata"]
        require(report.get("case") == metadata["case"], "capture and raw case differ")
        payload = "imu16" if metadata["payload"] == "imu-sample" else "bytes16"
        arguments = capture.command_for(metadata["case"], metadata["samples"], metadata["warmup"], payload)
        if runtime == "host":
            command = report.get("command")
            require(isinstance(command, list) and len(command) == 5 and command[1:] == arguments
                    and metadata["runtime_kind"] == "host-functional-only", "host command/runtime mismatch")
        else:
            require(report.get("command") == "event_path_perf " + " ".join(arguments),
                    "firmware command differs from raw experiment")
            require(receipt is not None and session is not None, "missing retained firmware/session association")
            observed = capture.analysis.firmware_frame(raw, report["command"])
            fresh = observed["fresh_boot_observed"]
            warning = observed["boot_rom_digest_warning_observed"]
            require(all(report.get(key) == value for key, value in observed.items()),
                    "recorded boot observations differ from raw console")
            facts = capture.associate(summary, receipt,
                                      capture.retained_session(session, manifest["calibration"]),
                                      directory if session.get("runtime_kind") == "physical" else None,
                                      fresh, warning)
            for key, value in facts.items():
                require(report.get(key) == value, "capture association differs on " + key)
            summary["capture_qualification"] = facts
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        if complete or "analysis.json" in files:
            raise
        summary = None
        analysis_error = str(error)
    if "analysis.json" in files:
        require(summary == read_json(directory / "analysis.json"), "stored analysis differs from raw evidence replay")
    return {
        "schema": "event-path-replay-v1", "evidence_valid": True,
        "capture_complete": complete, "runtime_kind": runtime, "case": report.get("case"),
        "analysis_replayed": summary is not None, "stored_analysis_verified": "analysis.json" in files,
        "outcome": "complete-capture" if complete else "retained-capture-failure",
        "failure_stage": report.get("failure_stage"), "error": report.get("error"),
        "raw_analysis_error": analysis_error,
        "build_verified": receipt is not None, "binary_verified": binary is not None,
        "build_id": receipt["build_id"] if receipt else None,
        "image_sha256": receipt["image_sha256"] if receipt else None,
        "binary_sha256": binary["sha256"] if binary else None,
        "clock_calibration_qualified": bool(facts and facts["clock_calibration_qualified"]),
        "physical_timing_qualified": False, "priority_progress_qualified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--artifact-root", type=Path,
                        help="explicit relocated archive root for a shared build or executable")
    args = parser.parse_args()
    try:
        result = replay(args.directory, args.artifact_root)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"event-path evidence replay refused: {error}\n")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
