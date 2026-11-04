#!/usr/bin/env python3
"""Capture one P2 case, preserving raw failures and separate qualification fields.

Console I/O, fresh-boot detection and independent-clock validation reuse P0
read-only. This runner never flashes a device or grants production acceptance.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import pty
import re
import subprocess
import sys
import termios
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE = ROOT / "tests/std-channel-perf"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# P0 capture imports its own analyzer by a legacy top-level name. Resolve that
# normal import explicitly, never swap implementations or patch shared globals.
if "analyze" in sys.modules and Path(sys.modules["analyze"].__file__).resolve() != BASE / "analyze.py":
    raise ImportError("P0 capture requires its own analyzer module")
sys.path.insert(0, str(BASE))
try:
    console = load("event_path_baseline_console", BASE / "capture.py")
finally:
    sys.path.pop(0)
analysis = load("event_path_analysis", HERE / "analyze.py")
calibration = load("event_path_baseline_calibration", BASE / "analyze.py")
builder = load("event_path_capture_build_contract", HERE / "build.py")
require = calibration.require
checked_file = calibration.checked_file

MAX_ARTIFACTS = 64
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_BUILD_BYTES = 256 * 1024 * 1024
MAX_CALIBRATION_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 600


def digest(path):
    hashed = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hashed.update(chunk)
    return hashed.hexdigest()


def portable_path(value, *, directory=False):
    require(isinstance(value, str) and value and "\\" not in value and "\x00" not in value,
            "invalid portable artifact path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and path.as_posix() == value
            and (value != "." or directory), "artifact path must be normalized and relative")
    return value


def bounded_bytes(path, limit):
    with Path(path).open("rb") as source:
        data = source.read(limit + 1)
    require(len(data) <= limit, "evidence file exceeds retention size bound")
    return data


def copy_verified(source, destination, expected, limit):
    """Copy bounded input bytes, then verify the retained destination itself."""
    data = bounded_bytes(source, limit)
    require(hashlib.sha256(data).hexdigest() == expected, "artifact changed before retention")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        output.write(data)
    require(digest(destination) == expected, "retained artifact hash mismatch")
    return len(data)


def command_for(case, samples, warmup, payload):
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", case) is not None, "invalid case name")
    require(case in analysis.CASES, "unknown event-path case")
    require(type(samples) is int and 1 <= samples <= 4096, "invalid sample count")
    require(type(warmup) is int and 0 <= warmup <= 1024, "invalid warmup count")
    require(payload in {"bytes16", "imu16"}, "invalid payload profile")
    if case.startswith("fanin-"):
        require(samples <= 256 and warmup <= 64, "finite fan-in storage bound")
    if case == "timer-provider-messaging":
        require(samples <= 128 and warmup <= 32, "timer-provider storage bound")
    if case.endswith("-saturation"):
        providers = analysis.CASES[case][5]
        capacity = 8 if payload == "bytes16" else 16
        require(providers * (samples + warmup) > capacity, "saturation attempts must exceed capacity")
    return [case, str(samples), str(warmup), payload]


def serial_budget(case, samples):
    """Conservative serial estimate; output follows the bounded fixture work."""
    rows = samples * analysis.CASES[case][5]
    row_bytes = 1536 if case == "timer-provider-messaging" else 768
    estimated_bytes = 16 * 1024 + rows * row_bytes
    # 115200 baud, 8N1 (ten wire bits per byte), with 25% output allowance.
    wire_seconds = math.ceil(estimated_bytes * 10 * 1.25 / 115200)
    return {"baud": 115200, "wire_bits_per_byte": 10, "retained_rows": rows,
            "estimated_bytes_per_row": row_bytes, "estimated_console_bytes": estimated_bytes,
            "wire_allowance_percent": 25, "wire_budget_seconds": wire_seconds,
            "setup_budget_seconds": 15, "fixture_budget_seconds": 30,
            "minimum_timeout_seconds": 15 + 30 + wire_seconds}


def choose_timeout(args):
    budget = serial_budget(args.case, args.samples) if args.runtime == "serial" else None
    requested = getattr(args, "timeout", None)
    minimum = budget["minimum_timeout_seconds"] if budget else 1
    timeout = requested if requested is not None else (max(30, minimum) if budget else 30)
    require(type(timeout) in (int, float) and math.isfinite(timeout)
            and 1 <= timeout <= MAX_TIMEOUT_SECONDS, "timeout must be 1..600 seconds")
    require(timeout >= minimum,
            f"serial timeout must be at least {minimum} seconds for the 115200-baud output budget")
    return timeout, budget


def build_receipt(directory):
    record = json.loads(bounded_bytes(checked_file(directory, "build.json"), MAX_RECEIPT_BYTES),
                        object_pairs_hook=calibration.unique_keys)
    require(isinstance(record, dict), "invalid build receipt")
    require(record.get("schema") == "event-path-build-v1" and record.get("status") == "complete", "incomplete event-path build")
    require(record.get("mode") == "uninstrumented" and record.get("hook_stage") == 0, "instrumented timing image")
    require(record.get("command") == "event_path_perf", "wrong firmware command")
    require(re.fullmatch(r"[0-9a-f]{64}", record.get("build_id", "")) is not None, "missing build identity")
    require(builder.build_identity(record) == record["build_id"], "build identity differs from frozen provenance")
    artifacts = record.get("artifacts_sha256")
    require(isinstance(artifacts, dict) and 1 <= len(artifacts) <= MAX_ARTIFACTS, "missing or oversized artifact inventory")
    total_bytes = 0
    for name, expected in artifacts.items():
        portable_path(name)
        require(PurePosixPath(name).parts[0] != "build.json", "artifact namespace collides with build receipt")
        require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
                "invalid artifact hash")
        path = checked_file(directory, name)
        size = path.stat().st_size
        total_bytes += size
        require(size <= MAX_ARTIFACT_BYTES and total_bytes <= MAX_BUILD_BYTES, "build artifact retention bound")
        require(digest(path) == expected, "changed build artifact: " + name)
    for key in ("image", "elf", "config"):
        name = portable_path(record.get(key + "_file"))
        require(name in artifacts, "required artifact missing from inventory: " + key)
        path = checked_file(directory, name)
        require(digest(path) == record.get(key + "_sha256"), "wrong " + key + " identity")
    for key in ("std_array_sha256", "std_array_original_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", str(record.get(key, ""))) is not None, "missing " + key)
    require(record["std_array_sha256"] == record["std_array_original_sha256"], "instrumented std array")
    require(record.get("compiler_version") == record["compiler_identity"]["version"], "compiler identity mismatch")
    hook_name = record.get("hook_receipt")
    registry_name = record.get("crossbeam_source_receipt")
    require(hook_name in artifacts and registry_name in artifacts, "source proofs absent from artifact inventory")
    hook = calibration.read_json(checked_file(directory, hook_name))
    require(isinstance(hook, dict) and hook.get("schema") == "std-channel-hook-v1" and hook.get("mode") == "latency"
            and hook.get("hook_stage") == 0 and hook.get("channel_instrumented") is False,
            "instrumented or invalid std source proof")
    require(hook.get("original", {}).get("sha256") == record["std_array_original_sha256"]
            and hook.get("selected", {}).get("sha256") == record["std_array_sha256"], "std source proof mismatch")
    library_name = (PurePosixPath(hook_name).parent / "std-library-sources.json").as_posix()
    require(library_name in artifacts, "std library inventory absent from artifact inventory")
    library = calibration.read_json(checked_file(directory, library_name))
    require(isinstance(library, dict) and library, "invalid std library inventory")
    for name, expected in library.items():
        portable_path(name)
        require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
                "invalid std library source hash")
    tree_hash = hashlib.sha256(json.dumps(library, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    require(hook.get("compiler") == record["compiler_identity"], "std compiler proof mismatch")
    require(hook.get("library_tree_sha256") == record["std_library_tree_sha256"] == tree_hash,
            "std library tree proof mismatch")
    require(library.get("std/src/sync/mpmc/array.rs") == record["std_array_sha256"], "std library array proof mismatch")
    registry = calibration.read_json(checked_file(directory, registry_name))
    require(registry == record.get("crossbeam") and registry.get("schema") == "event-path-registry-sources-v1", "Crossbeam source proof mismatch")
    packages = registry.get("packages")
    require(isinstance(packages, list) and len(packages) == len(builder.REGISTRY_PINS)
            and all(isinstance(item, dict) for item in packages), "missing Crossbeam source packages")
    require({item.get("name") for item in packages} == set(builder.REGISTRY_PINS), "wrong Crossbeam source packages")
    for package in packages:
        version, checksum = builder.REGISTRY_PINS[package["name"]]
        require(package.get("version") == version and package.get("registry_checksum") == checksum
                and package.get("uninstrumented_archive_match") is True, "instrumented or wrong Crossbeam source")
    builder.boot_image.verify(directory, record)
    return record


def shared_paths(args, original):
    root = getattr(args, "artifact_root", None)
    if root is None:
        return None
    root = Path(root).resolve(strict=True)
    require(root.is_dir(), "artifact root must be a directory")
    capture_path = args.output.resolve()
    original = original.resolve(strict=True)
    require(original.is_relative_to(root) and capture_path.is_relative_to(root),
            "shared artifact and capture must be inside artifact root")
    return (portable_path(original.relative_to(root).as_posix(), directory=True),
            portable_path(capture_path.relative_to(root).as_posix(), directory=True))


def retain_build(args, receipt):
    directory = args.build_directory
    source = checked_file(directory, "build.json")
    raw = bounded_bytes(source, MAX_RECEIPT_BYTES)
    require(json.loads(raw, object_pairs_hook=calibration.unique_keys) == receipt,
            "build receipt changed before retention")
    receipt_hash = hashlib.sha256(raw).hexdigest()
    shared = shared_paths(args, directory)
    if shared is None:
        destination = args.output / "build"
        destination.mkdir()
        (destination / "build.json").write_bytes(raw)
        for name, expected in receipt["artifacts_sha256"].items():
            copy_verified(checked_file(directory, name), destination / name, expected, MAX_ARTIFACT_BYTES)
        require(build_receipt(destination) == receipt and digest(destination / "build.json") == receipt_hash,
                "retained build differs from source receipt")
        retained = {"mode": "standalone", "directory": "build", "capture_directory": ".",
                    "receipt_sha256": receipt_hash}
    else:
        retained = {"mode": "shared", "directory": shared[0], "capture_directory": shared[1],
                    "receipt_sha256": receipt_hash}
    (args.output / "build.json").write_bytes(raw)
    return retained


def retain_binary(args, binary, expected):
    size = binary.stat().st_size
    require(size <= MAX_ARTIFACT_BYTES, "host executable retention bound")
    shared = shared_paths(args, binary)
    if shared is None:
        name = "binary/event-path-perf"
        copy_verified(binary, args.output / name, expected, MAX_ARTIFACT_BYTES)
        (args.output / name).chmod(binary.stat().st_mode & 0o777)
        return {"mode": "standalone", "file": name, "capture_directory": ".",
                "sha256": expected, "bytes": size}
    return {"mode": "shared", "file": shared[0], "capture_directory": shared[1],
            "sha256": expected, "bytes": size}


def retain_calibration(session, source_directory, output):
    record = session.get("clock_calibration")
    evidence = {"status": "not-supplied" if record is None else "unavailable",
                "original_file": None, "retained_file": None, "actual_sha256": None,
                "claimed_sha256": None, "error": None}
    if record is None:
        return evidence
    if not isinstance(record, dict):
        evidence["error"] = "calibration record is not an object"
        return evidence
    evidence["original_file"] = record.get("evidence_file")
    evidence["claimed_sha256"] = record.get("evidence_sha256")
    try:
        source = checked_file(source_directory, record.get("evidence_file"))
        data = bounded_bytes(source, MAX_CALIBRATION_BYTES)
        actual = hashlib.sha256(data).hexdigest()
        name = "calibration/evidence/" + actual
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as retained:
            retained.write(data)
        require(digest(destination) == actual, "retained calibration hash mismatch")
        evidence.update(status="retained", retained_file=name, actual_sha256=actual)
    except (OSError, ValueError, TypeError) as error:
        evidence["error"] = str(error)
    return evidence


def retained_session(session, evidence):
    """Rebase only the evidence path, preserving all supplied calibration claims."""
    rebased = copy.deepcopy(session)
    record = rebased.get("clock_calibration")
    if isinstance(record, dict):
        record["evidence_file"] = (evidence["retained_file"] if evidence["status"] == "retained"
                                   else "calibration/unavailable-evidence")
    return rebased


def read_until(fd, transcript, predicate, deadline, process=None):
    """Keep P0 I/O while checking P2 faults before either prompt is accepted."""
    def checked(data):
        if analysis.faulted(data):
            raise RuntimeError("guest fixture failure or kernel fault")
        return predicate(data)

    result = console.read_until(fd, transcript, checked, deadline, process)
    if analysis.faulted(result):
        raise RuntimeError("guest fixture failure or kernel fault")
    return result


def associate(summary, receipt, session, evidence_directory, fresh_boot, digest_warning):
    """Associate operator identity and clock evidence without certifying execution."""
    metadata = summary["metadata"]
    require(receipt["build_id"] == metadata["build_id"], "running build identity mismatch")
    require(receipt["clock_request"] == metadata["clock_source_request"], "running clock request mismatch")
    require(session.get("schema") == "event-path-session-v1", "wrong event-path session schema")
    runtime = session.get("runtime_kind")
    require(runtime in {"qemu", "physical"}, "unknown firmware runtime")
    require(metadata["runtime_kind"] == "nuttx-unqualified", "firmware runtime mismatch")
    require(session.get("image_sha256") == receipt["image_sha256"], "wrong flashed image")
    require(fresh_boot, "fresh boot must be observed")
    clock = {"qualified": False, "status": "not-applicable"}
    if runtime == "physical":
        for key in ("measurement_session", "board", "board_serial", "cache_state", "memory_placement", "power_management", "external_load", "flash_verification"):
            value = session.get(key)
            require(isinstance(value, str) and value.strip() and value.strip().lower() not in {"unknown", "todo", "unmeasured"} and not value.strip().lower().startswith(("replace", "todo", "<")), "missing physical " + key)
        require(type(session.get("active_cores")) is int and session["active_cores"] == 1, "one active core required")
        calibration.integer(session, "cpu_clock_hz", 1)
        if metadata["clock_source"] in (5, 6):
            require(session["cpu_clock_hz"] == metadata["clock_hz"], "session CPU frequency differs from hardware counter frequency")
        clock = calibration.independent_clock_calibration(summary, receipt, session, evidence_directory)
    return {
        "runtime_kind": runtime,
        # Matching session/image/clock records remain operator-supplied evidence;
        # they do not independently establish where the transcript was executed.
        "physical_execution_verified": False,
        "hardware_identity_origin": "operator-supplied-session-and-image-receipt" if runtime == "physical" else "emulator-command",
        "fresh_boot_observed": True,
        "boot_rom_digest_warning_observed": bool(digest_warning),
        "clock_calibration": clock,
        "clock_calibration_qualified": clock["qualified"],
        "physical_timing_qualified": False,
        "priority_progress_qualified": False,
        "timing_qualification_blockers": ["P0-matched-physical-cohort-review-required"] +
            (["clock-not-independently-calibrated"] if runtime == "physical" and not clock["qualified"] else []) +
            (["rom-digest-warning"] if digest_warning else []) +
            (["emulated-execution"] if runtime == "qemu" else []),
    }


def capture(args):
    arguments = command_for(args.case, args.samples, args.warmup, args.payload)
    timeout, budget = choose_timeout(args)
    require(not args.output.exists(), "output must be new; preserve previous captures")
    args.output.mkdir(parents=True)
    transcript = bytearray()
    report = {"schema": "event-path-capture-v1", "capture_complete": False,
              "runtime_kind": args.runtime, "case": args.case,
              "physical_timing_qualified": False, "priority_progress_qualified": False,
              "timeout_seconds": timeout, "serial_budget": budget,
              "evidence_manifest": "evidence.json",
              "failure_stage": "setup", "error": None}
    evidence = {"schema": "event-path-evidence-v1", "files_sha256": {},
                "build": None, "host_binary": None,
                "calibration": {"status": "not-applicable", "original_file": None,
                                "retained_file": None, "actual_sha256": None,
                                "claimed_sha256": None, "error": None}}
    session = receipt = None
    raw_session = raw_build = None
    fd = slave = process = serial_saved = None
    try:
        if args.runtime == "host":
            binary = args.binary.resolve(strict=True)
            shared_paths(args, binary)
            report["binary_sha256"] = digest(binary)
            evidence["host_binary"] = retain_binary(args, binary, report["binary_sha256"])
            report["command"] = [str(binary), *arguments]
            report["failure_stage"] = "execution"
            completed = subprocess.run(report["command"], capture_output=True, timeout=timeout)
            transcript.extend(completed.stdout)
            transcript.extend(completed.stderr)
            require(len(transcript) <= console.MAX_LOG_BYTES, "console size limit")
            report["returncode"] = completed.returncode
            require(completed.returncode == 0, "host fixture refused or failed")
            require(digest(binary) == report["binary_sha256"], "binary changed during execution")
        else:
            shared_paths(args, args.build_directory)
            raw_build = bounded_bytes(checked_file(args.build_directory, "build.json"), MAX_RECEIPT_BYTES)
            receipt = build_receipt(args.build_directory)
            require(json.loads(raw_build, object_pairs_hook=calibration.unique_keys) == receipt,
                    "build receipt changed during setup")
            report["build_receipt_sha256"] = hashlib.sha256(raw_build).hexdigest()
            evidence["build"] = retain_build(args, receipt)
            require(evidence["build"]["receipt_sha256"] == report["build_receipt_sha256"],
                    "build receipt bytes changed during retention")
            report["image_sha256"] = receipt["image_sha256"]
            report["command"] = "event_path_perf " + " ".join(arguments)
            if args.runtime == "qemu":
                require(receipt["platform"] == "mps2-an521-mock", "QEMU requires MPS2 build")
                elf = checked_file(args.build_directory, receipt["elf_file"])
                argv = [args.qemu, "-machine", "mps2-an521", "-kernel", str(elf), "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
                report["emulator_command"] = argv
                fd, slave = pty.openpty()
                process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                os.close(slave)
                slave = None
                session = {"schema": "event-path-session-v1", "runtime_kind": "qemu", "image_sha256": receipt["image_sha256"], "board": "mps2-an521"}
            else:
                raw_session = bounded_bytes(args.session, MAX_RECEIPT_BYTES)
                session = json.loads(raw_session, object_pairs_hook=calibration.unique_keys)
                require(isinstance(session, dict), "invalid physical session")
                require(session.get("runtime_kind") == "physical", "serial session requires physical identity")
                require(session.get("image_sha256") == receipt["image_sha256"], "wrong flashed image")
                evidence["calibration"] = retain_calibration(session, args.session.parent, args.output)
                fd, serial_saved = console.serial_open(args.port)
                print(f"Reset the already-flashed board; fresh-boot capture budget is {timeout:g} seconds.", flush=True)
            deadline = time.monotonic() + timeout
            report["failure_stage"] = "fresh-boot"
            boot = read_until(fd, transcript, console.fresh_boot, deadline, process)
            report["fresh_boot_observed"] = console.fresh_boot(boot)
            report["boot_rom_digest_warning_observed"] = console.ROM_DIGEST_WARNING in boot
            report["failure_stage"] = "command"
            console.write_command(fd, report["command"], deadline)
            read_until(fd, transcript, lambda data: console.PROMPT in data, deadline, process)
            report["failure_stage"] = "framing"
            observed = analysis.firmware_frame(bytes(transcript), report["command"])
            require(all(report[key] == value for key, value in observed.items()),
                    "firmware frame differs from captured boot observations")
        report["failure_stage"] = "analysis"
        summary = analysis.analyze(transcript.decode("utf-8", errors="strict"))
        metadata = summary["metadata"]
        require(metadata["case"] == args.case and metadata["samples"] == args.samples and metadata["warmup"] == args.warmup, "unexpected experiment")
        expected_payload = "none" if args.case in {"semaphore", "park", "fixed-peer"} else ("bytes16" if args.payload == "bytes16" else "imu-sample")
        require(metadata["payload"] == expected_payload, "unexpected payload profile")
        if args.runtime == "host":
            require(metadata["runtime_kind"] == "host-functional-only", "host runtime mismatch")
        else:
            require(build_receipt(args.build_directory) == receipt and
                    digest(args.build_directory / "build.json") == report["build_receipt_sha256"],
                    "firmware receipt changed during execution")
            associated_session = retained_session(session, evidence["calibration"])
            facts = associate(summary, receipt, associated_session, args.output if args.runtime == "serial" else None,
                              report["fresh_boot_observed"], report["boot_rom_digest_warning_observed"])
            report.update(facts)
            summary["capture_qualification"] = facts
        (args.output / "analysis.json").write_text(json.dumps(summary, indent=2) + "\n")
        report.update(capture_complete=True, failure_stage=None)
    except subprocess.TimeoutExpired as error:
        transcript.extend(error.stdout or b"")
        transcript.extend(error.stderr or b"")
        report["error"] = "external host deadline"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        cleanup_errors = []
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError) as error:
            cleanup_errors.append(f"emulator cleanup: {error}")
        if fd is not None:
            if serial_saved is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, serial_saved)
                except (OSError, termios.error) as error:
                    cleanup_errors.append(f"serial restore: {error}")
            try:
                os.close(fd)
            except OSError as error:
                cleanup_errors.append(f"console close: {error}")
        if slave is not None:
            try:
                os.close(slave)
            except OSError as error:
                cleanup_errors.append(f"slave close: {error}")
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
            report["capture_complete"] = False
            report["failure_stage"] = report["failure_stage"] or "cleanup"
            report["error"] = report["error"] or "; ".join(cleanup_errors)
        (args.output / "console.log").write_bytes(transcript)
        if raw_session is None and session is not None:
            raw_session = (json.dumps(session, indent=2) + "\n").encode()
        if raw_session is not None:
            (args.output / "session.json").write_bytes(raw_session)
            report["session_sha256"] = hashlib.sha256(raw_session).hexdigest()
        if raw_build is not None:
            (args.output / "build.json").write_bytes(raw_build)
        report["console_sha256"] = digest(args.output / "console.log")
        (args.output / "capture.json").write_text(json.dumps(report, indent=2) + "\n")
        names = [name for name in ("console.log", "capture.json", "analysis.json", "session.json", "build.json")
                 if (args.output / name).is_file()]
        if evidence["calibration"]["status"] == "retained":
            names.append(evidence["calibration"]["retained_file"])
        if evidence["host_binary"] and evidence["host_binary"]["mode"] == "standalone":
            names.append(evidence["host_binary"]["file"])
        evidence["files_sha256"] = {name: digest(args.output / name) for name in names}
        (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", choices=("host", "qemu", "serial"))
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--build-directory", type=Path)
    parser.add_argument("--port")
    parser.add_argument("--session", type=Path)
    parser.add_argument("--artifact-root", type=Path,
                        help="explicit shared root containing both the build/executable and this new capture")
    parser.add_argument("--qemu", default="qemu-system-arm")
    parser.add_argument("--case", required=True)
    parser.add_argument("--samples", type=int, default=128)
    parser.add_argument("--warmup", type=int, default=16)
    parser.add_argument("--payload", choices=("bytes16", "imu16"), default="bytes16")
    parser.add_argument("--timeout", type=float,
                        help="1..600 seconds; default 30 for host/QEMU, computed 115200-baud budget for serial")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout is not None and not 1 <= args.timeout <= MAX_TIMEOUT_SECONDS:
        parser.error("timeout must be 1..600 seconds")
    if args.runtime == "host" and args.binary is None:
        parser.error("host capture requires --binary")
    if args.runtime != "host" and args.build_directory is None:
        parser.error("firmware capture requires --build-directory")
    if args.runtime == "serial" and (args.session is None or args.port is None):
        parser.error("serial capture requires --session and --port")
    try:
        result = capture(args)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, indent=2))
    if not result["capture_complete"]:
        parser.exit(1, "capture failed; raw transcript and failed receipt retained\n")


if __name__ == "__main__":
    main()
