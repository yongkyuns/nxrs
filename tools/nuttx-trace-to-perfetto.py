#!/usr/bin/env python3
"""Convert NuttX noteram ASCII to precise Chrome/Perfetto Trace Event JSON.

Scheduler spans are scheduled residency, including overlapping interrupt time;
they are not exclusive task CPU execution. Only observed pairs become durations.
Capture edges and loss boundaries remain explicit unknown-duration markers.
Unframed console dumps carry unknown clock/source/loss unless supplied explicitly.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess

U64_MAX = (1 << 64) - 1
MAX_LINES = 1_000_000
MAX_BYTES = 128 * 1024 * 1024
NOTE = re.compile(
    r"^\s*(?:(?P<task>.*?)-(?P<pid>\d+)\s+)?\[(?P<cpu>\d+)\]\s+"
    r"(?P<seconds>\d+)\.(?P<fraction>\d{1,9}):\s*"
    r"(?P<kind>[A-Za-z_]\w*):\s*(?P<payload>.*)$"
)
SWITCH = re.compile(
    r"prev_comm=(.*?)\s+prev_pid=(\d+)\s+prev_prio=(\d+)\s+"
    r"prev_state=(\S+)\s+==>\s+next_comm=(.*?)\s+next_pid=(\d+)\s+next_prio=(\d+)"
)
NEXT_SWITCH = re.compile(r"next_comm=(.*?)\s+next_pid=(\d+)\s+next_prio=(\d+)")
IRQ = re.compile(r"(?:^|\s)irq=(\d+)(?:\s|$)")
WAKE = re.compile(r"comm=(.*?)\s+pid=(\d+)\s+target_cpu=(\d+)")
HEAP_REQUEST = re.compile(
    r"(malloc|memalign|realloc)\s+heap:\s*(\S+)\s+size:\s*(\d+)\s+caller:\s*(\S+)"
)
HEAP_USAGE = re.compile(
    r"C\|-?\d+\|Heap Usage\|(\d+)\|(\w+):\s+heap:\s*(\S+)"
    r"(?:\s+size:\s*(\d+),\s+address:\s*(\S+))?"
)
WATCHDOG = re.compile(r"I\|(-?\d+)\|wdog: (start|cancel|enter|leave)-(\S+) (\S+)")
LOSS = re.compile(r"^\s*(?:LOST_EVENTS\s+count=(\d+)|\[LOST\s+(\d+)\s+EVENTS\])\s*$")
BEGIN, END = "NXRS_NOTE_BEGIN ", "NXRS_NOTE_END "


def _frame(line, prefix):
    fields = {}
    for part in line[len(prefix):].split():
        key, separator, value = part.partition("=")
        if not separator or not value or key in fields:
            raise ValueError(f"Malformed or duplicate {prefix.strip()} field {part!r}")
        fields[key] = value
    return fields


def _unsigned(value, label):
    if not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{label} must be an unsigned integer")
    integer = int(value)
    if integer > U64_MAX:
        raise ValueError(f"{label} exceeds u64")
    return integer


def _bool_field(fields, name):
    value = fields.get(name)
    if value not in ("0", "1"):
        raise ValueError(f"Capture frame {name} must be 0 or 1")
    return value == "1"


def _us(nanoseconds):
    # 20 significant digits cover u64; Decimal avoids a binary-float timestamp.
    return Decimal(nanoseconds).scaleb(-3)


def encode_trace(value):
    """Encode exact decimal timestamps as standard unquoted JSON numbers."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite trace number")
        # Scientific notation is valid JSON and remains exact. Fixed-point
        # formatting could expand a short input like 1e-1000000000 by a GiB.
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + ":" + encode_trace(item)
                              for key, item in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(encode_trace(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def elf_identity(path):
    """Hash the supplied executable without loading the whole debug file into RAM."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def symbolize_trace(trace, elf, *, addr2line=None):
    """Resolve recorded PCs using one hash-matched ELF, never task-name guesses.

    Native request PCs are return addresses, so their line describes the recorded
    continuation. No instruction-width adjustment, stack unwind, semantic owner,
    or GPIO/UART consumer is inferred. On-target %pS names are preserved as such.
    """
    elf = Path(elf)
    metadata = trace["otherData"]["nxrs"]
    identity = elf_identity(elf)
    if metadata.get("elf_sha256") != identity:
        raise ValueError("Symbol ELF hash conflicts with trace ELF identity")
    with elf.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[5] not in (1, 2):
        raise ValueError("Symbol source is not a supported ELF executable")
    order = "little" if header[5] == 1 else "big"
    if int.from_bytes(header[16:18], order) != 2:
        raise ValueError("Symbol source must be a final ET_EXEC ELF")
    machine = int.from_bytes(header[18:20], order)
    source_fields = []
    addresses = set()
    counts = {"caller_observations": 0, "caller_resolved": 0, "handler_observations": 0, "handler_resolved": 0}
    for event in trace["traceEvents"]:
        if event.get("ph") == "M":
            continue
        args = event.get("args", {})
        for key in ("caller", "handler"):
            raw = args.get(key)
            if not isinstance(raw, str):
                continue
            counts[f"{key}_observations"] += 1
            # With symbol printf disabled, NuttX prints %pS as the raw pointer
            # followed by the literal S. It is still a PC, not a symbol name.
            pointer = re.fullmatch(r"0[xX]([0-9a-fA-F]+)S?" if key == "handler" else r"0[xX]([0-9a-fA-F]+)", raw)
            if pointer:
                address = int(pointer[1], 16)
                if address > U64_MAX:
                    raise ValueError("Recorded source address exceeds u64")
                args[f"{key}_pc"] = f"0x{address:x}"
                if address:
                    addresses.add(address)
                    source_fields.append((event, key, address))
            elif key == "handler" and raw not in ("(nil)", "NULL", "?"):
                args["handler_function"] = raw
                args["handler_evidence"] = "native callback name; device consumer not established"
                counts["handler_resolved"] += 1
    if len(addresses) > 65536:
        raise ValueError("Trace exceeds the 65,536 unique source-PC symbolization bound")
    resolved = {}
    tool = addr2line
    if tool is None and addresses:
        prefixes = {40: ["arm-none-eabi-addr2line"], 94: ["xtensa-esp32s3-elf-addr2line", "xtensa-esp32-elf-addr2line"],
                    243: ["riscv32-unknown-elf-addr2line", "riscv64-unknown-elf-addr2line"]}.get(machine, [])
        tool = next((found for candidate in prefixes + ["llvm-addr2line", "addr2line"]
                     if (found := shutil.which(candidate))), None)
    if addresses and tool:
        ordered = sorted(addresses)
        try:
            process = subprocess.run([str(tool), "-f", "-C", "-e", str(elf)],
                                     input="".join(f"0x{address:x}\n" for address in ordered),
                                     capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Could not run ELF symbolizer: {error}") from error
        if process.returncode:
            raise ValueError("ELF symbolizer failed; select the matching target addr2line with --addr2line: "
                             + process.stderr[:1000].strip())
        if len(process.stdout) > 16 * 1024 * 1024:
            raise ValueError("ELF symbolizer output exceeds 16 MiB")
        rows = process.stdout.splitlines()
        if len(rows) != 2 * len(ordered):
            raise ValueError("Unexpected addr2line output; expected one function/location pair per PC")
        for address, function, location in zip(ordered, rows[::2], rows[1::2]):
            entry = {}
            if function not in ("??", "?"):
                entry["function"] = function
            source_line = re.fullmatch(r"(.*?):(\d+)(?:\s+\(discriminator \d+\))?", location)
            if source_line and source_line[1] not in ("??", "?"):
                entry["file"] = source_line[1]
                if int(source_line[2]):
                    entry["line"] = int(source_line[2])
            if entry:
                resolved[address] = entry
    elif addresses:
        metadata["warnings"].append("No addr2line executable was found; caller and handler PCs remain unresolved. Supply --addr2line from the target toolchain.")
    for event, key, address in source_fields:
        args = event["args"]
        if address in resolved:
            args.update({f"{key}_{field}": value for field, value in resolved[address].items()})
            args[f"{key}_evidence"] = "hash-matched ELF; recorded return PC" if key == "caller" else "hash-matched ELF; recorded native callback PC, device consumer not established"
            counts[f"{key}_resolved"] += 1
            if key == "handler" and "function" in resolved[address]:
                if "irq" in event.get("cat", "").split(","):
                    event["name"] = f"IRQ {args['irq']} · {resolved[address]['function']}"
                elif "timer" in event.get("cat", "").split(","):
                    event["name"] = f"Timer {args.get('operation', 'callback')} · {resolved[address]['function']}"
        else:
            args[f"{key}_evidence"] = "unresolved recorded PC"
    metadata["source_attribution"] = {"elf_sha256": identity, **counts,
                                      "unique_pcs": len(addresses), "resolved_pcs": len(resolved)}
    if counts["caller_observations"]:
        metadata["warnings"].append("Allocation callers identify the recorded allocator return PC. A libc/Rust allocator wrapper is not proof of the application owner; deeper recorded call stacks or semantic probes are needed for that attribution.")
    if counts["handler_observations"]:
        metadata["warnings"].append("IRQ and timer handlers identify the recorded native callback. A shared GPIO, timer, UART or bus handler does not by itself identify an IMU/GNSS device consumer.")
    return trace


def convert(lines, *, clock=None, source=None, elf_sha256=None, notes_lost=None, complete=None):
    """Return event objects; encode_trace retains their exact decimal numbers.

    notes_lost is an explicit count, "detected" for an unknown nonzero count, or
    None for unknown. A frame's boolean notes_lost is never treated as a count.
    """
    bounded, size = [], 0
    for line in lines:
        size += len(line.encode("utf-8"))
        if len(bounded) >= MAX_LINES or size > MAX_BYTES:
            raise ValueError("Capture exceeds the 128 MiB / one million line bound")
        bounded.append(line.rstrip("\r\n"))
    lines = bounded
    if elf_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", elf_sha256):
        raise ValueError("ELF SHA-256 must contain 64 hexadecimal digits")
    if clock == "" or source == "":
        raise ValueError("Clock and source must be nonempty when supplied")
    warnings = []
    lost_records = None if notes_lost in (None, "unknown", "detected") else _unsigned(notes_lost, "notes_lost")
    loss_detected = True if notes_lost == "detected" else None if lost_records is None else lost_records > 0
    global_loss = loss_detected is True
    begin_indices = [index for index, line in enumerate(lines) if line.startswith(BEGIN)]
    end_indices = [index for index, line in enumerate(lines) if line.startswith(END)]
    framed = bool(begin_indices or end_indices)
    framing = None
    recording_id = None
    if framed:
        if len(begin_indices) != 1 or len(end_indices) != 1 or begin_indices[0] >= end_indices[0]:
            raise ValueError("Expected exactly one ordered NXRS_NOTE_BEGIN/NXRS_NOTE_END pair")
        begin = _frame(lines[begin_indices[0]], BEGIN)
        end = _frame(lines[end_indices[0]], END)
        if begin.get("schema") not in ("1", "2") or begin.get("schema") != end.get("schema"):
            raise ValueError("Unsupported or conflicting capture frame schema")
        recording_id = begin.get("recording_id")
        if recording_id != end.get("recording_id"):
            raise ValueError("Capture frame recording_id is missing or conflicts between begin and end")
        if recording_id is not None and not re.fullmatch(r"[!-~]{1,128}", recording_id):
            raise ValueError("Capture recording_id must contain 1 to 128 non-whitespace ASCII characters")
        declared_clock = begin.get("clock")
        if clock is not None and declared_clock is not None and clock != declared_clock:
            raise ValueError("Supplied clock conflicts with the capture frame")
        clock = clock or declared_clock
        source = source or begin.get("source")
        if not begin.get("source"):
            raise ValueError("Capture frame source is required")
        modes = [_unsigned(fields.get("mode", ""), "frame mode") for fields in (begin, end)]
        begin_loss, end_loss = _bool_field(begin, "notes_lost"), _bool_field(end, "notes_lost")
        frame_loss = begin_loss or end_loss or 2 in modes
        if frame_loss:
            if loss_detected is False:
                raise ValueError("Supplied zero-loss claim conflicts with the capture frame")
            loss_detected, global_loss = True, True
        elif modes == [0, 0]:
            if loss_detected is True:
                warnings.append("Caller reports record loss despite a zero-loss frame; pairing is disabled.")
            else:
                loss_detected, lost_records = False, 0
        else:
            warnings.append("Recorder mode is not bounded non-overwrite mode; absence of lost notes is unverified.")
            if lost_records == 0:
                lost_records, loss_detected = None, None
        declared_complete = _bool_field(end, "complete")
        if complete is True and not declared_complete:
            raise ValueError("Supplied completeness conflicts with the capture frame")
        complete = declared_complete if complete is None else complete and declared_complete
        framing = {"begin": begin, "end": end}
        clock_fields = {key: begin[key] for key in ("clock_bits", "frequency_hz", "clock_epoch") if key in begin}
        if "clock_bits" in clock_fields and not 1 <= _unsigned(clock_fields["clock_bits"], "clock_bits") <= 64:
            raise ValueError("Capture clock_bits must be between 1 and 64")
        if "frequency_hz" in clock_fields and _unsigned(clock_fields["frequency_hz"], "frequency_hz") == 0:
            raise ValueError("Capture frequency_hz must be positive")
        if clock_fields:
            warnings.append("Declared source clock: " + ", ".join(f"{key}={value}" for key, value in clock_fields.items())
                            + ". No counter rollover correction or cross-capture clock synchronization is inferred.")
        if begin.get("capture_mode") == "stopped-checkpoints":
            warnings.append("This recording contains debugger stops at memory checkpoints. The emulator clock pauses while stopped; durations describe this instrumented run, not uninterrupted firmware or physical-MCU performance.")
        lines = lines[begin_indices[0] + 1:end_indices[0]]

    metadata_events = [
        {"name": "process_name", "ph": "M", "pid": pid, "tid": 0, "args": {"name": name}}
        for pid, name in [(1, "NuttX CPU scheduled residency"), (2, "NuttX interrupt intervals"),
                          (3, "NuttX task scheduled residency"), (4, "NuttX application markers"),
                          (5, "Capture evidence"), (6, "NuttX heap observations"),
                          (7, "NuttX timer callbacks")]
    ]
    tracks, events = {}, []
    running, interrupts, markers, wakes, timers = {}, {}, {}, {}, {}
    ambiguous_wakes = set()
    origin = earliest = previous = None
    previous_cpu_edge, previous_marker_edge, previous_timer_edge = {}, {}, {}
    incomplete = recognized = ignored = loss_boundaries = explicit_loss_count = 0
    legacy_switches = 0
    native_counts, timer_counts = {}, {}
    saw_heap_requests = saw_heap_usage = False

    def track(process, key, label):
        identity = process, key
        if identity not in tracks:
            tracks[identity] = len(tracks) + 1
            metadata_events.append({"name": "thread_name", "ph": "M", "pid": process,
                                    "tid": tracks[identity], "args": {"name": label}})
        return tracks[identity]

    def event(process, key, label, name, category, start, args, end=None):
        item = {"name": name, "cat": category, "ph": "i" if end is None else "X",
                "pid": process, "tid": track(process, key, label), "ts": _us(start - origin), "args": args}
        if end is None:
            item["s"] = "t"
        else:
            if end < start:
                raise ValueError("Interval end precedes its start")
            item["dur"] = _us(end - start)
        events.append(item)

    def censored(process, key, label, name, stamp, reason, args=None, category=None):
        nonlocal incomplete
        incomplete += 1
        categories = f"{category},unknown,capture" if category else "unknown,capture"
        event(process, key, label, name, categories, stamp,
              {**(args or {}), "duration_unknown": True, "reason": reason})

    def reset_pending(reason):
        for cpu, (start, name, pid) in running.items():
            censored(1, cpu, f"CPU {cpu}", f"Unclosed scheduled residency: {name}", start, reason,
                     {"cpu": cpu, "task_id": pid})
        running.clear()
        for cpu, stack in interrupts.items():
            for irq, start, name, args in stack:
                censored(2, cpu, f"CPU {cpu} IRQ nesting", name, start, reason, args, category="irq")
        interrupts.clear()
        for cpu, stack in timers.items():
            for start, args in stack:
                censored(7, cpu, f"CPU {cpu} timer callbacks", "Unclosed timer callback", start, reason, args, category="timer")
        timers.clear()
        for pid, stack in markers.items():
            for start, name in stack:
                censored(4, pid, f"Task {pid} application scopes", name, start, reason, {"task_id": pid})
        markers.clear()
        for pid, (start, name, args) in wakes.items():
            censored(3, pid, f"{name} (task {pid})", "Wake without observed dispatch", start, reason, args)
        wakes.clear()
        ambiguous_wakes.clear()

    for number, line in enumerate(lines, 1):
        loss = LOSS.fullmatch(line)
        if loss:
            count = _unsigned(loss[1] or loss[2], "lost event count")
            if count == 0:
                raise ValueError("A loss boundary requires a positive lost event count")
            explicit_loss_count += count
            if explicit_loss_count > U64_MAX:
                raise ValueError("Lost event count overflows u64")
            loss_boundaries += 1
            loss_detected = True
            if origin is not None:
                reset_pending("record loss boundary; no duration is inferred across it")
                event(5, "loss", "Record loss", "Lost records", "loss,capture", previous,
                      {"lost_records": count, "timestamp_basis": "last observed note; loss time is unknown"})
            continue
        match = NOTE.fullmatch(line)
        if match is None:
            if not line.strip():
                continue
            if framed or re.search(r"\[\d+\]\s+\d+\.", line):
                raise ValueError(f"Line {number}: malformed NuttX note row")
            ignored += 1
            continue
        fields = match.groupdict()
        cpu = _unsigned(fields["cpu"], "CPU id")
        stamp = int(fields["seconds"]) * 1_000_000_000 + int(fields["fraction"].ljust(9, "0"))
        if stamp > U64_MAX:
            raise ValueError("NuttX timestamp overflows u64 nanoseconds")
        if origin is None:
            origin = earliest = stamp
        earliest = min(earliest, stamp)
        previous = stamp
        recognized += 1
        kind, payload = fields["kind"], fields["payload"]
        native_counts[kind] = native_counts.get(kind, 0) + 1
        if kind in ("sched_switch", "irq_handler_entry", "irq_handler_exit"):
            # CPUs can append in a different order from their timestamps. Heap
            # and counter notes can also be appended after a preempting IRQ.
            # Pairable scheduler/IRQ edges must still be ordered on each CPU.
            if stamp < previous_cpu_edge.get(cpu, stamp):
                raise ValueError(f"NuttX trace timestamps ran backward on CPU {cpu}")
            previous_cpu_edge[cpu] = stamp
        if global_loss and kind in ("sched_switch", "irq_handler_entry", "irq_handler_exit"):
            # Pairing surviving switches could bridge missing transitions.
            args = {"cpu": cpu, "payload": payload, "duration_unknown": True}
            if kind.startswith("irq_"):
                irq_match = IRQ.search(payload)
                handler = re.search(r"(?:^|\s)name=(\S+)", payload)
                if irq_match:
                    args["irq"] = _unsigned(irq_match[1], "IRQ id")
                if handler:
                    args["handler"] = handler[1]
            category = "irq,unknown,loss" if kind.startswith("irq_") else "unknown,loss"
            event(5, cpu, f"CPU {cpu} notes with unknown coverage", kind, category, stamp, args)
            continue

        if kind == "sched_switch":
            switch = SWITCH.fullmatch(payload)
            if switch is not None:
                prev_name, prev_pid, _, prev_state, next_name, next_pid, _ = switch.groups()
                prev_pid = _unsigned(prev_pid, "previous task id")
            else:
                switch = NEXT_SWITCH.fullmatch(payload)
                if switch is None:
                    raise ValueError(f"Line {number}: malformed scheduler switch")
                next_name, next_pid, _ = switch.groups()
                prev_name, prev_pid, prev_state = "unknown task", None, None
                legacy_switches += 1
            next_pid = _unsigned(next_pid, "next task id")
            if cpu in running and prev_pid is not None and running[cpu][2] != prev_pid:
                # A missing switch can include an earlier dispatch of any
                # pending task. Do not derive latency across that uncertainty.
                for pid, (start, name, args) in wakes.items():
                    censored(3, pid, f"{name} (task {pid})", "Wake across scheduler discontinuity", start,
                             "missing scheduler transitions; dispatch endpoint is ambiguous", args)
                wakes.clear()
                ambiguous_wakes.clear()
            ambiguous_wakes.discard(next_pid)
            if next_pid in wakes:
                start, name, args = wakes.pop(next_pid)
                # A self-switch cannot establish dispatch, even when no earlier
                # switch has established the CPU's running task in this capture.
                if prev_pid != next_pid and stamp >= start and not any(pid == next_pid for _, _, pid in running.values()):
                    event(3, next_pid, f"{name} (task {next_pid})", "Wake to dispatch", "sched,latency", start,
                          {**args, "dispatch_cpu": cpu, "accounting": "elapsed time between observed wake and dispatch; not CPU cost"}, stamp)
                else:
                    censored(3, next_pid, f"{name} (task {next_pid})", "Ambiguous wake to dispatch", start,
                             "task already scheduled or wake/dispatch timestamps conflict", args)
            if cpu in running:
                start, name, pid = running[cpu]
                if prev_pid is None or pid == prev_pid:
                    args = {"cpu": cpu, "task_id": pid, "next_task_id": next_pid,
                            "prev_state": prev_state, "accounting": "scheduled residency; may include IRQ time",
                            "previous_task_verified": prev_pid is not None}
                    event(1, cpu, f"CPU {cpu}", f"{name} [{pid}]", "sched,cpu", start, args, stamp)
                    event(3, pid, f"{name} (task {pid})", f"Scheduled on CPU {cpu}", "sched,task", start, args, stamp)
                else:
                    censored(1, cpu, f"CPU {cpu}", name, start, "scheduler task mismatch; missing transitions",
                             {"expected_task_id": pid, "observed_prev_task_id": prev_pid})
                    warnings.append(f"CPU {cpu}: scheduler continuity broke at {stamp} ns; residency was omitted.")
            else:
                censored(1, cpu, f"CPU {cpu}", f"Unknown preceding residency: {prev_name}", stamp,
                         "capture has no earlier switch/start for this task",
                         {"task_id": prev_pid} if prev_pid is not None else {})
            running[cpu] = stamp, next_name, next_pid
        elif kind in ("irq_handler_entry", "irq_handler_exit"):
            irq_match = IRQ.search(payload)
            if irq_match is None:
                raise ValueError(f"Line {number}: malformed interrupt event")
            irq = _unsigned(irq_match[1], "IRQ id")
            stack = interrupts.setdefault(cpu, [])
            label = f"IRQ {irq}"
            if kind == "irq_handler_entry":
                handler = re.search(r"(?:^|\s)name=(\S+)", payload)
                args = {"cpu": cpu, "irq": irq}
                if handler:
                    label += " " + handler[1]
                    args["handler"] = handler[1]
                if fields["pid"] is not None:
                    args.update(interrupted_task=fields["task"],
                                interrupted_task_id=_unsigned(fields["pid"], "interrupted task id"))
                stack.append((irq, stamp, label, args))
            elif stack and stack[-1][0] == irq:
                _, start, label, args = stack.pop()
                event(2, cpu, f"CPU {cpu} IRQ nesting", label, "irq", start,
                      {**args, "nesting_depth": len(stack)}, stamp)
            else:
                for open_irq, start, name, args in stack:
                    censored(2, cpu, f"CPU {cpu} IRQ nesting", name, start, "IRQ nesting mismatch; end unknown", args, category="irq")
                stack.clear()
                censored(2, cpu, f"CPU {cpu} IRQ nesting", label, stamp,
                         "exit has no matching nested entry", {"cpu": cpu, "irq": irq}, category="irq")
        elif kind == "heap_request":
            request = HEAP_REQUEST.fullmatch(payload)
            if request is None:
                raise ValueError(f"Line {number}: malformed heap allocation request")
            operation, heap, requested, caller = request.groups()
            args = {"heap": heap, "requested_bytes": _unsigned(requested, "heap request size"), "caller": caller,
                    "cpu": cpu, "operation": operation, "outcome": "unknown; allocator entry only"}
            if fields["pid"] is not None:
                args.update(task=fields["task"], task_id=_unsigned(fields["pid"], "request task id"))
            event(6, ("requests", cpu), f"CPU {cpu} heap requests", operation, "heap,request", stamp, args)
            saw_heap_requests = True
        elif kind == "tracing_mark_write":
            watchdog = WATCHDOG.fullmatch(payload)
            if watchdog is not None:
                pid, operation, handler, argument = watchdog.groups()
                if stamp < previous_timer_edge.get(cpu, stamp):
                    raise ValueError(f"Timer callback timestamps ran backward on CPU {cpu}")
                previous_timer_edge[cpu] = stamp
                args = {"cpu": cpu, "handler": handler, "callback_arg": argument, "operation": operation,
                        "identity_semantics": "recorded timer callback and argument; device consumer not inferred"}
                if int(pid) >= 0:
                    args["task_id"] = _unsigned(pid, "timer context task id")
                timer_counts[operation] = timer_counts.get(operation, 0) + 1
                label = f"Timer {operation} · {handler}"
                stack = timers.setdefault(cpu, [])
                if global_loss or operation in ("start", "cancel"):
                    category = "timer"
                    if global_loss and operation in ("enter", "leave"):
                        args["duration_unknown"] = True
                        category += ",unknown,loss"
                    event(7, cpu, f"CPU {cpu} timer callbacks", label, category, stamp, args)
                elif operation == "enter":
                    stack.append((stamp, args))
                elif stack and (stack[-1][1]["handler"], stack[-1][1]["callback_arg"]) == (handler, argument):
                    start, args = stack.pop()
                    event(7, cpu, f"CPU {cpu} timer callbacks", f"Timer callback · {handler}", "timer", start,
                          {**args, "operation": "callback", "nesting_depth": len(stack),
                           "accounting": "elapsed callback interval; can include nested interrupts"}, stamp)
                else:
                    for start, prior in stack:
                        censored(7, cpu, f"CPU {cpu} timer callbacks", "Timer callback", start,
                                 "callback nesting mismatch; end unknown", prior, category="timer")
                    stack.clear()
                    censored(7, cpu, f"CPU {cpu} timer callbacks", label, stamp,
                             "callback leave has no matching nested entry", args, category="timer")
                continue
            usage = HEAP_USAGE.fullmatch(payload)
            if usage is not None:
                used, operation, heap, size, address = usage.groups()
                events.append({"ph": "C", "name": f"Heap {heap}", "cat": "heap,counter", "pid": 6,
                               "tid": track(6, ("usage", heap), f"Heap {heap} usage"), "ts": _us(stamp - origin),
                               "args": {"used_bytes": _unsigned(used, "heap usage")}})
                if size is not None:
                    category = {"malloc": "allocation", "free": "free", "add": "region", "remove": "region"}.get(operation, "observation")
                    args = {"heap": heap, "operation": operation, "address": address,
                            "bytes": _unsigned(size, "heap note size"), "used_bytes": _unsigned(used, "heap usage"), "cpu": cpu,
                            "size_semantics": "native allocator note; may include metadata or internal chunks",
                            "address_semantics": "native note address; not a qualified allocation identity"}
                    if fields["pid"] is not None:
                        args.update(task=fields["task"], task_id=_unsigned(fields["pid"], "heap task id"))
                    event(6, ("operations", heap), f"Heap {heap} operations", f"Heap {operation}",
                          f"heap,{category}", stamp, args)
                saw_heap_usage = True
                continue
            pieces = payload.split("|")
            phase = pieces[0]
            if phase == "E" and len(pieces) == 1 and fields["pid"] is not None:
                pieces.append(fields["pid"])
            if phase not in ("B", "E", "I", "C") or len(pieces) < 2:
                event(4, cpu, f"CPU {cpu} unrecognized markers", "Unrecognized application marker", "app,unknown", stamp, {"payload": payload})
                warnings.append(f"Line {number}: unsupported application marker retained as an instant.")
                continue
            pid = _unsigned(pieces[1], "marker task id")
            if phase in ("B", "E"):
                if stamp < previous_marker_edge.get(pid, stamp):
                    raise ValueError(f"Application scope timestamps ran backward for task {pid}")
                previous_marker_edge[pid] = stamp
                if global_loss:
                    event(4, pid, f"Task {pid} application scopes", "Unpaired application marker", "unknown,loss", stamp,
                          {"task_id": pid, "payload": payload, "duration_unknown": True})
                    continue
            stack = markers.setdefault(pid, [])
            if phase == "B" and len(pieces) == 3 and pieces[2]:
                stack.append((stamp, pieces[2]))
            elif phase == "E" and len(pieces) in (2, 3):
                if stack and (len(pieces) == 2 or pieces[2] == stack[-1][1]):
                    start, name = stack.pop()
                    event(4, pid, f"Task {pid} application scopes", name, "app", start, {"task_id": pid}, stamp)
                else:
                    for start, name in stack:
                        censored(4, pid, f"Task {pid} application scopes", name, start, "application marker nesting mismatch")
                    stack.clear()
                    censored(4, pid, f"Task {pid} application scopes", pieces[2] if len(pieces) == 3 else "Unmatched application end", stamp, "end has no matching begin")
            elif phase == "I" and len(pieces) == 3 and pieces[2]:
                event(4, pid, f"Task {pid} application scopes", pieces[2], "app", stamp, {"task_id": pid})
            elif phase == "C" and len(pieces) == 4 and pieces[2]:
                try:
                    value = Decimal(pieces[3])
                except InvalidOperation as error:
                    raise ValueError(f"Line {number}: malformed marker counter") from error
                if not value.is_finite() or not math.isfinite(float(value)):
                    raise ValueError("Counter value exceeds finite plotting range")
                events.append({"ph": "C", "name": pieces[2], "cat": "app,counter", "pid": 4,
                               "tid": track(4, pid, f"Task {pid} application scopes"), "ts": _us(stamp - origin),
                               "args": {"value": value}})
            else:
                raise ValueError(f"Line {number}: malformed application marker")
        elif kind == "sched_waking":
            wake = WAKE.fullmatch(payload)
            if wake is None:
                raise ValueError(f"Line {number}: malformed scheduler wake")
            name, pid, target_cpu = wake.groups()
            pid, target_cpu = _unsigned(pid, "wake task id"), _unsigned(target_cpu, "wake CPU id")
            args = {"task_id": pid, "task": name, "target_cpu": target_cpu, "cpu": cpu}
            event(3, int(pid), f"{name} (task {pid})", "Wake requested", "sched,wake", stamp,
                  args)
            if not global_loss:
                if pid in wakes:
                    start, prior_name, prior_args = wakes.pop(pid)
                    censored(3, pid, f"{prior_name} (task {pid})", "Repeated wake before observed dispatch", start,
                             "multiple wakes; first ready transition is ambiguous", prior_args)
                    ambiguous_wakes.add(pid)
                elif pid not in ambiguous_wakes and not any(running_pid == pid for _, _, running_pid in running.values()):
                    wakes[pid] = stamp, name, args
        else:
            event(5, cpu, f"CPU {cpu} other notes", kind, "note", stamp, {"cpu": cpu, "payload": payload})

    if origin is None or not recognized:
        raise ValueError("The capture has no timestamped NuttX notes")
    reset_pending("capture ended before the corresponding end/switch; duration unknown")
    if global_loss:
        complete = False
        warnings.append("Record loss has no located boundary: all duration reconstruction was disabled; surviving observations are instants and counters only.")
    if loss_boundaries:
        complete = False
        if lost_records is not None and lost_records > 0 and explicit_loss_count > lost_records:
            raise ValueError("Located loss boundaries exceed the declared total lost-record count")
        if not global_loss:
            lost_records = explicit_loss_count
        warnings.append(f"{loss_boundaries} explicit loss boundary/boundaries reset pending intervals.")
    if incomplete:
        warnings.append(f"{incomplete} incomplete interval observation(s) retained as markers with no duration.")
        complete = False
    if ignored:
        warnings.append(f"Ignored {ignored} non-note console line(s) outside framed capture data.")
    if legacy_switches:
        warnings.append(f"{legacy_switches} scheduler switch(es) omit the previous task; residency uses observed switch endpoints but previous-task continuity cannot be checked.")
    if saw_heap_requests:
        warnings.append("Heap requests are allocator-entry observations, including failures, retries and internal backing calls; they do not establish successful allocations or lifetimes.")
    if saw_heap_usage:
        warnings.append("Heap usage is a sampled native used-byte counter. Operation notes retain raw native addresses and sizes, whose semantics can differ between allocator paths; they do not establish allocation lifetimes or heap topology.")
    if not native_counts.get("irq_handler_entry"):
        warnings.append("No interrupt entries were captured. Enable IRQ recording with trace mode +i for interrupt timing; absence of notes does not mean no IRQs occurred.")
    if not saw_heap_requests:
        warnings.append("No allocation requests were captured. A diagnostic build with MM_ALLOC_REQUEST_TRACE and active heap recording is required to identify request callers.")
    if clock is None:
        warnings.append("Clock unspecified; timestamps are supplied note coordinates, not independently qualified wall time.")
    if source is None:
        warnings.append("Capture source unspecified; QEMU and physical hardware are not inferred.")
    if loss_detected is None:
        warnings.append("Record-loss status is unknown; a missing interval does not prove idle time.")
    warnings.append("CPU and per-task tracks show the same scheduled residency; IRQ and app scopes overlap them and must not be added as CPU time.")
    if earliest < origin:
        shift = _us(origin - earliest)
        for item in events:
            item["ts"] += shift
        origin = earliest
    events.sort(key=lambda item: (item["ts"], -item.get("dur", 0)))
    nxrs = {"schema_version": 1, "clock": clock, "source": source, "origin_ns": origin,
            "recording_id": recording_id,
            "elf_sha256": elf_sha256.lower() if elf_sha256 else None, "lost_records": lost_records,
            "loss_detected": loss_detected, "complete": complete, "incomplete_intervals": incomplete,
            "warnings": warnings, "input_format": "nuttx-noteram-ascii"}
    nxrs["event_counts"] = native_counts
    nxrs["timer_event_counts"] = timer_counts
    nxrs["observed_note_count"] = recognized
    nxrs["ignored_line_count"] = ignored
    nxrs["observed_start_ns"] = earliest
    nxrs["observed_end_ns"] = max(origin + int(item["ts"] * 1000) + int(item.get("dur", 0) * 1000) for item in events)
    if loss_boundaries:
        nxrs["located_lost_records"] = explicit_loss_count
    if framing is not None:
        nxrs["capture_frames"] = framing
    return {"traceEvents": metadata_events + events, "displayTimeUnit": "ns", "otherData": {"nxrs": nxrs}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="Console text from trace dump or framed noteram capture")
    parser.add_argument("output", type=Path, help="Chrome/Perfetto Trace Event JSON output")
    parser.add_argument("--clock", help="Explicit target clock description; omitted means unknown")
    parser.add_argument("--source", help="Explicit capture origin, for example a measured QEMU run")
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--elf-sha256", help="Explicit final ELF SHA-256 identity")
    identity.add_argument("--elf", type=Path, help="Hash this final ELF and resolve recorded callers/IRQ handlers; not target attestation")
    parser.add_argument("--addr2line", help="Target addr2line executable; otherwise discover from ELF architecture and PATH")
    parser.add_argument("--notes-lost", help="Explicit lost-record count, 'detected' for unknown count, or 'unknown'")
    parser.add_argument("--complete", action="store_true", default=None, help="Declare complete capture; cannot override censored intervals or reported loss")
    args = parser.parse_args()
    if args.capture.resolve() == args.output.resolve():
        parser.error("The capture and output must be different files")
    if args.elf and args.elf.resolve() == args.output.resolve():
        parser.error("The symbol ELF and output must be different files")
    if args.addr2line and not args.elf:
        parser.error("--addr2line requires --elf")
    try:
        if args.capture.stat().st_size > MAX_BYTES:
            raise ValueError("Capture exceeds the 128 MiB import bound")
        identity = elf_identity(args.elf) if args.elf else args.elf_sha256
        with args.capture.open(encoding="utf-8", errors="strict") as capture:
            trace = convert(capture, clock=args.clock, source=args.source, elf_sha256=identity,
                            notes_lost=args.notes_lost, complete=args.complete)
        if args.elf:
            symbolize_trace(trace, args.elf, addr2line=args.addr2line)
        args.output.write_text(encode_trace(trace) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
