#!/usr/bin/env python3
"""Compare actual allocator notes with independently retained call outcomes.

The C witness records return values/counts/payload checks separately from the
note stream. This gate never uses the trace converter to establish those facts.
"""

from __future__ import annotations

import argparse
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
import sys
import time

from qemu_console import nsh_prompt_ready


CLOCK = "qemu-an521-cmsdk-timer1-20mhz"
CASES = (
    ("disabled_before", "malloc", 17, 0),
    ("malloc_success", "malloc", 4096, 0),
    ("malloc_failure", "malloc", 131072, 0),
    ("realloc_shrink", "realloc", 1024, 0),
    ("realloc_grow", "realloc", 3072, 0),
    ("malloc_blocker", "malloc", 4096, 0),
    ("realloc_move", "realloc", 8192, 0),
    ("realloc_failure", "realloc", 131072, 0),
    ("memalign", "memalign", 512, 256),
    ("realloc_null", "realloc", 256, 0),
    ("realloc_zero", "realloc", 0, 0),
    ("disabled_after", "malloc", 19, 0),
)
NOTE = re.compile(r"^.+-(\d+)\s+\[0\]\s+(\d+)\.(\d{9}): ([a-z_]+): (.*)$")
# Pinned NuttX libvsprintf suppresses the hex prefix for a null %p value.
PTR = r"(0x[0-9a-fA-F]+|0)"
REQUEST = re.compile(r"(malloc|memalign|realloc) heap:\s*" + PTR +
                     r" id:(\d+) size:(\d+) caller:\s*" + PTR +
                     r" old:\s*" + PTR + r" align:(\d+)")
RESULT = re.compile(r"(malloc|memalign|realloc) heap:\s*" + PTR +
                    r" id:(\d+) result:\s*" + PTR + r" outcome:(success|failure)")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fields(line):
    result = {}
    for field in line.split():
        key, value = field.split("=", 1)
        require(key not in result, "Duplicate target evidence field")
        result[key] = value
    return result


def evidence(console, expected_recording=None):
    text = console.decode("utf-8", errors="strict")
    require("NXRS_HEAP_REQUESTS FAIL" not in text, "Native allocator workload failed")
    lines = text.splitlines()

    def frame(prefix):
        matches = [(index, fields(line[len(prefix):])) for index, line in enumerate(lines)
                   if line.startswith(prefix)]
        require(len(matches) == 1, f"Expected exactly one {prefix.strip()} record")
        return matches[0]

    begin_index, begin = frame("NXRS_NOTE_BEGIN ")
    end_index, end = frame("NXRS_NOTE_END ")
    _, clock = frame("NXRS_REQUEST_CLOCK ")
    _, summary = frame("NXRS_HEAP_REQUESTS PASS ")
    recording = summary["recording_id"]
    require(re.fullmatch(r"[0-9a-f]{32}", recording) is not None and
            (expected_recording is None or expected_recording == recording), "Unexpected recording/reset identity")
    require(begin_index < end_index and all(item.get("recording_id") == recording for item in (begin, end)),
            "Target note framing/recording differs")
    require(all(item.get("mode") == "0" and item.get("notes_lost") == "0" for item in (begin, end)) and
            end.get("complete") == "1", "Target capture did not close without overflow")
    require(begin.get("cpus") == "1" and begin.get("clock") == CLOCK and
            begin.get("clock_bits") == "32" and begin.get("clock_epoch") == "fixture-invocation" and
            begin.get("frequency_hz") == "20000000", "Unsupported target clock or execution context")
    require(int(clock["frequency_hz"]) == 20000000 and int(clock["after"]) > int(clock["before"]) >= 0,
            "Target perf clock did not advance")
    require(summary.get("rows") == "12" and summary.get("enabled_rows") == "10" and
            summary.get("disabled_rows") == "2" and 0 < int(summary["dump_bytes"]) < 262144,
            "Native workload row/dump bounds differ")
    heap, owner = int(summary["heap"], 0), int(summary["owner"])
    require(heap > 0 and owner > 0, "Missing actual fixture heap/task identity")
    record_sizes = {key: int(summary[key]) for key in ("request_note_bytes", "result_note_bytes", "fixture_rows_bytes")}
    require(all(0 < record_sizes[key] < 256 for key in ("request_note_bytes", "result_note_bytes")) and
            record_sizes["fixture_rows_bytes"] >= 12 * 32, "Native record/fixture storage sizes are invalid")

    rows = []
    for line in lines:
        if not line.startswith("NXRS_REQUEST_ROW "):
            continue
        raw = fields(line[len("NXRS_REQUEST_ROW "):])
        row = {key: raw[key] for key in ("case", "phase", "op")}
        for key in ("index", "size", "align", "before_count", "after_count", "before_ticks", "after_ticks", "payload_preserved"):
            row[key] = int(raw[key])
        row.update({key: int(raw[key], 0) for key in ("old", "result")})
        rows.append(row)
    require(len(rows) == len(CASES), "Native call rows are incomplete")
    for index, (row, expected) in enumerate(zip(rows, CASES)):
        require(row["index"] == index and (row["case"], row["op"], row["size"], row["align"]) == expected,
                "Native call inputs/order differ from the qualified workload")
        phase = "disabled_before" if index == 0 else "disabled_after" if index == 11 else "enabled"
        require(row["phase"] == phase and row["payload_preserved"] == 1,
                "Native call phase or payload preservation failed")
        require(0 <= row["before_count"] < row["after_count"] < 2**64 and
                0 < row["before_ticks"] < row["after_ticks"] < 100000000,
                "Native aggregate count or clock bounds are invalid")
        if index:
            require(row["before_count"] == rows[index - 1]["after_count"] and
                    row["before_ticks"] >= rows[index - 1]["after_ticks"],
                    "Unobserved fixture heap entry or nonmonotonic call windows")
        failure = row["case"] in ("malloc_failure", "realloc_failure")
        require((row["result"] == 0) == failure, "Native success/failure return differs")
        if row["op"] != "realloc" or row["case"] == "realloc_null":
            require(row["old"] == 0, "Unexpected old pointer for a new allocation")
        else:
            require(row["old"] > 0, "Realloc witness lacks the old allocation")
        if row["case"] in ("realloc_shrink", "realloc_grow", "realloc_zero"):
            require(row["result"] == row["old"], "Pinned in-place realloc witness changed its pointer")
        if row["case"] == "realloc_move":
            require(row["result"] != row["old"], "Move witness did not move")
        if row["case"] == "memalign":
            require(row["result"] % row["align"] == 0, "Native aligned result is misaligned")
    require(rows[3]["old"] == rows[1]["result"] and rows[4]["old"] == rows[3]["result"] and
            rows[6]["old"] == rows[4]["result"] and rows[7]["old"] == rows[6]["result"] and
            rows[10]["old"] == rows[9]["result"], "Native allocation inputs do not follow actual prior return values")

    entries, results, parsed, stack = {}, {}, [], []
    last_time, last_id = 0, 0
    for line_index in range(begin_index + 1, end_index):
        line = lines[line_index]
        match = NOTE.fullmatch(line)
        require(match is not None, "Malformed row in native note frame")
        task, seconds, nanos, kind, payload = match.groups()
        time_ns = int(seconds) * 1000000000 + int(nanos)
        require(last_time <= time_ns < 5000000000, "Native note timestamps ran backward or outside the clock epoch")
        last_time = time_ns
        if kind not in ("heap_request", "heap_result"):
            continue
        values = (REQUEST if kind == "heap_request" else RESULT).fullmatch(payload)
        require(values is not None, "Malformed native allocator request/result")
        operation, raw_heap, request_id, *rest = values.groups()
        request_id = int(request_id)
        require(request_id > 0, "Native request identity is zero")
        if int(raw_heap, 0) != heap:
            continue
        require(int(task) == owner, "Native allocator note belongs to another task")
        record = {"kind": kind, "operation": operation, "heap": heap, "request_id": request_id,
                  "task": int(task), "time_ns": time_ns, "line_index": line_index}
        if kind == "heap_request":
            size, caller, old, alignment = rest
            record.update(size=int(size), caller=int(caller, 0), old=int(old, 0), alignment=int(alignment))
            require(record["caller"] > 0 and request_id not in entries and request_id > last_id,
                    "Missing caller or duplicate/reordered native request identity")
            entries[request_id] = record
            stack.append(request_id)
            last_id = request_id
        else:
            result, outcome = rest
            record.update(result=int(result, 0), outcome=outcome)
            require(request_id in entries and request_id not in results and stack and stack[-1] == request_id,
                    "Missing, duplicate or misnested native result")
            request = entries[request_id]
            require(request["operation"] == operation and request["time_ns"] <= time_ns and
                    ((record["result"] == 0) == (outcome == "failure")), "Native result contradicts its request or outcome")
            results[request_id] = record
            stack.pop()
        parsed.append(record)
    require(not stack and entries.keys() == results.keys(), "Native entry/result pairs are incomplete")
    require(parsed and parsed[-1]["time_ns"] > parsed[0]["time_ns"], "Native request clock did not advance")

    outer_calls = []
    admitted_ids = set()
    for row in rows:
        first, last = row["before_ticks"] * 50, row["after_ticks"] * 50
        inside = [item for item in parsed if first <= item["time_ns"] <= last]
        if row["phase"] != "enabled":
            require(not inside, "Allocator note escaped the runtime disabled filter")
            continue
        calls = [item for item in inside if item["kind"] == "heap_request"]
        require(len(calls) == row["after_count"] - row["before_count"] and calls,
                "Independent aggregate counter detects missing allocator calls")
        outer = calls[0]
        result = results[outer["request_id"]]
        require((outer["operation"], outer["size"], outer["old"], outer["alignment"]) ==
                (row["op"], row["size"], row["old"], row["align"]) and
                result["result"] == row["result"] and result["time_ns"] <= last,
                "Native notes disagree with independently retained call inputs/return value")
        for call in calls:
            completion = results[call["request_id"]]
            require(first <= call["time_ns"] <= completion["time_ns"] <= last,
                    "Native allocator call crosses an independent observation window")
            admitted_ids.add(call["request_id"])
        if row["case"] in ("realloc_move", "realloc_failure", "memalign", "realloc_null"):
            require(len(calls) >= 2 and any(call["operation"] == "malloc" for call in calls[1:]),
                    "Native nested allocation was not independently observed")
        outer_calls.append({"index": row["index"], "case": row["case"],
                            "request_id": outer["request_id"], "nested_calls": len(calls) - 1,
                            "entry_ns": outer["time_ns"], "result_ns": result["time_ns"],
                            "caller": outer["caller"], "result": result["result"],
                            "outcome": result["outcome"]})
    require(admitted_ids == entries.keys(), "Native allocator records are outside the enabled workload")
    return {"schema_version": 1, "evidence": "real_nuttx_qemu_allocator_outcomes", "clock": CLOCK,
            "recording_id": recording, "heap": heap, "owner": owner,
            "rows": rows, "outer_calls": outer_calls, "calls": len(entries),
            "nested_calls": len(entries) - len(outer_calls), "disabled_rows": 2,
            "first_record_ns": parsed[0]["time_ns"], "last_record_ns": parsed[-1]["time_ns"],
            "native_records": parsed, "framing": {"begin": begin, "end": end},
            "target_reported_sizes": record_sizes,
            "checks": {"actual_return_values": True, "aggregate_entry_counts": True,
                       "payload_preservation": True, "runtime_disabled_filter": True,
                       "exact_pair_identity": True, "nested_call_order": True}}


def controls(console, report):
    """Derive explicit host-side loss controls; never call these target loss."""
    lines = console.decode().splitlines()
    selected = next(call["request_id"] for call in report["outer_calls"] if call["case"] == "malloc_success")
    records = [item for item in report["native_records"] if item["request_id"] == selected]
    result = {}
    for name, kinds in (("missing_result", {"heap_result"}), ("missing_request", {"heap_request"}),
                        ("missing_pair", {"heap_request", "heap_result"})):
        removed = {item["line_index"] for item in records if item["kind"] in kinds}
        altered = ("\n".join(line for index, line in enumerate(lines) if index not in removed) + "\n").encode()
        try:
            evidence(altered, report["recording_id"])
        except RuntimeError as error:
            reason = str(error)
        else:
            raise RuntimeError("Independent outcome gate accepted deliberately missing native evidence")
        result[name] = {"console": altered, "request_id": selected, "removed_kinds": sorted(kinds),
                        "rejected_by_native_oracle": reason,
                        "scope": "Host-derived removal from a complete target capture; not measured target loss"}
    return result


def capture_sources(repository):
    return {path: hashlib.sha256((repository / path).read_bytes()).hexdigest() for path in (
        "tools/memory-explorer/tests/qualify_requests_qemu.py",
        "tools/memory-explorer/tests/qemu_console.py", "tools/nuttx-trace-to-perfetto.py")}


def run(args):
    require(os.name == "posix", "Native QEMU request qualification needs a POSIX host")
    require(0 < args.timeout <= 120, "Timeout must be at most 120 seconds")
    repository = Path(__file__).resolve().parents[3]
    qemu, build_path, output = [getattr(args, name).resolve() for name in ("qemu", "build", "output")]
    build = json.loads(build_path.read_text())
    image, config = build_path.parent / "nuttx", build_path.parent / "resolved.config"
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    require(digest(image) == build["elf_sha256"] and digest(config) == build["config_sha256"],
            "Native image/configuration differ from the build receipt")
    configuration = config.read_text().splitlines()
    for symbol in ("EXAMPLES_HEAP_REQUESTS", "MM_ALLOC_REQUEST_TRACE", "DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE",
                   "ARCH_BOARD_MPS2_AN521", "SCHED_INSTRUMENTATION_HEAP"):
        require("CONFIG_" + symbol + "=y" in configuration, "Missing qualification configuration: " + symbol)
    require("CONFIG_SMP=y" not in configuration, "Only the single-core fixture is admitted")
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(config, output / "resolved.config")
    recording = secrets.token_hex(16)
    command = [str(qemu), "-machine", "mps2-an521", "-kernel", str(image), "-display", "none",
               "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    sources = capture_sources(repository)
    settings = {"recording_id": recording, "elf_sha256": digest(image), "config_sha256": digest(config),
                "build_sha256": digest(build_path), "capture_sources_sha256": sources, "qemu_command": command,
                "qemu_version": subprocess.check_output([str(qemu), "--version"], text=True, timeout=10).splitlines()[0]}
    (output / "run.json").write_text(json.dumps(settings, indent=2) + "\n")
    process, console, sent = None, bytearray(), False
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 0.05)[0]:
                data = os.read(process.stdout.fileno(), 65536)
                require(data, "QEMU exited before the allocator witness completed")
                console.extend(data)
            require(len(console) <= 4 * 1024 * 1024, "Native console exceeded 4 MiB")
            if not sent and nsh_prompt_ready(console):
                for byte in ("heap_requests " + recording + "\n").encode():
                    process.stdin.write(bytes((byte,)))
                    process.stdin.flush()
                    time.sleep(0.01)
                sent = True
            if sent and b"NXRS_HEAP_REQUESTS " in console and nsh_prompt_ready(console):
                break
        else:
            raise TimeoutError("Native allocator witness exceeded its deadline")
        report = {**settings, **evidence(console, recording)}
        (output / "console.log").write_bytes(console)
        derived = controls(console, report)
        for name, control in derived.items():
            (output / (name + ".log")).write_bytes(control.pop("console"))
        report["controls"] = derived
        report["limits"] = ["Native production instrumentation in an isolated fixture heap; no product topology replay",
                            "Observed call returns do not establish allocation generations, lifetimes, or region ownership",
                            "Instrumented QEMU workload, not physical timing or target overhead qualification",
                            "A zero-size realloc succeeds at the pinned allocator minimum; no release is inferred"]
        converter = Path(__file__).resolve().parents[2] / "nuttx-trace-to-perfetto.py"
        for name in ("console", "missing_result", "missing_request", "missing_pair"):
            destination = "timing.json" if name == "console" else name + ".json"
            subprocess.run([sys.executable, str(converter), str(output / (name + ".log")),
                            str(output / destination), "--source", "qemu-mps2-an521", "--elf", str(image)], check=True)
        require(capture_sources(repository) == sources, "Capture/converter sources changed during qualification")
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(output / "report.json"), "calls": report["calls"],
                          "nested_calls": report["nested_calls"], "disabled_rows": report["disabled_rows"]}))
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        (output / "console.log").write_bytes(console)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qemu", "build", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    run(parser.parse_args())
