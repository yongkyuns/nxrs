#!/usr/bin/env python3
"""Render a qualified heap journal as exact Chrome Trace Event JSON.

This is a derived view of the same stopped debugger recording. Its durations
describe instrumented lock windows, not uninterrupted allocator or CPU costs.
The collector must validate its journal before calling this fixture helper.
"""

from decimal import Decimal
import importlib.util
from pathlib import Path


def trace_view(journal):
    origin = journal["initial"]["time_ns"]
    events = [
        {"ph": "M", "pid": 1, "name": "process_name",
         "args": {"name": "Isolated heap · debugger-instrumented QEMU"}},
        {"ph": "M", "pid": 1, "tid": 1, "name": "thread_name",
         "args": {"name": "Allocator mutation windows"}},
        {"ph": "M", "pid": 1, "tid": 2, "name": "thread_name",
         "args": {"name": "Committed states and safe observations"}},
    ]

    def timestamp(time_ns):
        if not isinstance(time_ns, int) or not origin <= time_ns < 2**64:
            raise ValueError("Derived trace time is outside its source epoch")
        return Decimal(time_ns - origin).scaleb(-3)

    def instant(name, time_ns, sequence, evidence):
        events.append({"ph": "i", "s": "t", "pid": 1, "tid": 2,
                       "name": name, "cat": "heap.observation", "ts": timestamp(time_ns),
                       "args": {"sequence": sequence, "heap_id": journal["heap"]["id"],
                                "heap_epoch": journal["heap"]["epoch"], "evidence": evidence}})

    instant("Initial heap checkpoint", origin, journal["initial"]["sequence"], "observed checkpoint")
    lost = False
    for record in journal["records"]:
        kind = record["kind"]
        if kind == "transaction":
            start, commit = record["start_ns"], record["commit_ns"]
            if commit < start:
                raise ValueError("Derived mutation window runs backward")
            sequence = record["sequence"]
            events.append({"ph": "X", "pid": 1, "tid": 1,
                           "name": record.get("label") or f"Allocator phase {sequence}",
                           "cat": "heap.mutation", "ts": timestamp(start),
                           "dur": Decimal(commit - start).scaleb(-3),
                           "args": {"sequence": sequence, "heap_id": journal["heap"]["id"],
                                    "measurement": "locked mutation window; debugger stops and wrappers perturb execution",
                                    "source_start_ns": start, "source_commit_ns": commit}})
            instant(f"Heap commit #{sequence}", commit, sequence, "reconstructed commit boundary")
        elif kind == "checkpoint":
            instant(record.get("label") or "Complete heap checkpoint", record["time_ns"],
                    record["sequence"], "observed checkpoint")
        elif kind == "loss":
            lost = True
            events.append({"ph": "i", "s": "t", "pid": 1, "tid": 2,
                           "name": "Heap evidence lost", "cat": "heap.loss",
                           "ts": timestamp(record["time_ns"]), "args": {"reason": record["reason"]}})
        else:
            raise ValueError("Unknown heap journal record")
    closing = journal.get("closing")
    if closing is not None:
        instant("End of qualified heap coverage", closing["time_ns"], closing["sequence"], "coverage receipt")
    return {
        "displayTimeUnit": "ns",
        "otherData": {"nxrs": {
            "schema_version": 1, "recording_id": journal["recording_id"],
            "elf_sha256": journal["build"]["elf_sha256"], "clock": journal["clock"],
            "source": "derived-stopped-heap-journal; debugger-instrumented, not production timing",
            "origin_ns": origin, "complete": closing is not None and not lost,
            "lost_records": "detected" if lost else 0,
        }},
        "traceEvents": events,
    }


def write_trace(journal, destination):
    # Share the existing exact Decimal encoder rather than round nanoseconds
    # through binary floating point or introduce a second trace wire format.
    path = Path(__file__).resolve().parents[2] / "nuttx-trace-to-perfetto.py"
    spec = importlib.util.spec_from_file_location("nxrs_trace_encoder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Path(destination).open("x", encoding="utf-8") as stream:
        stream.write(module.encode_trace(trace_view(journal)) + "\n")
