#!/usr/bin/env python3
"""Qualify production finite capture against native bytes and a slow-work oracle.

Run two and ten second uninterrupted captures, then a deliberately overflowing
two second capture. The same native image executes all three cases; no debugger,
host timestamps, or converter-generated expectations enter the native oracle.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import subprocess
import time

from build_timing_nuttx import APPS_REVISION, NUTTX_REVISION


FREQUENCY = 20_000_000
TICK_NS = 50
ABI = {"common_bytes": 16, "clock_offset": 8, "clock_bytes": 8,
       "pid_offset": 4, "pid_bytes": 4, "event_bytes": 32, "data_offset": 24,
       "begin_type": 31, "end_type": 32, "mark_type": 33, "word_bytes": 4}
MARKER = re.compile(r"^.+-(\d+)\s+\[0\]\s+(\d+)\.(\d{9}): tracing_mark_write: ([BEI])\|(\d+)\|(.+)$")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fields(line, prefix):
    if not line.startswith(prefix + " "):
        raise RuntimeError("Missing native row prefix: " + prefix)
    result = {}
    for item in line[len(prefix) + 1:].split():
        key, value = item.split("=", 1)
        if key in result:
            raise RuntimeError("Duplicate native row field: " + key)
        result[key] = value
    return result


def one(lines, prefix):
    rows = [line for line in lines if line.startswith(prefix + " ")]
    if len(rows) != 1:
        raise RuntimeError("Expected exactly one " + prefix + " row")
    return fields(rows[0], prefix)


def number(value):
    if not re.fullmatch(r"\d+", value):
        raise RuntimeError("Expected an unsigned decimal native value: " + value)
    return int(value)


def fnv(data):
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


def native_evidence(console, seconds, overflow, recording_id):
    """Decode raw native records independently of the production converter."""
    lines = console.decode("utf-8", errors="strict").splitlines()
    if lines.count("NXRS_TIMED_WITNESS PASS") != 1 or any(
        line.startswith("NXRS_TIMED_WITNESS FAIL") for line in lines
    ):
        raise RuntimeError("Native timed witness did not pass")
    begin, end, oracle, abi, raw_end = [one(lines, prefix) for prefix in (
        "NXRS_NOTE_BEGIN", "NXRS_NOTE_END", "NXRS_TIMED_ORACLE", "NXRS_TIMED_ABI", "NXRS_TIMED_RAW_END")]
    if any(frame.get("schema") != "3" or frame.get("recording_id") != recording_id
           for frame in (begin, end)):
        raise RuntimeError("Production capture frame identity differs")
    if (begin.get("source"), begin.get("clock"), begin.get("cpus"), begin.get("categories")) != (
        "nuttx-noteram-ascii", "nuttx-perf", "1", "app"
    ):
        raise RuntimeError("Production capture source, clock or sparse filter differs")
    if any(frame.get("mode") != ("2" if overflow else "0") or
           frame.get("notes_lost") != str(int(overflow)) for frame in (begin, end)):
        raise RuntimeError("Production raw overflow state differs from the native case")
    if end.get("complete") != "1" or begin.get("stop_reason") != "duration":
        raise RuntimeError("Production capture did not finish the bounded dump/duration")
    if begin.get("frequency_hz") != str(FREQUENCY):
        raise RuntimeError("Production source frequency differs from the independent clock")
    start_ns, end_ns, requested_ns, buffer_bytes = [number(begin[key]) for key in (
        "capture_start_ns", "capture_end_ns", "requested_duration_ns", "buffer_bytes")]
    if requested_ns != seconds * 1_000_000_000 or not requested_ns <= end_ns - start_ns <= requested_ns + 100_000_000:
        raise RuntimeError("Actual perf-clock capture duration differs from the finite request")
    if buffer_bytes != 32768:
        raise RuntimeError("The native ring size differs from the qualified image")
    if {key: number(value) for key, value in abi.items()} != ABI:
        raise RuntimeError("The independently decoded pinned ARM note ABI differs")
    if any(oracle.get(key) != value for key, value in {
        "schema": "1", "seconds": str(seconds), "overflow": str(int(overflow)),
        "frequency_hz": str(FREQUENCY), "recording_id": recording_id,
        "operation_count": str(seconds * 3), "worker_error": "0",
        "worker_stack_bytes": "8192", "work_bytes": "256",
        "flood_count": "2048" if overflow else "0",
    }.items()):
        raise RuntimeError("Independent slow-work receipt differs")
    status = int(oracle["capture_status"])
    if (status <= 0 if overflow else status != 0):
        raise RuntimeError("Production command exit status contradicts capture coverage")
    call_before = number(oracle["call_before_ticks"]) * TICK_NS
    call_after = number(oracle["call_after_ticks"]) * TICK_NS
    if not call_before <= start_ns < end_ns <= call_after:
        raise RuntimeError("Capture window is outside its independently timed native command")
    if not number(oracle["enabled_seen_ticks"]) < number(oracle["disabled_seen_ticks"]):
        raise RuntimeError("Independent worker did not observe both enabled and disabled states")
    owner_recovery = None
    if seconds == 2 and not overflow:
        owner_recovery = one(lines, "NXRS_TIMED_OWNER_RECOVERY")
        if (owner_recovery.get("signal"), owner_recovery.get("stop_status"),
            owner_recovery.get("recorder_enabled"), owner_recovery.get("owner_exit_status")) != ("9", "0", "0", "1") or \
           number(owner_recovery["owner_pid"]) == number(owner_recovery["stop_pid"]):
            raise RuntimeError("Killed native trace owner did not recover through an actual stop command")
    task = number(oracle["worker_task_id"])
    raw_rows = [fields(line, "NXRS_TIMED_RAW") for line in lines if line.startswith("NXRS_TIMED_RAW ")]
    raw_bytes = []
    raw_events = []
    aligned = 0
    for index, row in enumerate(raw_rows):
        if number(row["index"]) != index or not re.fullmatch(r"(?:[0-9a-f]{2})+", row["bytes"]):
            raise RuntimeError("Raw native note inventory is incomplete or malformed")
        data = bytes.fromhex(row["bytes"])
        if data[0] != len(data) or len(data) < ABI["event_bytes"] or data[1] not in (31, 32, 33):
            raise RuntimeError("Unexpected native note in the sparse application-only capture")
        tick = int.from_bytes(data[8:16], "little")
        pid = int.from_bytes(data[4:8], "little")
        label_length = len(data) - ABI["event_bytes"]
        label = data[24:24 + label_length].decode("ascii", errors="strict")
        raw_events.append({"time_ns": tick * TICK_NS, "ticks": tick, "task_id": pid,
                           "edge": {31: "B", 32: "E", 33: "I"}[data[1]], "label": label})
        raw_bytes.append(data)
        aligned += (len(data) + 3) & ~3
    if (number(raw_end["records"]) != len(raw_rows) or number(raw_end["aligned_bytes"]) != aligned or
        int(raw_end["checksum"], 16) != fnv(b"".join(raw_bytes)) or not 0 < aligned < buffer_bytes):
        raise RuntimeError("Independent retained native ring checksum/size differs")
    # The production command restores the original no-overwrite preference
    # after recording the actual overflow state in its immutable frame.
    if raw_end.get("mode") != "0":
        raise RuntimeError("Production command did not restore the native recorder preference")
    if overflow and buffer_bytes - aligned > 256:
        raise RuntimeError("Overflow witness did not actually fill the native byte ring")
    begin_index = next(index for index, line in enumerate(lines) if line.startswith("NXRS_NOTE_BEGIN "))
    end_index = next(index for index, line in enumerate(lines) if line.startswith("NXRS_NOTE_END "))
    text_events = []
    for line in lines[begin_index + 1:end_index]:
        if not line:
            continue
        match = MARKER.fullmatch(line)
        if not match or match[1] != match[5]:
            raise RuntimeError("Unexpected or inconsistent production ASCII note")
        text_events.append((int(match[2]) * 1_000_000_000 + int(match[3]),
                            int(match[1]), match[4], match[6]))
    native_events = [(event["time_ns"], event["task_id"], event["edge"], event["label"]) for event in raw_events]
    if text_events != native_events:
        raise RuntimeError("Production ASCII dump differs from independently decoded native bytes")
    if any(left[0] > right[0] for left, right in zip(native_events, native_events[1:])):
        raise RuntimeError("Native timestamps ran backward")
    operations = [fields(line, "NXRS_TIMED_OPERATION") for line in lines if line.startswith("NXRS_TIMED_OPERATION ")]
    if len(operations) != seconds * 3:
        raise RuntimeError("Independent operation inventory differs")
    groups = collections.defaultdict(list)
    for row in operations:
        source, sequence = number(row["source"]), number(row["sequence"])
        if source not in (1, 2):
            raise RuntimeError("Unexpected simulated source")
        if int(row["checksum"], 16) != fnv(bytes((source * 41 + sequence * 17 + index) & 255 for index in range(256))):
            raise RuntimeError("Independent simulated payload checksum differs")
        stamps = [number(row[key]) for key in (
            "due_ticks", "begin_before_ticks", "begin_after_ticks", "end_before_ticks",
            "end_after_ticks", "fix_before_ticks", "fix_after_ticks")]
        due = number(oracle["enabled_seen_ticks"]) + FREQUENCY // 5 + (sequence - 1) * FREQUENCY // source
        if stamps[0] != due or stamps != sorted(stamps) or stamps[1] - due > FREQUENCY // 10:
            raise RuntimeError("Simulated source did not meet its independently recorded perf deadline")
        if stamps[1] * TICK_NS < start_ns + 100_000_000 or stamps[-1] * TICK_NS > end_ns - 100_000_000:
            raise RuntimeError("Sparse leading/trailing capture silence is missing")
        groups[source].append(row)
    matched = 0
    for source in (1, 2):
        rows = sorted(groups[source], key=lambda row: number(row["sequence"]))
        if [number(row["sequence"]) for row in rows] != list(range(1, seconds * source + 1)):
            raise RuntimeError("A slow source operation was lost or duplicated in the independent oracle")
        prefix = f"gnss/simulated_{source}hz/"
        for edge, stage, before, after in (
            ("B", "read_parse_deliver", "begin_before_ticks", "begin_after_ticks"),
            ("E", "read_parse_deliver", "end_before_ticks", "end_after_ticks"),
            ("I", "fix_received", "fix_before_ticks", "fix_after_ticks"),
        ):
            observed = [event for event in raw_events if event["edge"] == edge and event["label"] == prefix + stage]
            if not overflow and len(observed) != len(rows):
                raise RuntimeError("A complete capture lost a slow-source marker")
            matched_sequences = set()
            for event in observed:
                candidates = [row for row in rows if number(row[before]) <= event["ticks"] <= number(row[after])]
                if len(candidates) != 1 or event["task_id"] != task:
                    raise RuntimeError("Native marker timestamp has no unique independent operation bracket")
                sequence = number(candidates[0]["sequence"])
                if sequence in matched_sequences:
                    raise RuntimeError("Native markers duplicate one operation instead of covering the independent inventory")
                matched_sequences.add(sequence)
                matched += 1
            if not overflow and matched_sequences != set(range(1, len(rows) + 1)):
                raise RuntimeError("Native markers omit an independently completed operation")
    if not overflow and matched != len(raw_events):
        raise RuntimeError("Unexpected extra application marker in complete capture")
    if overflow and matched >= len(operations) * 3:
        raise RuntimeError("Overflow control retained every slow-source operation unexpectedly")
    return {"frames": {"begin": begin, "end": end}, "oracle": oracle, "abi": abi,
            "operations": operations, "raw_records": raw_rows, "raw_events": raw_events,
            "raw_checksum": raw_end["checksum"], "native_aligned_bytes": aligned,
            "actual_duration_ns": end_ns - start_ns, "requested_duration_ns": requested_ns,
            "expected_overflow": overflow, "independent_operations": len(operations),
            "killed_owner_recovery": owner_recovery,
            "matched_native_markers": matched, "native_note_count": len(raw_events),
            "leading_silence_ns": min(number(row["begin_before_ticks"]) * TICK_NS for row in operations) - start_ns,
            "trailing_silence_ns": end_ns - max(number(row["fix_after_ticks"]) * TICK_NS for row in operations)}


def at_nsh_prompt(console):
    # NSH may erase the rest of the line after printing its prompt. Inspect
    # only that known suffix; keep the retained native evidence unchanged.
    return console.rstrip().removesuffix(b"\x1b[K").rstrip().endswith(b"nsh>")


def capture(qemu, image, command, output, timeout):
    process = None
    console = bytearray()
    qemu_command = [str(qemu), "-machine", "mps2-an521", "-kernel", str(image), "-display", "none",
                    "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    (output / "command.json").write_text(json.dumps({"qemu": qemu_command, "native": command}, indent=2) + "\n")
    try:
        process = subprocess.Popen(qemu_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        sent = False
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 0.05)[0]:
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError("QEMU exited before the timed capture witness completed")
                console.extend(data)
            if len(console) > 4 * 1024 * 1024:
                raise RuntimeError("Timed capture console exceeded 4 MiB")
            if b"Assertion failed" in console or b"NXRS_TIMED_WITNESS FAIL" in console:
                raise RuntimeError("Native boot/workload failed; inspect the retained console.log")
            if not sent and b"nsh>" in console:
                for byte in (command + "\n").encode("ascii"):
                    process.stdin.write(bytes((byte,)))
                    process.stdin.flush()
                    time.sleep(0.01)
                sent = True
            if sent and b"NXRS_TIMED_WITNESS " in console and at_nsh_prompt(console):
                return bytes(console)
        raise TimeoutError("Native timed capture exceeded its bounded host deadline")
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        (output / "console.log").write_bytes(console)


def capture_sources(repository):
    return {path: sha256(repository / path) for path in (
        "tools/memory-explorer/tests/qualify_timed_capture_qemu.py", "tools/nuttx-trace-to-perfetto.py")}


def validate_build_sources(build, repository):
    patch = build["production_patch"]
    if patch["path"] != "upstream/nuttx-apps/patches/0007-trace-capture-window.patch" or \
       patch["sha256"] != sha256(repository / patch["path"]):
        raise RuntimeError("Native build does not contain the current production capture patch")
    fixture = repository / "tests/nuttx-std/timed-capture"
    expected = {path.name: sha256(path) for path in fixture.iterdir() if path.is_file()}
    if build["fixture_sha256"] != expected or build["installed_fixture_sha256"] != expected:
        raise RuntimeError("Native build fixture source/installed identity differs")
    copied = {"acquisition_trace.c": "shared/acquisition/src/platform/nuttx/ffi/trace.c",
              "qemu_timer_clock.c": "tests/nuttx-std/memory-timing/qemu_timer_clock.c"}
    if set(build["copied_sources"]) != set(copied):
        raise RuntimeError("Native build copied source inventory differs")
    for name, path in copied.items():
        expected_hash = sha256(repository / path)
        if build["copied_sources"][name] != {"source": path, "sha256": expected_hash, "installed_sha256": expected_hash}:
            raise RuntimeError("Native build bridge/clock source identity differs")
    helpers = ("tools/memory-explorer/tests/build_timed_capture_nuttx.py",
               "tools/memory-explorer/tests/build_timing_nuttx.py")
    if build["build_sources_sha256"] != {path: sha256(repository / path) for path in helpers}:
        raise RuntimeError("Native build helper identity differs")
    if any(build["source_identity_before_patch"][name]["tracked_sources_clean"] is not True
           for name in ("nuttx", "apps")) or build["apps_tracked_sources_restored"] is not True:
        raise RuntimeError("Native build source cleanliness was not established")


def run(args):
    if os.name != "posix" or not 0 < args.timeout <= 120:
        raise RuntimeError("Timed QEMU qualification requires POSIX and a host timeout no greater than 120 seconds")
    repository = Path(__file__).resolve().parents[3]
    qemu, build_dir, output = [getattr(args, name).resolve() for name in ("qemu", "build", "output")]
    image = build_dir / "nuttx"
    build = json.loads((build_dir / "build.json").read_text())
    if build["nuttx_revision"] != NUTTX_REVISION or build["apps_revision"] != APPS_REVISION:
        raise RuntimeError("Native build source pins differ")
    if build["elf_sha256"] != sha256(image) or build["config_sha256"] != sha256(build_dir / "resolved.config"):
        raise RuntimeError("Native build ELF/config identity differs")
    validate_build_sources(build, repository)
    output.mkdir(parents=True, exist_ok=False)
    sources = capture_sources(repository)
    report = {"schema_version": 1, "evidence": "production_nuttx_timed_capture_in_qemu",
              "build_sha256": sha256(build_dir / "build.json"), "elf_sha256": sha256(image),
              "config_sha256": sha256(build_dir / "resolved.config"), "capture_sources_sha256": sources,
              "qemu_version": subprocess.check_output([str(qemu), "--version"], text=True).splitlines()[0],
              "debugger_stops": False, "cases": [],
              "limits": ["Simulated periodic operations, no physical GNSS input or sensor readiness",
                         "Application markers only; elapsed operation time is not exclusive CPU cost",
                         "Actual emulated source-clock windows, not host elapsed time or physical throughput",
                         "The fixture resets its dedicated clock before launching the unchanged production command"]}
    (output / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    for name, seconds, overflow in (("two-seconds", 2, False), ("ten-seconds", 10, False), ("overflow", 2, True)):
        case_dir = output / name
        case_dir.mkdir()
        recording_id = secrets.token_hex(16)
        command = f"timed_capture {seconds} {recording_id} {'overflow' if overflow else 'normal'}"
        console = capture(qemu, image, command, case_dir, args.timeout)
        evidence = native_evidence(console, seconds, overflow, recording_id)
        (case_dir / "native.json").write_text(json.dumps(evidence, indent=2) + "\n")
        converter = ["python3", str(repository / "tools/nuttx-trace-to-perfetto.py"),
                     str(case_dir / "console.log"), str(case_dir / "timing.json"), "--elf", str(image)]
        addr2line = shutil.which("arm-none-eabi-addr2line")
        if not addr2line:
            raise RuntimeError("ARM addr2line is required to bind the real native ELF")
        converter.extend(["--addr2line", addr2line])
        with (case_dir / "conversion.log").open("w") as log:
            subprocess.run(converter, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
        converted = json.loads((case_dir / "timing.json").read_text())["otherData"]["nxrs"]
        window = converted["capture_window"]
        if (converted["recording_id"] != recording_id or converted["loss_detected"] != overflow or
            window["complete"] != (not overflow) or window["end_ns"] - window["start_ns"] != evidence["actual_duration_ns"]):
            raise RuntimeError("Converted window/coverage differs from independent native evidence")
        report["cases"].append({"name": name, "recording_id": recording_id, "seconds": seconds,
                                "expected_overflow": overflow, "actual_duration_ns": evidence["actual_duration_ns"],
                                "independent_operations": evidence["independent_operations"],
                                "matched_native_markers": evidence["matched_native_markers"],
                                "leading_silence_ns": evidence["leading_silence_ns"],
                                "trailing_silence_ns": evidence["trailing_silence_ns"],
                                "native_sha256": sha256(case_dir / "native.json"),
                                "console_sha256": sha256(case_dir / "console.log"),
                                "timing_sha256": sha256(case_dir / "timing.json")})
    if capture_sources(repository) != sources:
        raise RuntimeError("Capture/converter sources changed during qualification")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(output / "report.json"), "cases": report["cases"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qemu", "build", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    run(parser.parse_args())
