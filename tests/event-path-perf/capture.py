#!/usr/bin/env python3
"""Capture one P2 case, preserving raw failures and separate qualification fields.

Console I/O, fresh-boot detection and independent-clock validation reuse P0
read-only. This runner never flashes a device or grants production acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
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


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command_for(case, samples, warmup, payload):
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", case) is not None, "invalid case name")
    require(type(samples) is int and 1 <= samples <= 4096, "invalid sample count")
    require(type(warmup) is int and 0 <= warmup <= 1024, "invalid warmup count")
    require(payload in {"bytes16", "imu16"}, "invalid payload profile")
    if case.startswith("fanin-"):
        require(samples <= 256 and warmup <= 64, "finite fan-in storage bound")
    return [case, str(samples), str(warmup), payload]


def build_receipt(directory):
    record = calibration.read_json(directory / "build.json")
    require(record.get("schema") == "event-path-build-v1" and record.get("status") == "complete", "incomplete event-path build")
    require(record.get("mode") == "uninstrumented" and record.get("hook_stage") == 0, "instrumented timing image")
    require(record.get("command") == "event_path_perf", "wrong firmware command")
    require(re.fullmatch(r"[0-9a-f]{64}", record.get("build_id", "")) is not None, "missing build identity")
    artifacts = record.get("artifacts_sha256")
    require(isinstance(artifacts, dict) and bool(artifacts), "missing artifact inventory")
    for name, expected in artifacts.items():
        require(digest(checked_file(directory, name)) == expected, "changed build artifact: " + name)
    for key in ("image", "elf", "config"):
        path = checked_file(directory, record.get(key + "_file"))
        require(digest(path) == record.get(key + "_sha256"), "wrong " + key + " identity")
    for key in ("std_array_sha256", "std_array_original_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", str(record.get(key, ""))) is not None, "missing " + key)
    require(record["std_array_sha256"] == record["std_array_original_sha256"], "instrumented std array")
    require(record.get("compiler_version") and record.get("kernel_revision"), "missing compiler/kernel identity")
    hook_name = record.get("hook_receipt")
    registry_name = record.get("crossbeam_source_receipt")
    require(hook_name in artifacts and registry_name in artifacts, "source proofs absent from artifact inventory")
    hook = calibration.read_json(checked_file(directory, hook_name))
    require(hook.get("schema") == "std-channel-hook-v1" and hook.get("mode") == "latency"
            and hook.get("hook_stage") == 0 and hook.get("channel_instrumented") is False,
            "instrumented or invalid std source proof")
    require(hook.get("original", {}).get("sha256") == record["std_array_original_sha256"]
            and hook.get("selected", {}).get("sha256") == record["std_array_sha256"], "std source proof mismatch")
    registry = calibration.read_json(checked_file(directory, registry_name))
    require(registry == record.get("crossbeam") and registry.get("schema") == "event-path-registry-sources-v1", "Crossbeam source proof mismatch")
    packages = registry.get("packages")
    require(isinstance(packages, list) and len(packages) == len(builder.REGISTRY_PINS), "missing Crossbeam source packages")
    require({item.get("name") for item in packages} == set(builder.REGISTRY_PINS), "wrong Crossbeam source packages")
    for package in packages:
        version, checksum = builder.REGISTRY_PINS[package["name"]]
        require(package.get("version") == version and package.get("registry_checksum") == checksum
                and package.get("uninstrumented_archive_match") is True, "instrumented or wrong Crossbeam source")
    return record


def associate(summary, receipt, session, evidence_directory, fresh_boot, digest_warning):
    """Keep execution identity/calibration facts separate from P2 acceptance."""
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
        "physical_execution_verified": runtime == "physical",
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
    require(not args.output.exists(), "output must be new; preserve previous captures")
    args.output.mkdir(parents=True)
    transcript = bytearray()
    report = {"schema": "event-path-capture-v1", "capture_complete": False,
              "runtime_kind": args.runtime, "case": args.case,
              "physical_timing_qualified": False, "priority_progress_qualified": False,
              "failure_stage": "setup", "error": None}
    session = receipt = None
    fd = slave = process = serial_saved = None
    try:
        if args.runtime == "host":
            binary = args.binary.resolve(strict=True)
            report["binary_sha256"] = digest(binary)
            report["command"] = [str(binary), *arguments]
            report["failure_stage"] = "execution"
            completed = subprocess.run(report["command"], capture_output=True, timeout=args.timeout)
            transcript.extend(completed.stdout)
            transcript.extend(completed.stderr)
            require(len(transcript) <= console.MAX_LOG_BYTES, "console size limit")
            report["returncode"] = completed.returncode
            require(completed.returncode == 0, "host fixture refused or failed")
            require(digest(binary) == report["binary_sha256"], "binary changed during execution")
        else:
            receipt = build_receipt(args.build_directory)
            report["build_receipt_sha256"] = digest(args.build_directory / "build.json")
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
                session = calibration.read_json(args.session)
                require(session.get("runtime_kind") == "physical", "serial session requires physical identity")
                require(session.get("image_sha256") == receipt["image_sha256"], "wrong flashed image")
                fd, serial_saved = console.serial_open(args.port)
                print("Reset the already-flashed board; waiting for a fresh boot banner.", flush=True)
            deadline = time.monotonic() + args.timeout
            report["failure_stage"] = "fresh-boot"
            boot = console.read_until(fd, transcript, console.fresh_boot, deadline, process)
            report["fresh_boot_observed"] = console.fresh_boot(boot)
            report["boot_rom_digest_warning_observed"] = console.ROM_DIGEST_WARNING in boot
            report["failure_stage"] = "command"
            console.write_command(fd, report["command"], deadline)
            console.read_until(fd, transcript, lambda data: console.PROMPT in data, deadline, process)
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
            facts = associate(summary, receipt, session, args.session.parent if args.runtime == "serial" else None,
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
        if session is not None:
            (args.output / "session.json").write_text(json.dumps(session, indent=2) + "\n")
        if receipt is not None:
            (args.output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
        report["console_sha256"] = digest(args.output / "console.log")
        (args.output / "capture.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", choices=("host", "qemu", "serial"))
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--build-directory", type=Path)
    parser.add_argument("--port")
    parser.add_argument("--session", type=Path)
    parser.add_argument("--qemu", default="qemu-system-arm")
    parser.add_argument("--case", required=True)
    parser.add_argument("--samples", type=int, default=128)
    parser.add_argument("--warmup", type=int, default=16)
    parser.add_argument("--payload", choices=("bytes16", "imu16"), default="bytes16")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 120:
        parser.error("timeout must be 1..120 seconds")
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
