#!/usr/bin/env python3
"""Convert scheduler, interrupt and heap events from NuttX trace dump."""

import argparse
import json
from pathlib import Path
import re


NOTE = re.compile(
    r"\[(\d+)\]\s+(\d+)\.(\d{1,9}): "
    r"(sched_switch|irq_handler_entry|irq_handler_exit|heap_request|tracing_mark_write):\s*(.*)"
)
NEXT_TASK = re.compile(r"\bnext_comm=(.*?) next_pid=(\d+)\s+next_prio=")
IRQ = re.compile(r"\birq=(\d+)")
TASK = re.compile(r"^\s*(.+?)-(\d+)\s+\[")
HEAP_REQUEST = re.compile(
    r"(malloc|memalign|realloc) heap:\s*(\S+) size:\s*(\d+) caller:\s*(\S+)"
)
HEAP_USAGE = re.compile(r"^C\|-?\d+\|Heap Usage\|(\d+)\|\w+: heap:\s*(\S+) ")


def convert(lines):
    metadata = [
        {"name": "process_name", "ph": "M", "pid": pid, "tid": 0, "args": {"name": name}}
        for pid, name in [(1, "NuttX scheduler"), (2, "NuttX interrupt handlers"), (3, "NuttX heap")]
    ]
    tracks = {}
    events = []
    running = {}
    interrupts = {}
    previous = {}
    origin = earliest = None

    def track_id(process, track):
        key = process, track
        if key not in tracks:
            tracks[key] = len(tracks) + 1
            metadata.append({
                "name": "thread_name", "ph": "M", "pid": process,
                "tid": tracks[key], "args": {"name": track},
            })
        return tracks[key]

    def span(process, track, name, start, end, args):
        if end == start:
            return
        events.append({
            "name": name, "ph": "X", "pid": process, "tid": track_id(process, track),
            "ts": (start - origin) / 1000, "dur": (end - start) / 1000, "args": args,
        })

    for line in lines:
        match = NOTE.search(line)
        if match is None:
            continue
        cpu, seconds, fraction, kind, payload = match.groups()
        usage = HEAP_USAGE.match(payload) if kind == "tracing_mark_write" else None
        if kind == "tracing_mark_write" and usage is None:
            continue
        stamp = int(seconds) * 1_000_000_000 + int(fraction.ljust(9, "0"))
        if origin is None:
            origin = earliest = stamp
        earliest = min(earliest, stamp)
        if kind in ("sched_switch", "irq_handler_entry", "irq_handler_exit"):
            # Different CPUs can append in an order different from timestamp order.
            if stamp < previous.get(cpu, stamp):
                raise ValueError(f"NuttX trace timestamps ran backward on CPU {cpu}")
            previous[cpu] = stamp
        if kind == "sched_switch":
            task = NEXT_TASK.search(payload)
            if task is None:
                raise ValueError("Malformed scheduler switch")
            if cpu in running:
                start, name, tid = running[cpu]
                span(1, f"CPU {cpu}", name, start, stamp, {"NuttX task ID": int(tid)})
            running[cpu] = stamp, *task.groups()
        elif kind in ("irq_handler_entry", "irq_handler_exit"):
            irq = IRQ.search(payload)
            if irq is None:
                raise ValueError("Malformed interrupt event")
            name = f"IRQ {irq[1]}"
            stack = interrupts.setdefault((cpu, name), [])
            if kind == "irq_handler_entry":
                stack.append(stamp)
            elif stack:
                span(2, f"CPU {cpu} {name}", name, stack.pop(), stamp, {})
        elif kind == "heap_request":
            request = HEAP_REQUEST.fullmatch(payload.strip())
            if request is None:
                raise ValueError("Malformed heap allocation request")
            operation, heap, size, caller = request.groups()
            args = {"heap": heap, "requested_bytes": int(size), "caller": caller}
            if task := TASK.match(line):
                args.update(task=task[1], task_id=int(task[2]))
            events.append({
                "name": operation, "ph": "i", "s": "t", "pid": 3,
                "tid": track_id(3, f"CPU {cpu} heap requests"),
                "ts": (stamp - origin) / 1000,
                "args": args,
            })
        else:
            size, heap = usage.groups()
            events.append({
                "name": f"Heap {heap}", "ph": "C", "pid": 3,
                "ts": (stamp - origin) / 1000, "args": {"used_bytes": int(size)},
            })

    if not events:
        raise ValueError("The trace has no complete scheduler or interrupt intervals or heap events")
    if earliest < origin:
        shift = (origin - earliest) / 1000
        for event in events:
            event["ts"] += shift
    events.sort(key=lambda event: event["ts"])
    return {"traceEvents": metadata + events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="Console text from trace dump")
    parser.add_argument("output", type=Path, help="Perfetto JSON output")
    args = parser.parse_args()
    if args.capture.resolve() == args.output.resolve():
        parser.error("The capture and output must be different files")
    try:
        with args.capture.open(errors="replace") as capture:
            trace = convert(capture)
        with args.output.open("w", encoding="utf-8") as output:
            json.dump(trace, output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
