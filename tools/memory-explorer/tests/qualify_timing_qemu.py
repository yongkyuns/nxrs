#!/usr/bin/env python3
"""Capture the native NuttX timing fixture without debugger stops.

This is an instrumented emulator witness, not a physical-board performance test.
The target supplies timestamps, no-overwrite mode, overflow and dump completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time

from build_timing_nuttx import validate_atomic_stack_patch
from qemu_console import nsh_prompt_ready


def capture_sources(repository):
    return {path: hashlib.sha256((repository / path).read_bytes()).hexdigest() for path in (
        "tools/memory-explorer/tests/qualify_timing_qemu.py",
        "tools/memory-explorer/tests/qemu_console.py")}


def evidence(console):
    text = console.decode("utf-8", errors="strict")
    if "NXRS_TIMING_WITNESS FAIL" in text or "NXRS_TIMING_WITNESS PASS rounds=4" not in text:
        raise RuntimeError("The native target timing witness did not pass; inspect console.log")
    frames = {}
    for name in ("NXRS_NOTE_BEGIN", "NXRS_NOTE_END", "NXRS_TIMING_CLOCK"):
        lines = [line for line in text.splitlines() if line.startswith(name + " ")]
        if len(lines) != 1:
            raise RuntimeError(f"Expected one {name} record")
        frames[name] = dict(item.split("=", 1) for item in lines[0].split()[1:])
    begin, end, clock = (frames[name] for name in ("NXRS_NOTE_BEGIN", "NXRS_NOTE_END", "NXRS_TIMING_CLOCK"))
    if any(frame.get("mode") != "0" or frame.get("notes_lost") != "0" for frame in (begin, end)):
        raise RuntimeError("The target did not establish no-overwrite/no-overflow capture")
    if end.get("complete") != "1" or begin.get("cpus") != "1":
        raise RuntimeError("The capture is incomplete or not the single-core witness")
    if int(clock["frequency_hz"]) <= 0 or int(clock["after"]) <= int(clock["before"]):
        raise RuntimeError("The target perf clock did not advance")
    lines = text.splitlines()
    begin_index = next(index for index, line in enumerate(lines) if line.startswith("NXRS_NOTE_BEGIN "))
    end_index = next(index for index, line in enumerate(lines) if line.startswith("NXRS_NOTE_END "))
    if end_index <= begin_index:
        raise RuntimeError("Invalid target note framing order")
    timestamps = []
    for line in lines[begin_index + 1:end_index]:
        match = re.match(r"^.+-\d+\s+\[0\]\s+(\d+)\.(\d{9}): ", line)
        if not match:
            raise RuntimeError("Malformed or unexpected row inside the target note frame")
        value = int(match[1]) * 1_000_000_000 + int(match[2])
        if timestamps and value < timestamps[-1]:
            raise RuntimeError("Target note timestamps ran backward; capture is not timing-qualified")
        timestamps.append(value)
    if len(timestamps) < 2 or timestamps[-1] <= timestamps[0]:
        raise RuntimeError("Target note timestamps did not advance")
    counts = {name: len(re.findall(r": " + name + r"[: ]", text))
              for name in ("sched_switch", "irq_handler_entry", "irq_handler_exit", "tracing_mark_write")}
    if not all(counts.values()):
        raise RuntimeError("The capture lacks scheduler, IRQ or application marker observations")
    return {"clock": begin["clock"], "clock_probe": clock, "framing": frames,
            "monotonic_records": True, "first_record_ns": timestamps[0], "last_record_ns": timestamps[-1],
            "observed_records": counts, "lost_records": 0, "complete": True}


def acquisition_bridge_evidence(console):
    """Independently inspect actual native notes from the production bridge."""
    text = console.decode("utf-8", errors="strict")
    lines = text.splitlines()
    receipts = [match.groups() for line in lines
                if (match := re.fullmatch(r"NXRS_ACQUISITION_BRIDGE rounds=(\d+) disabled_probes=(\d+)", line))]
    if receipts != [("4", "2")]:
        raise RuntimeError("Missing acquisition bridge execution receipt")
    first = next(index for index, line in enumerate(lines) if line.startswith("NXRS_NOTE_BEGIN "))
    last = next(index for index, line in enumerate(lines) if line.startswith("NXRS_NOTE_END "))
    frame = lines[first + 1:last]
    if any("bridge/disabled-" in line for line in frame):
        raise RuntimeError("Acquisition bridge emitted a note while tracing was disabled")
    edges = []
    for line in frame:
        if "imu/qemu-fixture/" not in line:
            continue
        match = re.fullmatch(r".+-\d+\s+\[0\]\s+(\d+)\.(\d{9}): tracing_mark_write: ([BEI])\|(\d+)\|(imu/qemu-fixture/(?:wake|drain))", line)
        if match is None:
            raise RuntimeError("Malformed acquisition bridge note")
        seconds, nanos, edge, task, name = match.groups()
        edges.append((edge, int(task), name, int(seconds) * 1_000_000_000 + int(nanos)))
    if len(edges) != 12:
        raise RuntimeError("Acquisition bridge needs four wake/begin/end groups")
    for offset in range(0, 12, 3):
        wake, begin, end = edges[offset:offset + 3]
        if (wake[0], begin[0], end[0]) != ("I", "B", "E") or wake[2] != "imu/qemu-fixture/wake" or begin[2] != end[2] or begin[2] != "imu/qemu-fixture/drain":
            raise RuntimeError("Acquisition bridge marker order or scope identity differs")
        if not wake[1] == begin[1] == end[1] or not wake[3] <= begin[3] < end[3]:
            raise RuntimeError("Acquisition bridge task or source-clock boundaries differ")
    return {"drain_scopes": 4, "wake_instants": 4, "disabled_probes": 2,
            "disabled_markers_absent": True,
            "scope": "production note ABI and filtering; simulated workload, no physical sensor"}


def run(args):
    if os.name != "posix":
        raise RuntimeError("The QEMU timing qualification requires a POSIX host")
    if not 0 < args.timeout <= 120:
        raise ValueError("Timeout must be at most 120 s, below the dedicated timer's 214.7483648 s wrap period")
    repository = Path(__file__).resolve().parents[3]
    qemu, image, config, output = [getattr(args, name).resolve() for name in ("qemu", "image", "config", "output")]
    for path in (qemu, image, config):
        if not path.is_file():
            raise FileNotFoundError(path)
    build = json.loads((image.parent / "build.json").read_text())
    validate_atomic_stack_patch(build, repository, image.parent)
    if (build["elf_sha256"] != hashlib.sha256(image.read_bytes()).hexdigest() or
            build["config_sha256"] != hashlib.sha256(config.read_bytes()).hexdigest()):
        raise RuntimeError("Native timing image/configuration differ from the build receipt")
    configuration = config.read_text().splitlines()
    for symbol in ("EXAMPLES_MEMORY_TIMING", "DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE", "ARCH_BOARD_MPS2_AN521"):
        if "CONFIG_" + symbol + "=y" not in configuration:
            raise RuntimeError(f"Missing qualification configuration: {symbol}")
    if "CONFIG_SMP=y" in configuration:
        raise RuntimeError("Only the single-core fixture is qualified")
    output.mkdir(parents=True, exist_ok=False)
    command = [str(qemu), "-machine", "mps2-an521", "-kernel", str(image), "-display", "none",
               "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    sources = capture_sources(repository)
    report = {"schema_version": 1, "evidence": "real_nuttx_qemu_native_timing", "machine": "mps2-an521",
              "elf_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
              "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
              "capture_sources_sha256": sources,
              "qemu_command": command,
              "qemu_version": subprocess.check_output([str(qemu), "--version"], text=True, timeout=10).splitlines()[0],
              "limits": ["Native NuttX qualification workload, not NXRS std-demo",
                         "Instrumented QEMU time, not physical-board execution cost",
                         "No debugger stops or manually supplied coordinates enter these timestamps",
                         "Dedicated timer1 is reset per invocation; this is not a boot-time epoch",
                         "The 32-bit 20 MHz counter wraps after 214.7483648 s; capture is bounded below one wrap",
                         "QEMU AN521 MAINCLK is 20 MHz while the stock NuttX SysTick setup assumes 32 MHz; requested sleeps are not duration oracles"]}
    (output / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "resolved.config").write_text(config.read_text())
    console = bytearray()
    process = None
    sent = False
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 0.05)[0]:
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError("QEMU exited before completing the timing witness")
                console.extend(data)
            if len(console) > 4 * 1024 * 1024:
                raise RuntimeError("Timing console exceeded 4 MiB")
            if not sent and nsh_prompt_ready(console):
                time.sleep(0.02)
                for byte in b"memory_timing\n":
                    process.stdin.write(bytes((byte,)))
                    process.stdin.flush()
                    time.sleep(0.01)
                sent = True
            if sent and b"NXRS_TIMING_WITNESS " in console and nsh_prompt_ready(console):
                break
        else:
            raise TimeoutError("QEMU timing qualification exceeded its deadline")
        report.update(evidence(console))
        report["acquisition_trace_bridge"] = acquisition_bridge_evidence(console)
        if capture_sources(repository) != sources:
            raise RuntimeError("Capture sources changed during qualification")
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(output / "report.json"), "clock": report["clock"],
                          "observed_records": report["observed_records"]}))
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
    for name in ("qemu", "image", "config", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    run(parser.parse_args())
