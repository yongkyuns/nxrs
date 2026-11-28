#!/usr/bin/env python3
"""Qualify and retain committed heap topology from one real QEMU run.

The fixture owns one fixed arena. Every admitted lock acquisition/release is
observed, even if no geometry changes. Independent safe-point walks and native
counter receipts are required before constructing the replay journal.
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
import time

from build_replay_nuttx import NUTTX_REVISION, hook_coverage, sha256
from build_timing_nuttx import validate_atomic_stack_patch
from qualify_qemu import drain_console, version
from replay_trace import write_trace


CLOCK = "qemu-an521-cmsdk-timer1-20mhz"
COLLECTOR = "nuttx-default-heap-lock-probes-v1"
HEAP_EPOCH = "1"
LABELS = ["initial heap", "allocate left", "allocate middle", "allocate right", "free middle",
          "split middle hole", "free left", "coalesce both neighbors", "release right",
          "allocate for realloc", "realloc shrink", "realloc grow in place", "allocate blocker",
          "realloc move", "realloc failure", "release blocker", "release moved allocation",
          "aligned allocation", "release aligned allocation", "allocate reuse candidate",
          "release reuse candidate", "reuse address", "defer release", "reclaim delayed allocation",
          "release final allocation"]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def geometry(event):
    return [{key: chunk[key] for key in ("start", "size", "state")} for chunk in event["chunks"]]


def validate_build(build_path):
    build = json.loads(build_path.read_text())
    directory = build_path.parent
    validate_atomic_stack_patch(build, Path(__file__).resolve().parents[3], directory)
    require(build.get("schema_version") == 1 and build.get("nuttx_revision") == NUTTX_REVISION,
            "Unqualified allocator revision or build receipt")
    for filename, key in (("nuttx", "elf_sha256"), ("resolved.config", "config_sha256")):
        require(sha256(directory / filename) == build.get(key), "Build receipt hash differs: " + filename)
    require(sha256(directory / "disassembly.txt") == build.get("disassembly_sha256"), "Disassembly receipt hash differs")
    effective = (directory / "resolved.config").read_text().splitlines()
    for symbol in ("ARCH_BOARD_MPS2_AN521", "DEBUG_SYMBOLS", "EXAMPLES_HEAP_REPLAY", "MM_DEFAULT_MANAGER",
                   "BUILD_FLAT", "LTO_NONE", "DEBUG_NOOPT"):
        require("CONFIG_" + symbol + "=y" in effective, "Missing required native configuration: " + symbol)
    for symbol in ("SMP", "MM_HEAP_MEMPOOL", "MM_TLSF_MANAGER", "MM_KERNEL_HEAP"):
        require("CONFIG_" + symbol + "=y" not in effective, "Unsupported native configuration: " + symbol)
    for symbol, value in (("MM_REGIONS", "1"), ("MM_FREE_DELAYCOUNT_MAX", "0"),
                          ("MM_DEFAULT_ALIGNMENT", "8"), ("MM_NODE_GUARDSIZE", "0")):
        require("CONFIG_" + symbol + "=" + value in effective, "Unexpected native configuration: " + symbol)
    coverage = hook_coverage((directory / "disassembly.txt").read_text())
    require(coverage == build.get("hook_coverage"), "Hook coverage receipt differs")
    return build


def validate_observations(raw, console, recording_id, elf_sha256):
    require(raw.get("schema_version") == 1 and raw.get("recording_id") == recording_id and
            raw.get("elf_sha256") == elf_sha256 and raw.get("target_function_calls") is False,
            "Raw observation identity or read-only contract differs")
    events = raw.get("events", [])
    require(2 < len(events) <= 538 and events[0].get("kind") == "initial" and events[-1].get("kind") == "final",
            "Missing or excessive initial/final probe observations")
    initial = events[0]
    require(initial["sequence"] == initial["begin_count"] == initial["commit_count"] == initial["safe_count"] == 0,
            "Capture did not start at sequence zero")
    seq, pending, safe, previous_time = 0, None, 0, initial["time_ns"]
    current = geometry(initial)
    operations = {}
    for index, event in enumerate(events):
        require(event["probe_sequence"] == index + 1, "Missing, duplicate or reordered native probe")
        require(event["region"] == initial["region"] and event["heap_address"] == initial["heap_address"] and
                event["task_id"] == initial["task_id"], "Heap identity, bounds or owning task changed")
        require(event["timer_unchanged"] and event["metadata_unchanged"] and event["primask"] == 1 and
                event["basepri"] != 0 and event["xpsr"] & 0x1ff == 0 and event["faults"] == 0,
                "Observation lacks stopped task-context quiescence")
        require(event["time_ns"] == event["ticks"] * 50 and previous_time <= event["time_ns"] < 5_000_000_000 and
                event["timer_ticks"] >= event["ticks"], "Invalid native clock order or epoch")
        previous_time = event["time_ns"]
        kind = event["kind"]
        if kind == "begin":
            require(pending is None and event["sequence"] == seq and event["begin_count"] == seq + 1 and
                    event["commit_count"] == seq and geometry(event) == current,
                    "Missing, reordered or contradictory acquisition boundary")
            require(event["mutex_holder"] & 0x7fffffff == event["task_id"], "Acquisition does not hold native mutex")
            pending = event
        elif kind == "commit":
            require(pending is not None and event["sequence"] == seq + 1 and event["begin_count"] == seq + 1 and
                    event["commit_count"] == seq + 1 and event["operation"] == pending["operation"],
                    "Missing, duplicate or reordered committed sequence")
            require(event["mutex_holder"] & 0x7fffffff == event["task_id"], "Commit does not hold native mutex")
            seq += 1
            current = geometry(event)
            operations.setdefault(event["operation"], []).append((pending, event))
            pending = None
        elif kind == "safe":
            safe += 1
            require(pending is None and event["sequence"] == seq and event["operation"] == safe and
                    event["safe_count"] == safe and geometry(event) == current,
                    "Independent application observation contradicts committed geometry")
        elif kind in ("initial", "final"):
            require(index == 0 if kind == "initial" else index == len(events) - 1,
                    "Unexpected nested initial/final probe")
            require(pending is None and event["sequence"] == seq and geometry(event) == current,
                    "Initial/final topology boundary contradicts the journal")
        else:
            raise RuntimeError("Unknown native probe kind")
        if kind in ("initial", "safe", "final"):
            require(event["mutex_holder"] == 0x7ffffffe and event["begin_count"] == seq and
                    event["commit_count"] == seq, "Independent safe point has outstanding mutation")
            independent = event.get("independent_safe_heap", {})
            require(independent.get("address") == initial["heap_address"] and len(independent.get("regions", [])) == 1,
                    "Independent oracle did not observe the selected heap")
            region = independent["regions"][0]
            expected = [{"start": chunk["start"], "size": chunk["size"],
                         "state": "allocated" if chunk["kind"] == "allocation" else chunk["kind"]}
                        for chunk in region["chunks"]]
            require({key: region[key] for key in ("start", "size")} == event["region"] and expected == geometry(event) and
                    independent["deferred_payloads"] == event["deferred_payloads"],
                    "Independent raw safe-point walker differs")
    require(safe == 24 and pending is None and seq <= 256, "Incomplete fixture operation/transaction count")
    text = console.decode("utf-8", errors="strict")
    lines = [line for line in text.splitlines() if line.startswith("NXRS_REPLAY ")]
    require(len(lines) == 1 and lines[0].startswith("NXRS_REPLAY PASS "), "Native fixture did not report one PASS")
    frame = dict(item.split("=", 1) for item in lines[0].split()[2:])
    require(frame.get("recording_id") == recording_id and frame.get("faults") == "0" and
            int(frame.get("sequence", -1)) == seq == int(frame.get("begins", -1)) == int(frame.get("commits", -1)) and
            int(frame.get("safe_points", -1)) == safe and int(frame.get("owner", -1)) == initial["task_id"] and
            int(frame.get("probes", -1)) == len(events) and frame.get("arena_bytes") == "65536",
            "Independent UART footer does not close every target sequence")
    return {"transactions": seq, "safe_points": safe, "native_footer": frame, "operations": operations}


def validate_workload(raw, receipt):
    safe = {event["operation"]: event for event in raw["events"] if event["kind"] == "safe"}
    initial = geometry(raw["events"][0])

    def allocated(point, pointer):
        matches = [chunk for chunk in point["chunks"] if chunk["state"] == "allocated" and
                   chunk["start"] < pointer < chunk["start"] + chunk["size"]]
        require(len(matches) == 1, "Controlled payload is not inside one allocated raw chunk")
        return matches[0]

    first = [allocated(safe[3], pointer) for pointer in safe[3]["blocks"][:3]]
    require(all(left["start"] + left["size"] == right["start"] for left, right in zip(first, first[1:])),
            "Initial allocations are not adjacent")
    middle = first[1]
    require(any(chunk["start"] == middle["start"] and chunk["size"] == middle["size"] and chunk["state"] == "free"
                for chunk in safe[4]["chunks"]), "Middle release did not establish an exact interior hole")
    before_coalescing = safe[6]["chunks"]
    released = allocated(safe[6], safe[6]["blocks"][1])
    index = before_coalescing.index(released)
    require(0 < index < len(before_coalescing) - 1, "Coalescing target lacks neighbors")
    previous, following = before_coalescing[index - 1], before_coalescing[index + 1]
    require(previous["state"] == following["state"] == "free" and any(
        chunk["state"] == "free" and chunk["start"] == previous["start"] and
        chunk["size"] == previous["size"] + released["size"] + following["size"]
        for chunk in safe[7]["chunks"]), "Release did not coalesce both observed free neighbors")
    require(geometry(safe[8]) == initial, "Two-sided coalescing did not restore initial geometry")
    p9, p10, p11 = [safe[op]["blocks"][0] for op in (9, 10, 11)]
    require(p9 == p10 == p11, "Realloc shrink/grow did not retain its address")
    sizes = [allocated(safe[op], p9)["size"] for op in (9, 10, 11)]
    require(sizes[1] < sizes[2] < sizes[0], "Realloc shrink/grow sizes did not change as requested")
    require(safe[13]["blocks"][0] != p9 and len(receipt["operations"][13]) >= 3,
            "Moved realloc did not expose all release phases")
    require(geometry(safe[14]) == geometry(safe[13]), "Failed realloc changed committed geometry")
    require(len(receipt["operations"][17]) >= 2 and safe[17]["blocks"][0] % 256 == 0,
            "Aligned allocation lacks intermediate backing phase or requested alignment")
    require(safe[19]["blocks"][0] == safe[21]["blocks"][0], "Pointer-reuse witness did not reuse an address")
    pointer = safe[21]["blocks"][0]
    require(safe[22]["deferred_payloads"] == [pointer] and geometry(safe[22]) == geometry(safe[21]),
            "Deferred release was incorrectly treated as reclaimed")
    require(not safe[23]["deferred_payloads"] and len(receipt["operations"][23]) >= 2,
            "Delayed free reclamation and subsequent allocation were not separate commits")
    require(geometry(receipt["operations"][23][0][1]) == initial,
            "First delayed-reclamation commit did not restore the complete free heap")
    require(geometry(safe[24]) == initial and geometry(raw["events"][-1]) == initial,
            "Fixture did not return to its exact initial geometry")
    return {"split_and_interior_hole": True, "coalescing_recovery": True, "realloc_shrink_grow_move_failure": True,
            "alignment_intermediate_phase": True, "pointer_reuse": True,
            "delayed_release_and_reclamation": True, "final_exact_recovery": True}


def make_journal(raw, build):
    initial = raw["events"][0]
    journal = {"schema_version": 1, "build": {"elf_sha256": build["elf_sha256"], "label": "QEMU isolated heap replay"},
        "provenance": {"nuttx_revision": build["nuttx_revision"], "config_sha256": build["config_sha256"], "collector": COLLECTOR},
        "recording_id": raw["recording_id"], "clock": CLOCK,
        "heap": {"id": "isolated-default-heap", "epoch": HEAP_EPOCH, "region": "cpu-ssram23-secure", **initial["region"]},
        "initial": {"sequence": 0, "time_ns": initial["time_ns"], "chunks": geometry(initial)}, "records": []}
    before, pending = geometry(initial), None
    for event in raw["events"][1:]:
        if event["kind"] == "begin":
            pending = event
        elif event["kind"] == "commit":
            journal["records"].append({"kind": "transaction", "sequence": event["sequence"], "heap_epoch": HEAP_EPOCH,
                "start_ns": pending["time_ns"], "commit_ns": event["time_ns"], **initial["region"],
                "expected": before, "replacement": geometry(event), "label": LABELS[event["operation"]]})
            before, pending = geometry(event), None
        elif event["kind"] == "safe":
            journal["records"].append({"kind": "checkpoint", "heap_epoch": HEAP_EPOCH, "sequence": event["sequence"],
                "time_ns": event["time_ns"], "chunks": geometry(event), "label": LABELS[event["operation"]] + " · safe point"})
        elif event["kind"] == "final":
            journal["closing"] = {"heap_epoch": HEAP_EPOCH, "sequence": event["sequence"], "time_ns": event["time_ns"]}
    return journal


def loss_control(journal):
    """Remove a real cancelling malloc/free pair; retain the next full oracle.

    This is explicitly derived negative evidence, never a claim that the native
    complete collector lost these records. Equal endpoint geometry must not
    establish the intervening lifetime or hide missing native sequences.
    """
    candidates = [record for record in journal["records"] if record["kind"] == "transaction" and
                  record["label"] in ("allocate reuse candidate", "release reuse candidate")]
    require(len(candidates) == 2 and candidates[1]["sequence"] == candidates[0]["sequence"] + 1 and
            candidates[0]["expected"] == candidates[1]["replacement"],
            "Negative control requires the actual cancelling allocation/free pair")
    dropped = [record["sequence"] for record in candidates]
    result = copy.deepcopy(journal)
    result["build"]["label"] += " · injected missing allocation/free pair"
    result["provenance"]["collector"] += "-missing-pair-control"
    result["records"] = [record for record in result["records"] if record.get("sequence") not in dropped]
    next_transaction = next(record for record in result["records"] if record["kind"] == "transaction" and
                            record["sequence"] > dropped[-1])
    recovery = next(record for record in result["records"] if record["kind"] == "checkpoint" and
                    record["sequence"] >= next_transaction["sequence"])
    receipt = {"kind": "injected_missing_cancelling_pair", "source": "journal.json",
               "derived": "journal-loss-control.json", "dropped_sequences": dropped,
               "same_geometry_before_and_after_pair": True,
               "unknown_probe_time_ns": next_transaction["commit_ns"],
               "recovery_checkpoint_time_ns": recovery["time_ns"],
               "recovery_checkpoint_sequence": recovery["sequence"],
               "scope": "Deliberate host-side removal from a complete real capture; not measured target loss"}
    return result, receipt


def run(args):
    require(os.name == "posix" and 0 < args.timeout <= 120, "POSIX host and timeout in (0,120] seconds required")
    qemu, debugger, build_path, output = [getattr(args, name).expanduser().resolve()
                                         for name in ("qemu", "gdb", "build", "output")]
    build = validate_build(build_path)
    image = build_path.parent / "nuttx"
    require(all(character not in str(output) for character in (",", "\n", "\r")), "Invalid QEMU output path")
    output.mkdir(parents=True, exist_ok=False)
    serial = output / "uart"
    in_path, out_path = Path(str(serial) + ".in"), Path(str(serial) + ".out")
    os.mkfifo(in_path)
    os.mkfifo(out_path)
    in_fd = os.open(in_path, os.O_RDWR | os.O_NONBLOCK)
    out_fd = os.open(out_path, os.O_RDWR | os.O_NONBLOCK)
    recording_id = secrets.token_hex(16)
    test_tools = Path(__file__).resolve().parent
    settings = {"schema_version": 1, "image": str(image), "output": str(output), "build": str(build_path),
        "test_tools": str(test_tools), "elf_sha256": build["elf_sha256"], "config_sha256": build["config_sha256"],
        "recording_id": recording_id, "qemu_version": version(qemu), "gdb_version": version(debugger),
        "command": "heap_replay " + recording_id,
        "qemu_command": [str(qemu), "-machine", "mps2-an521", "-kernel", str(image), "-display", "none",
            "-monitor", "none", "-nic", "none", "-no-reboot", "-serial", "pipe:" + str(serial), "-S", "-gdb", "stdio"]}
    (output / "run.json").write_text(json.dumps(settings, indent=2) + "\n")
    environment = os.environ.copy()
    environment["NXRS_HEAP_REPLAY_RUN"] = str(output / "run.json")
    command = [str(debugger), "-nx", "-q", "--batch", str(image), "-x", str(test_tools / "replay_capture_gdb.py")]
    process, console, sent = None, bytearray(), False
    capture_started = time.monotonic()
    try:
        with (output / "gdb.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment, start_new_session=True)
            deadline = time.monotonic() + args.timeout
            while True:
                require(time.monotonic() < deadline, "QEMU/GDB replay capture exceeded its deadline")
                if select.select([out_fd], [], [], 0.05)[0]:
                    console.extend(os.read(out_fd, 65536))
                require(len(console) <= 4 * 1024 * 1024 and log.tell() <= 4 * 1024 * 1024, "Capture transcript exceeded 4 MiB")
                if (output / "failure.json").is_file():
                    raise RuntimeError("GDB replay capture failed: " + json.loads((output / "failure.json").read_text())["error"])
                if not sent and b"nsh>" in console and (output / "send-command").exists():
                    for byte in (settings["command"] + "\n").encode():
                        os.write(in_fd, bytes((byte,)))
                        time.sleep(0.01)
                    sent = True
                if process.poll() is not None:
                    drain_console(out_fd, console, log, deadline)
                    break
        require(process.returncode == 0, f"GDB replay capture exited with {process.returncode}")
        raw = json.loads((output / "raw-events.json").read_text())
        receipt = validate_observations(raw, console, recording_id, build["elf_sha256"])
        workload = validate_workload(raw, receipt)
        journal = make_journal(raw, build)
        (output / "journal.json").write_text(json.dumps(journal, indent=2) + "\n")
        write_trace(journal, output / "timing.json")
        loss_journal, loss_receipt = loss_control(journal)
        (output / "journal-loss-control.json").write_text(json.dumps(loss_journal, indent=2) + "\n")
        (output / "loss-control.json").write_text(json.dumps(loss_receipt, indent=2) + "\n")
        report = {"schema_version": 1, "evidence": "real_nuttx_qemu_isolated_heap_replay", "recording_id": recording_id,
            "elf_sha256": build["elf_sha256"], "config_sha256": build["config_sha256"], "clock": CLOCK,
            "transactions": receipt["transactions"], "safe_points": receipt["safe_points"],
            "probe_count": len(raw["events"]), "capture_wall_seconds": round(time.monotonic() - capture_started, 6),
            "first_source_ns": raw["events"][0]["time_ns"], "last_source_ns": raw["events"][-1]["time_ns"],
            "native_footer": receipt["native_footer"], "workload": workload, "hook_coverage": build["hook_coverage"],
            "qemu_version": settings["qemu_version"], "gdb_version": settings["gdb_version"],
            "static_arena_bytes": 65536, "metadata_prefix_bytes": raw["events"][0]["metadata_prefix_bytes"],
            "linked_fixture_code_bytes": build["linked_fixture_code_bytes"],
            "linked_fixture_storage_bytes": build["linked_fixture_storage_bytes"],
            "task_stack_reservation_bytes": build["task_stack_reservation_bytes"],
            "heap_bytes": raw["events"][0]["region"]["size"],
            "raw_capture_bytes": (output / "raw-events.json").stat().st_size,
            "journal_bytes": (output / "journal.json").stat().st_size,
            "timing_bytes": (output / "timing.json").stat().st_size,
            "timing_sha256": sha256(output / "timing.json"),
            "loss_control": loss_receipt,
            "checks": {"every_target_sequence_observed": True, "independent_safe_point_geometry": True,
                       "native_mutex_owner_verified": True, "read_only_gdb": True, "stopped_timer_unchanged": True},
            "limits": ["Isolated fixture-owned default heap only; no production allocator replay admission",
                       "Debugger stops and link wrappers perturb execution; no physical or uninterrupted performance claim",
                       "Application allocation identity, source and generations are unobserved",
                       "Topology is unknown during mutation windows; stacks and other heaps have no replay coverage",
                       "The 32-bit 20 MHz timer is reset per invocation; this fixture is admitted below 5 seconds"]}
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(output / "report.json"), "transactions": receipt["transactions"],
                          "safe_points": receipt["safe_points"], "recording_id": recording_id}))
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
    for name in ("qemu", "gdb", "build", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    run(parser.parse_args())
