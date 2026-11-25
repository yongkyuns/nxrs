#!/usr/bin/env python3
"""Convert scheduler switches and interrupt intervals from NuttX trace dump."""

import argparse
import json
from pathlib import Path
import re


NOTE = re.compile(
    r"\[(\d+)\]\s+(\d+)\.(\d{1,9}): "
    r"(sched_switch|irq_handler_entry|irq_handler_exit):\s*(.*)"
)
NEXT_TASK = re.compile(r"\bnext_comm=(.*?) next_pid=(\d+)\s+next_prio=")
IRQ = re.compile(r"\birq=(\d+)")


def convert(lines):
    metadata = [
        {"name": "process_name", "ph": "M", "pid": pid, "tid": 0, "args": {"name": name}}
        for pid, name in [(1, "NuttX scheduler"), (2, "NuttX interrupt handlers")]
    ]
    tracks = {}
    spans = []
    running = {}
    interrupts = {}
    origin = previous = None

    def span(process, track, name, start, end, args):
        if end == start:
            return
        key = process, track
        if key not in tracks:
            tracks[key] = len(tracks) + 1
            metadata.append({
                "name": "thread_name", "ph": "M", "pid": process,
                "tid": tracks[key], "args": {"name": track},
            })
        spans.append({
            "name": name, "ph": "X", "pid": process, "tid": tracks[key],
            "ts": (start - origin) / 1000, "dur": (end - start) / 1000, "args": args,
        })

    for line in lines:
        match = NOTE.search(line)
        if match is None:
            continue
        cpu, seconds, fraction, kind, payload = match.groups()
        stamp = int(seconds) * 1_000_000_000 + int(fraction.ljust(9, "0"))
        if previous is not None and stamp < previous:
            raise ValueError("NuttX trace timestamps ran backward")
        if origin is None:
            origin = stamp
        previous = stamp
        if kind == "sched_switch":
            task = NEXT_TASK.search(payload)
            if task is None:
                raise ValueError("Malformed scheduler switch")
            if cpu in running:
                start, name, tid = running[cpu]
                span(1, f"CPU {cpu}", name, start, stamp, {"NuttX task ID": int(tid)})
            running[cpu] = stamp, *task.groups()
        else:
            irq = IRQ.search(payload)
            if irq is None:
                raise ValueError("Malformed interrupt event")
            name = f"IRQ {irq[1]}"
            stack = interrupts.setdefault((cpu, name), [])
            if kind == "irq_handler_entry":
                stack.append(stamp)
            elif stack:
                span(2, f"CPU {cpu} {name}", name, stack.pop(), stamp, {})

    if not spans:
        raise ValueError("The trace has no complete scheduler or interrupt intervals")
    spans.sort(key=lambda event: event["ts"])
    return {"traceEvents": metadata + spans}


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
