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
    r"C\|-?\d+\|Heap Usage\|(\d+)\|\w+:\s+heap:\s*(\S+)(?:\s+.*)?"
)
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
    if framed:
        if len(begin_indices) != 1 or len(end_indices) != 1 or begin_indices[0] >= end_indices[0]:
            raise ValueError("Expected exactly one ordered NXRS_NOTE_BEGIN/NXRS_NOTE_END pair")
        begin = _frame(lines[begin_indices[0]], BEGIN)
        end = _frame(lines[end_indices[0]], END)
        if begin.get("schema") not in ("1", "2") or begin.get("schema") != end.get("schema"):
            raise ValueError("Unsupported or conflicting capture frame schema")
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
        lines = lines[begin_indices[0] + 1:end_indices[0]]

    metadata_events = [
        {"name": "process_name", "ph": "M", "pid": pid, "tid": 0, "args": {"name": name}}
        for pid, name in [(1, "NuttX CPU scheduled residency"), (2, "NuttX interrupt intervals"),
                          (3, "NuttX task scheduled residency"), (4, "NuttX application markers"),
                          (5, "Capture evidence"), (6, "NuttX heap observations")]
    ]
    tracks, events = {}, []
    running, interrupts, markers = {}, {}, {}
    origin = earliest = previous = None
    previous_cpu_edge, previous_marker_edge = {}, {}
    incomplete = recognized = ignored = loss_boundaries = explicit_loss_count = 0
    legacy_switches = 0
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

    def censored(process, key, label, name, stamp, reason, args=None):
        nonlocal incomplete
        incomplete += 1
        event(process, key, label, name, "unknown,capture", stamp,
              {**(args or {}), "duration_unknown": True, "reason": reason})

    def reset_pending(reason):
        for cpu, (start, name, pid) in running.items():
            censored(1, cpu, f"CPU {cpu}", f"Unclosed scheduled residency: {name}", start, reason,
                     {"cpu": cpu, "task_id": pid})
        running.clear()
        for cpu, stack in interrupts.items():
            for irq, start, name in stack:
                censored(2, cpu, f"CPU {cpu} IRQ nesting", name, start, reason, {"cpu": cpu, "irq": irq})
        interrupts.clear()
        for pid, stack in markers.items():
            for start, name in stack:
                censored(4, pid, f"Task {pid} application scopes", name, start, reason, {"task_id": pid})
        markers.clear()

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
        if kind in ("sched_switch", "irq_handler_entry", "irq_handler_exit"):
            # CPUs can append in a different order from their timestamps. Heap
            # and counter notes can also be appended after a preempting IRQ.
            # Pairable scheduler/IRQ edges must still be ordered on each CPU.
            if stamp < previous_cpu_edge.get(cpu, stamp):
                raise ValueError(f"NuttX trace timestamps ran backward on CPU {cpu}")
            previous_cpu_edge[cpu] = stamp
        if global_loss and kind in ("sched_switch", "irq_handler_entry", "irq_handler_exit"):
            # Pairing surviving switches could bridge missing transitions.
            event(5, cpu, f"CPU {cpu} notes with unknown coverage", kind, "unknown,loss", stamp,
                  {"cpu": cpu, "payload": payload, "duration_unknown": True})
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
                if handler:
                    label += " " + handler[1]
                stack.append((irq, stamp, label))
            elif stack and stack[-1][0] == irq:
                _, start, label = stack.pop()
                event(2, cpu, f"CPU {cpu} IRQ nesting", label, "irq", start,
                      {"cpu": cpu, "irq": irq, "nesting_depth": len(stack)}, stamp)
            else:
                for open_irq, start, name in stack:
                    censored(2, cpu, f"CPU {cpu} IRQ nesting", name, start, "IRQ nesting mismatch; end unknown", {"irq": open_irq})
                stack.clear()
                censored(2, cpu, f"CPU {cpu} IRQ nesting", label, stamp,
                         "exit has no matching nested entry", {"irq": irq})
        elif kind == "heap_request":
            request = HEAP_REQUEST.fullmatch(payload)
            if request is None:
                raise ValueError(f"Line {number}: malformed heap allocation request")
            operation, heap, requested, caller = request.groups()
            args = {"heap": heap, "requested_bytes": _unsigned(requested, "heap request size"), "caller": caller}
            if fields["pid"] is not None:
                args.update(task=fields["task"], task_id=_unsigned(fields["pid"], "request task id"))
            event(6, ("requests", cpu), f"CPU {cpu} heap requests", operation, "heap,request", stamp, args)
            saw_heap_requests = True
        elif kind == "tracing_mark_write":
            usage = HEAP_USAGE.fullmatch(payload)
            if usage is not None:
                used, heap = usage.groups()
                events.append({"ph": "C", "name": f"Heap {heap}", "cat": "heap,counter", "pid": 6,
                               "tid": track(6, ("usage", heap), f"Heap {heap} usage"), "ts": _us(stamp - origin),
                               "args": {"used_bytes": _unsigned(used, "heap usage")}})
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
            event(3, int(pid), f"{name} (task {pid})", "Wake requested", "sched,wake", stamp,
                  {"task_id": int(pid), "target_cpu": int(target_cpu), "cpu": cpu})
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
        warnings.append("Heap usage is a sampled native used-byte counter; it does not establish individual allocation addresses or lifetimes.")
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
            "elf_sha256": elf_sha256.lower() if elf_sha256 else None, "lost_records": lost_records,
            "loss_detected": loss_detected, "complete": complete, "incomplete_intervals": incomplete,
            "warnings": warnings, "input_format": "nuttx-noteram-ascii"}
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
    identity.add_argument("--elf", type=Path, help="Record SHA-256 of this final ELF; not target attestation")
    parser.add_argument("--notes-lost", help="Explicit lost-record count, 'detected' for unknown count, or 'unknown'")
    parser.add_argument("--complete", action="store_true", default=None, help="Declare complete capture; cannot override censored intervals or reported loss")
    args = parser.parse_args()
    if args.capture.resolve() == args.output.resolve():
        parser.error("The capture and output must be different files")
    try:
        if args.capture.stat().st_size > MAX_BYTES:
            raise ValueError("Capture exceeds the 128 MiB import bound")
        identity = hashlib.sha256(args.elf.read_bytes()).hexdigest() if args.elf else args.elf_sha256
        with args.capture.open(encoding="utf-8", errors="strict") as capture:
            trace = convert(capture, clock=args.clock, source=args.source, elf_sha256=identity,
                            notes_lost=args.notes_lost, complete=args.complete)
        args.output.write_text(encode_trace(trace) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
