#!/usr/bin/env python3
"""Capture memory checkpoints and native timing from one QEMU invocation.

An opt-in native fixture masks interrupts around each recorded checkpoint and
hardware breakpoint. GDB exports only while stopped. Exact native note times,
not host elapsed time or debugger clock calls, bind the final observations.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import subprocess
import sys
import time

from build_timing_nuttx import validate_atomic_stack_patch
from qualify_qemu import drain_console, version
from qualify_timing_qemu import evidence


CLOCK = "qemu-an521-cmsdk-timer1-20mhz"
LABELS = ["baseline", "three allocations", "middle allocation freed", "workload released"]
MARKER = re.compile(r"^.+-\d+\s+\[0\]\s+(\d+)\.(\d{9}): tracing_mark_write: I\|(\d+)\|memory/checkpoint/([1-9]\d*)$")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_markers(console, report, recording_id):
    """Cross-check stopped native ring headers against independent UART text."""
    timing = evidence(console)
    begin = timing["framing"]["NXRS_NOTE_BEGIN"]
    end = timing["framing"]["NXRS_NOTE_END"]
    if (begin.get("recording_id") != recording_id or end.get("recording_id") != recording_id or
            report.get("recording_id") != recording_id):
        raise RuntimeError("Recording identity differs across native framing and GDB")
    if (begin.get("clock") != CLOCK or begin.get("clock_bits") != "32" or
            begin.get("frequency_hz") != "20000000" or begin.get("clock_epoch") != "fixture-invocation"):
        raise RuntimeError("Checkpoint capture does not establish the admitted timer epoch")
    if begin.get("capture_mode") != "stopped-checkpoints" or begin.get("checkpoints") != "4":
        raise RuntimeError("Target did not report the stopped-checkpoint mode")
    points = report.get("snapshots", [])
    if [point.get("checkpoint_id") for point in points] != [1, 2, 3, 4]:
        raise RuntimeError("Expected all four ordered stopped checkpoints")
    markers = {}
    inside = False
    for line in console.decode("utf-8", errors="strict").splitlines():
        if line.startswith("NXRS_NOTE_BEGIN "):
            inside = True
        elif line.startswith("NXRS_NOTE_END "):
            inside = False
        elif inside and "memory/checkpoint/" in line:
            match = MARKER.fullmatch(line)
            if match is None:
                raise RuntimeError("Malformed reserved checkpoint marker")
            seconds, nanos, task, identifier = map(int, match.groups())
            if identifier in markers:
                raise RuntimeError("Duplicate checkpoint marker")
            markers[identifier] = {"source_time_ns": seconds * 1_000_000_000 + nanos,
                                   "task_id": task, "name": f"memory/checkpoint/{identifier}"}
    if set(markers) != {1, 2, 3, 4}:
        raise RuntimeError("Native UART markers do not cover exactly the four stopped checkpoints")
    previous = None
    for point in points:
        marker = markers[point["checkpoint_id"]]
        raw = point["marker"]
        if (point.get("recording_id") != recording_id or
                not point.get("metadata_unchanged") or not point.get("timer_unchanged_while_stopped")):
            raise RuntimeError("Stopped observation identity or quiescence checks failed")
        if any(raw.get(key) != value for key, value in marker.items()) or raw.get("cpu") != 0:
            raise RuntimeError("UART checkpoint does not match the independently read native note")
        if raw.get("clock_ticks", -1) * 50 != marker["source_time_ns"]:
            raise RuntimeError("Native note clock ticks do not match the UART conversion")
        timestamp = marker["source_time_ns"]
        if timestamp >= 5_000_000_000 or previous is not None and timestamp <= previous:
            raise RuntimeError("Checkpoint timestamps are not strictly ordered within one timer epoch")
        previous = timestamp
    return timing, markers


def validate_heap_lifecycle(raw_points):
    """Check known live payloads and a real free hole, not only heap totals."""
    if len(raw_points) != 4:
        raise RuntimeError("Expected four raw allocator observations")

    def chunks(point):
        return [chunk for heap in point["heaps"] for region in heap["regions"]
                for chunk in region["chunks"]]

    def containing(point, address, kind, size=1):
        return [chunk for chunk in chunks(point) if chunk["kind"] == kind and
                chunk["start"] < address and address + size <= chunk["start"] + chunk["size"]]

    pointers = [point["checkpoint"]["block_pointers"] for point in raw_points]
    if pointers[0] != [0, 0, 0] or pointers[3] != [0, 0, 0]:
        raise RuntimeError("Fixture block ownership is not empty at baseline and release")
    if len(set(pointers[1])) != 3 or any(pointer <= 0 for pointer in pointers[1]):
        raise RuntimeError("Expected three distinct live allocation payloads")
    if pointers[2] != [pointers[1][0], 0, pointers[1][2]]:
        raise RuntimeError("The middle allocation was not exclusively released")
    allocated = []
    for pointer, size in zip(pointers[1], (16384, 32768, 16384)):
        observed = containing(raw_points[1], pointer, "allocation", size)
        if len(observed) != 1:
            raise RuntimeError("A live fixture payload is not contained by one allocated chunk")
        allocated.append(observed[0])
    if any(left["start"] + left["size"] != right["start"] for left, right in zip(allocated, allocated[1:])):
        raise RuntimeError("The controlled allocations are not three contiguous address-ordered chunks")
    middle = pointers[1][1]
    free_middle = containing(raw_points[2], middle, "free", 32768)
    if len(free_middle) != 1:
        raise RuntimeError("The released middle payload is not in an observed free chunk")
    if any(free_middle[0][key] != allocated[1][key] for key in ("start", "size")):
        raise RuntimeError("The free hole does not retain the interior allocation's exact extent")
    for index in (0, 2):
        if containing(raw_points[2], pointers[1][index], "allocation") != [allocated[index]]:
            raise RuntimeError("A retained fixture payload is not still allocated")
    # Thread retirement may leave delayed frees. Check actual deallocation of
    # the three controlled blocks; do not infer a leak from aggregate totals.
    for pointer in pointers[1]:
        if len(containing(raw_points[3], pointer, "free")) != 1:
            raise RuntimeError("Released fixture payload is not in an observed free chunk")
    return {"three_payloads_observed_allocated": True, "three_chunks_contiguous_in_address_order": True,
            "middle_payload_observed_free_while_neighbors_live": True,
            "all_fixture_payloads_observed_free_after_release": True}


def bind_capture(capture, point, recording_id, elf_sha256):
    if capture.get("build", {}).get("elf_sha256") != elf_sha256:
        raise RuntimeError("Unbound snapshot has a different firmware identity")
    snapshots = capture.get("snapshots", [])
    if len(snapshots) != 1 or snapshots[0].get("time_ns") != point["marker"]["source_time_ns"]:
        raise RuntimeError("Unbound snapshot coordinate differs from the native note")
    result = copy.deepcopy(capture)
    result["snapshots"][0]["correlation"] = {
        "recording_id": recording_id, "clock": CLOCK, "checkpoint_id": point["checkpoint_id"]}
    replaced = ("Snapshot coordinates are supplied manually;", "The ELF hash identifies the loaded host file;",
                "All GDB-reported threads were stopped and chunk topology checked;")
    result["warnings"] = [warning for warning in result.get("warnings", [])
                          if not warning.startswith(replaced)]
    result["warnings"].extend([
        "This checkpoint is bound to an independently read native marker and its matching UART record in one QEMU invocation.",
        "The harness launched this exact ELF. Its single-core critical section preserves heap geometry and task stack reservations from marker to stop.",
        "QEMU peripheral time was unchanged during debugger export. This stopped and instrumented execution is not uninterrupted timing or physical-device evidence.",
        "Only the checkpoint is observed; memory state between linked markers remains unknown.",
    ])
    detail = result["capture"]
    detail["time_ns_basis"] = "exact native note timestamp, independently matched to raw UART; source-clock nanoseconds"
    detail["target_build_identity"] = "QEMU launched from the exact hashed ELF by this capture harness"
    detail["consistency"] = "single-core task critical section and PRIMASK exclude allocator/task mutations from marker through stop"
    detail["linked_evidence"] = {"unbound_file": point["unbound_file"], "raw_file": point["raw_file"],
                                 "recording_id": recording_id, "native_marker": point["marker"]}
    # The original exporter remains untouched and available. Its generic
    # quiescence limitations are replaced only for this narrowly checked run.
    detail["limits"] = [limit for limit in detail["limits"] if not limit.startswith((
        "Caller must halt every relevant CPU", "Topology checks do not prove allocator"))]
    detail["limits"].append("The safe-point proof applies only to this single-core, sole-noteram, masked QEMU fixture.")
    return result


def run(args):
    if os.name != "posix":
        raise RuntimeError("The QEMU lifecycle qualification requires a POSIX host")
    if not 0 < args.timeout <= 120:
        raise ValueError("The host deadline must be positive and at most 120 seconds")
    qemu, debugger, image, config, pynuttx, output = [getattr(args, key).expanduser().resolve()
        for key in ("qemu", "gdb", "image", "config", "pynuttx", "output")]
    for path in (qemu, debugger, image, config, pynuttx / "gdbinit.py"):
        if not path.is_file():
            raise FileNotFoundError(path)
    repository = Path(__file__).resolve().parents[3]
    build = json.loads((image.parent / "build.json").read_text())
    validate_atomic_stack_patch(build, repository, image.parent)
    if build["elf_sha256"] != sha256(image) or build["config_sha256"] != sha256(config):
        raise RuntimeError("Native lifecycle image/configuration differ from the build receipt")
    configuration = config.read_text().splitlines()
    for symbol in ("ARCH_BOARD_MPS2_AN521", "DEBUG_SYMBOLS", "EXAMPLES_MEMORY_TIMING",
                   "EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK", "DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE",
                   "MM_DEFAULT_MANAGER", "BUILD_FLAT"):
        if "CONFIG_" + symbol + "=y" not in configuration:
            raise RuntimeError(f"Missing lifecycle configuration: {symbol}")
    if "CONFIG_SMP=y" in configuration or "CONFIG_DRIVERS_NOTE_MAX=1" not in configuration:
        raise RuntimeError("Only the single-core sole-noteram fixture is admitted")
    if any(character in str(output) for character in (",", "\n", "\r")):
        raise ValueError("The output path must not contain QEMU option delimiters")
    tools = Path(__file__).resolve().parent
    output.mkdir(parents=True, exist_ok=False)
    serial = output / "uart"
    in_path, out_path = Path(str(serial) + ".in"), Path(str(serial) + ".out")
    os.mkfifo(in_path)
    os.mkfifo(out_path)
    in_fd = os.open(in_path, os.O_RDWR | os.O_NONBLOCK)
    out_fd = os.open(out_path, os.O_RDWR | os.O_NONBLOCK)
    recording_id = secrets.token_hex(16)
    settings = {"schema_version": 1, "image": str(image), "output": str(output),
        "pynuttx": str(pynuttx), "test_tools": str(tools),
        "exporter": str(tools.parent / "gdb_export.py"), "config": str(config),
        "config_sha256": sha256(config), "elf_sha256": sha256(image),
        "recording_id": recording_id, "labels": LABELS,
        "qemu_version": version(qemu), "gdb_version": version(debugger),
        "command": "memory_timing --checkpoints " + recording_id,
        "qemu_command": [str(qemu), "-machine", "mps2-an521", "-kernel", str(image),
                         "-display", "none", "-monitor", "none", "-nic", "none", "-no-reboot",
                         "-serial", "pipe:" + str(serial), "-S", "-gdb", "stdio"]}
    (output / "run.json").write_text(json.dumps(settings, indent=2) + "\n")
    (output / "resolved.config").write_text(config.read_text())
    environment = os.environ.copy()
    environment["NXRS_MEMORY_LIFECYCLE_RUN"] = str(output / "run.json")
    command = [str(debugger), "-nx", "-q", "--batch", str(image), "-x", str(tools / "lifecycle_capture_gdb.py")]
    process, console, sent = None, bytearray(), False
    try:
        with (output / "gdb.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=True)
            deadline = time.monotonic() + args.timeout
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("QEMU/GDB lifecycle capture exceeded its deadline")
                if select.select([out_fd], [], [], 0.05)[0]:
                    console.extend(os.read(out_fd, 65536))
                if len(console) > 4 * 1024 * 1024 or log.tell() > 4 * 1024 * 1024:
                    raise RuntimeError("Lifecycle transcript exceeded 4 MiB")
                if (output / "failure.json").is_file():
                    failure = json.loads((output / "failure.json").read_text())
                    raise RuntimeError("GDB lifecycle capture failed: " + failure["error"])
                if not sent and b"nsh>" in console and (output / "send-command").exists():
                    for byte in (settings["command"] + "\n").encode():
                        os.write(in_fd, bytes((byte,)))
                        time.sleep(0.01)
                    sent = True
                if process.poll() is not None:
                    drain_console(out_fd, console, log, deadline)
                    break
        if process.returncode != 0:
            raise RuntimeError(f"GDB lifecycle capture failed with exit {process.returncode}")
        (output / "console.log").write_bytes(console)
        report = json.loads((output / "gdb-report.json").read_text())
        if report["elf_sha256"] != settings["elf_sha256"]:
            raise RuntimeError("GDB and launched ELF identities differ")
        timing, markers = validate_markers(console, report, recording_id)
        raw_points = [json.loads((output / point["raw_file"]).read_text()) for point in report["snapshots"]]
        lifecycle = validate_heap_lifecycle(raw_points)
        for point, raw in zip(report["snapshots"], raw_points):
            if (raw["elf_sha256"] != settings["elf_sha256"] or
                    raw["checkpoint"]["recording_id"] != recording_id or
                    raw["checkpoint"]["marker"] != point["marker"]):
                raise RuntimeError("Saved independent raw evidence differs from the stopped report")
        subprocess.run([sys.executable, str(tools.parents[1] / "nuttx-trace-to-perfetto.py"),
                        str(output / "console.log"), str(output / "timing.json"),
                        "--source", "qemu-mps2-an521", "--elf", str(image)], check=True, timeout=15)
        for point in report["snapshots"]:
            capture = json.loads((output / point["unbound_file"]).read_text())
            bound = bind_capture(capture, point, recording_id, settings["elf_sha256"])
            name = f"capture-{point['checkpoint_id']}.json"
            with (output / name).open("x") as stream:
                stream.write(json.dumps(bound, indent=2) + "\n")
            point["capture_file"] = name
            point["time_ns"] = markers[point["checkpoint_id"]]["source_time_ns"]
        report.update({"clock": CLOCK, "timing": timing, "lifecycle": lifecycle,
                       "qemu_version": settings["qemu_version"], "gdb_version": settings["gdb_version"],
                       "config_sha256": settings["config_sha256"], "command": settings["command"],
                       "limits": ["Instrumented and debugger-stopped QEMU execution; no physical-device timing claim",
                                  "Single-core quiescent heap geometry and task stack reservations at four points only",
                                  "Common clock and recording identity do not establish intermediate allocator history",
                                  "The exact host ELF was launched; no separate physical-target attestation"]})
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(output / "report.json"), "recording_id": recording_id,
                          "snapshots": len(report["snapshots"]), "elf_sha256": settings["elf_sha256"]}))
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        os.close(in_fd)
        os.close(out_fd)
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        (output / "console.log").write_bytes(console)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("qemu", "gdb", "image", "config", "pynuttx", "output"):
        parser.add_argument("--" + argument, type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    run(parser.parse_args())
