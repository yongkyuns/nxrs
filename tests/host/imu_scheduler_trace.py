#!/usr/bin/env python3
"""Validate and summarize bounded NuttX /dev/note/ram captures."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

BEGIN = "NXRS_NOTE_BEGIN "
END = "NXRS_NOTE_END "
MAX_CAPTURE_BYTES = 1_048_576
MAX_CAPTURE_LINES = 100_000
EXPECTED_WINDOW_MS = 200
I2C_LABELS = {
    "nxrs.i2c.char_lock_wait",
    "nxrs.i2c.esp32s3_lock_wait",
    "nxrs.i2c.esp32s3_service",
}

HEADER = re.compile(
    r"^\s*(?P<task>.+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\]\s+"
    r"(?P<seconds>\d+)\.(?P<nanos>\d{9}):\s*(?P<body>.*)$"
)
IRQ_ENTRY = re.compile(r"^irq_handler_entry: irq=(\d+) name=\S+$")
IRQ_EXIT = re.compile(r"^irq_handler_exit: irq=(\d+) ret=handled$")
WAKE = re.compile(r"^sched_waking: comm=(\S+) pid=(\d+) target_cpu=(\d+)$")
SWITCH = re.compile(
    r"^sched_switch: prev_comm=(\S+) prev_pid=(\d+) prev_prio=\d+ "
    r"prev_state=. ==> next_comm=(\S+) next_pid=(\d+) next_prio=\d+$"
)
I2C_MARK = re.compile(r"^tracing_mark_write: ([BE])\|(\d+)\|([^|]+)$")


class CaptureError(ValueError):
    pass


@dataclass(frozen=True)
class Row:
    kind: str
    timestamp_ns: int
    task: str
    pid: int
    cpu: int
    irq: int | None = None
    comm: str | None = None
    target_pid: int | None = None
    label: str | None = None
    marker_pid: int | None = None


def _fields(line: str, prefix: str) -> dict[str, str]:
    if not line.startswith(prefix):
        raise CaptureError(f"missing {prefix.strip()} frame")
    result: dict[str, str] = {}
    for field in line[len(prefix) :].split():
        if "=" not in field:
            raise CaptureError(f"malformed frame field {field!r}")
        key, value = field.split("=", 1)
        if not value or key in result:
            raise CaptureError(f"duplicate or empty frame field {key!r}")
        result[key] = value
    return result


def _get(fields: dict[str, str], key: str) -> str:
    try:
        return fields[key]
    except KeyError as error:
        raise CaptureError(f"missing field {key!r}") from error


def _int(fields: dict[str, str], key: str, minimum: int = 0) -> int:
    raw = _get(fields, key)
    try:
        value = int(raw, 10)
    except ValueError as error:
        raise CaptureError(f"invalid integer {key}={raw!r}") from error
    if value < minimum:
        raise CaptureError(f"out-of-range {key}={value}")
    return value


def _bool(fields: dict[str, str], key: str) -> bool:
    value = _get(fields, key)
    if value not in ("0", "1"):
        raise CaptureError(f"invalid boolean {key}={value!r}")
    return value == "1"


def _parse_row(line: str, number: int) -> Row:
    match = HEADER.fullmatch(line)
    if match is None:
        raise CaptureError(f"line {number}: malformed NuttX note row")
    seconds = int(match["seconds"])
    nanos = int(match["nanos"])
    if nanos >= 1_000_000_000:
        raise CaptureError(f"line {number}: invalid nanoseconds")
    common = dict(
        timestamp_ns=seconds * 1_000_000_000 + nanos,
        task=match["task"].strip(),
        pid=int(match["pid"]),
        cpu=int(match["cpu"]),
    )
    body = match["body"]
    if found := IRQ_ENTRY.fullmatch(body):
        return Row("irq_entry", irq=int(found[1]), **common)
    if found := IRQ_EXIT.fullmatch(body):
        return Row("irq_exit", irq=int(found[1]), **common)
    if found := WAKE.fullmatch(body):
        return Row(
            "worker_wake" if found[1] == "imu-acquire" else "other",
            comm=found[1], target_pid=int(found[2]), **common
        )
    if found := SWITCH.fullmatch(body):
        return Row(
            "worker_switch" if found[3] == "imu-acquire" else "other",
            comm=found[3], target_pid=int(found[4]), **common
        )
    if body.startswith("tracing_mark_write:"):
        found = I2C_MARK.fullmatch(body)
        if found and found[3] in I2C_LABELS:
            return Row(
                "i2c_begin" if found[1] == "B" else "i2c_end",
                label=found[3], marker_pid=int(found[2]), **common
            )
        if "nxrs.i2c." in body:
            raise CaptureError(f"line {number}: malformed I2C scheduler note")
    if body.startswith(("irq_handler_entry:", "irq_handler_exit:", "sched_waking:", "sched_switch:")):
        raise CaptureError(f"line {number}: malformed relevant note {body!r}")
    return Row("other", **common)


def _distribution(values: list[int]) -> dict[str, int | None]:
    ordered = sorted(values)

    def percentile(p: int) -> int | None:
        if not ordered:
            return None
        rank = (len(ordered) * p + 99) // 100
        return ordered[max(0, rank - 1)]

    return {
        "p50_nearest_rank": percentile(50),
        "p95_nearest_rank": percentile(95),
        "p99_nearest_rank": percentile(99),
        "max": max(ordered) if ordered else None,
        "samples": len(ordered),
    }


def analyze(text: str, *, expected_window_ms: int = EXPECTED_WINDOW_MS) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_CAPTURE_BYTES:
        raise CaptureError("capture exceeds one-MiB read bound")
    lines = text.replace("\r", "").splitlines()
    if len(lines) > MAX_CAPTURE_LINES + 2:
        raise CaptureError("capture exceeds line bound")
    begins = [i for i, line in enumerate(lines) if line.startswith(BEGIN)]
    ends = [i for i, line in enumerate(lines) if line.startswith(END)]
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise CaptureError("expected exactly one ordered begin/end frame")
    first, last = begins[0], ends[0]
    begin, end = _fields(lines[first], BEGIN), _fields(lines[last], END)

    schema, end_schema = _int(begin, "schema"), _int(end, "schema")
    if schema not in (1, 2) or schema != end_schema:
        raise CaptureError("unsupported capture schema")
    if _get(begin, "source") != "nuttx-noteram-ascii":
        raise CaptureError("unsupported capture source")
    if _get(begin, "clock") != "perf_gettime-converted":
        raise CaptureError("unsupported capture clock")
    cpus = _int(begin, "cpus", 1)
    if cpus != 1:
        raise CaptureError("only UP traces are supported")
    irq_text = _get(begin, "irq")
    irq = None if irq_text == "none" else _int(begin, "irq")
    i2c_enabled = _bool(begin, "i2c") if schema == 2 else False
    if schema == 1 and irq is None:
        raise CaptureError("schema 1 requires a numeric IRQ")
    if schema == 2 and irq is None and not i2c_enabled:
        raise CaptureError("capture enables neither IRQ nor I2C notes")
    worker = _get(begin, "worker")
    if worker != "imu-acquire":
        raise CaptureError("unexpected worker name")
    window_ms = _int(begin, "window_ms", 1)
    mode, end_mode = _int(begin, "mode"), _int(end, "mode")
    notes_lost, end_lost = _bool(begin, "notes_lost"), _bool(end, "notes_lost")
    complete = _bool(end, "complete")
    if mode != end_mode or notes_lost != end_lost:
        raise CaptureError("begin/end loss metadata disagree")

    rows: list[Row] = []
    previous_time: int | None = None
    nonmonotonic = 0
    for index in range(first + 1, last):
        if not lines[index].strip():
            continue
        row = _parse_row(lines[index], index + 1)
        if previous_time is not None and row.timestamp_ns < previous_time:
            nonmonotonic += 1
        previous_time = row.timestamp_ns
        rows.append(row)

    invalid: set[str] = set()
    if mode == 2 or notes_lost:
        invalid.add("note_buffer_overflow")
    elif mode != 0:
        invalid.add("unknown_note_buffer_mode")
    if not complete:
        invalid.add("incomplete_capture")
    if window_ms != expected_window_ms:
        invalid.add("unexpected_window_duration")
    if nonmonotonic:
        invalid.add("nonmonotonic_converted_time")
    if any(row.cpu != 0 for row in rows):
        invalid.add("unexpected_cpu")

    counts = {
        "capture_rows": len(rows),
        "note_buffer_overflow": int(mode == 2 or notes_lost),
        "nonmonotonic_rows": nonmonotonic,
        "gpio_irq_entries": 0,
        "worker_wakes": 0,
        "worker_switches": 0,
        "matched": 0,
        "worker_already_running": 0,
        "unmatched_worker_switches": 0,
        "coalesced_gpio_irqs": 0,
        "missing_worker_wake": 0,
        "missing_post_irq_worker_switch": 0,
        "duplicate_worker_wake": 0,
        "worker_pid_mismatch": 0,
        "unmatched_irq_exits": 0,
        "leading_censored_irq_exits": 0,
        "trailing_censored_irq_entries": 0,
    }
    active: Row | None = None
    wakes: list[Row] = []
    saw_entry = False
    waiting_switch_after_woken_irq = False
    irq_pairs: list[tuple[int, int, int]] = []
    latencies: list[int] = []
    for index, row in enumerate(rows):
        if row.kind == "irq_entry" and irq is not None and row.irq == irq:
            counts["gpio_irq_entries"] += 1
            saw_entry = True
            if waiting_switch_after_woken_irq:
                counts["coalesced_gpio_irqs"] += 1
                invalid.add("ambiguous_gpio_irq_coalescing")
                waiting_switch_after_woken_irq = False
            if active is not None:
                counts["coalesced_gpio_irqs"] += 1
                invalid.add("overlapping_gpio_irq_dispatch")
            else:
                active = row
                wakes = []
                if row.task == worker:
                    counts["worker_already_running"] += 1
            continue

        if row.kind == "worker_wake":
            counts["worker_wakes"] += 1
            if active is not None:
                wakes.append(row)
            continue

        if row.kind == "irq_exit" and irq is not None and row.irq == irq:
            if active is None:
                if not saw_entry:
                    counts["leading_censored_irq_exits"] += 1
                else:
                    counts["unmatched_irq_exits"] += 1
                    invalid.add("unmatched_irq_exit")
                continue

            # noteram_dump_one prints this synthesized switch immediately
            # after the IRQ-leave row, reusing the IRQ-leave timestamp.
            next_row = rows[index + 1] if index + 1 < len(rows) else None
            switched = (
                next_row is not None
                and next_row.kind == "worker_switch"
                and next_row.timestamp_ns == row.timestamp_ns
            )
            if active.task == worker:
                active, wakes = None, []
                continue
            if len(wakes) == 0:
                counts["missing_worker_wake"] += 1
                invalid.add("missing_worker_wake")
            elif len(wakes) > 1:
                counts["duplicate_worker_wake"] += 1
                invalid.add("duplicate_worker_wake")
            elif not switched:
                counts["missing_post_irq_worker_switch"] += 1
                invalid.add("missing_post_irq_worker_switch")
                waiting_switch_after_woken_irq = len(wakes) == 1
            elif next_row.target_pid != wakes[0].target_pid:
                counts["worker_pid_mismatch"] += 1
                invalid.add("worker_pid_mismatch")
            else:
                latency = row.timestamp_ns - active.timestamp_ns
                if latency < 0:
                    invalid.add("negative_latency")
                else:
                    latencies.append(latency)
                    counts["matched"] += 1
                    irq_pairs.append((active.timestamp_ns, index + 1, next_row.target_pid))
            active, wakes = None, []
            continue

        if row.kind == "worker_switch":
            counts["worker_switches"] += 1
            previous_is_expected_exit = (
                index > 0 and rows[index - 1].kind == "irq_exit"
                and irq is not None and rows[index - 1].irq == irq
            )
            if not previous_is_expected_exit:
                counts["unmatched_worker_switches"] += 1
                # A switch without an immediately preceding GPIO IRQ-leave
                # was not caused by this measured IRQ for this analysis.
            waiting_switch_after_woken_irq = False

    if active is not None:
        counts["trailing_censored_irq_entries"] += 1
    if irq is not None and not latencies:
        invalid.add("no_unambiguous_pairs")

    if irq is None and any(row.kind in ("irq_entry", "irq_exit") for row in rows):
        invalid.add("unexpected_irq_records")

    # Pair complete worker scopes. A leading end or trailing begin may be
    # censored by capture boundaries; an orphan inside the capture is invalid.
    opens: dict[tuple[str, int], tuple[int, int]] = {}
    seen_scope: set[tuple[str, int]] = set()
    scope_values = {label: [] for label in sorted(I2C_LABELS)}
    matched_begins = {label: set() for label in sorted(I2C_LABELS)}
    scope_counts = {
        label: {
            "begins": 0, "ends": 0, "matched": 0,
            "leading_censored_ends": 0, "trailing_censored_begins": 0,
            "unmatched": 0, "ambiguous": 0, "ignored_other_task": 0,
        }
        for label in sorted(I2C_LABELS)
    }
    worker_pids: set[int] = set()
    for row_index, row in enumerate(rows):
        if row.kind not in ("i2c_begin", "i2c_end") or row.label is None:
            continue
        stats = scope_counts[row.label]
        stats["begins" if row.kind == "i2c_begin" else "ends"] += 1
        if row.task != worker:
            stats["ignored_other_task"] += 1
            continue
        if row.marker_pid != row.pid:
            stats["unmatched"] += 1
            invalid.add("i2c_note_worker_pid_mismatch")
            continue
        worker_pids.add(row.pid)
        key = (row.label, row.pid)
        if row.kind == "i2c_begin":
            if key in opens:
                stats["ambiguous"] += 1
                invalid.add("ambiguous_i2c_scope")
            else:
                opens[key] = (row.timestamp_ns, row_index)
                seen_scope.add(key)
        elif key in opens:
            start_ns, start_index = opens.pop(key)
            duration = row.timestamp_ns - start_ns
            if duration < 0:
                stats["unmatched"] += 1
                invalid.add("negative_i2c_scope")
            else:
                stats["matched"] += 1
                scope_values[row.label].append(duration)
                matched_begins[row.label].add(start_index)
        elif key not in seen_scope:
            stats["leading_censored_ends"] += 1
            seen_scope.add(key)
        else:
            stats["unmatched"] += 1
            invalid.add("unmatched_i2c_scope_end")

    for (label, _pid) in opens:
        scope_counts[label]["trailing_censored_begins"] += 1
    if i2c_enabled:
        for label, values in scope_values.items():
            if not values:
                invalid.add("missing_i2c_evidence:" + label)
    elif any(row.kind in ("i2c_begin", "i2c_end") for row in rows):
        invalid.add("unexpected_i2c_records")
    if len(worker_pids) > 1:
        invalid.add("multiple_imu_worker_pids")

    # Report software-note coverage after the worker switch; this does not
    # identify the physical IRQ edge or electrical bus activity.
    entry_metrics: dict[str, dict[str, Any]] = {}
    entry_specs = (
        ("nxrs.i2c.char_lock_wait", "irq_to_char_driver_entry",
         "gpio_irq_dispatch_entry_to_char_driver_lock_wait_entry"),
        ("nxrs.i2c.esp32s3_service", "irq_to_controller_service_entry",
         "gpio_irq_dispatch_entry_to_controller_service_entry"),
    )
    for label, metric_name, metric in entry_specs:
        entries: list[int] = []
        for irq_entry_ns, switch_index, worker_pid in irq_pairs:
            next_irq = next(
                (i for i in range(switch_index + 1, len(rows)) if rows[i].kind == "irq_entry"),
                len(rows),
            )
            candidate_index = next((
                i for i in range(switch_index + 1, next_irq)
                if i in matched_begins[label]
                and rows[i].task == worker
                and rows[i].marker_pid == worker_pid
            ), None)
            if candidate_index is not None:
                entries.append(rows[candidate_index].timestamp_ns - irq_entry_ns)

        entry_metrics[metric_name] = {
            "metric": metric,
            "endpoint": label + " sched_note begin",
            "candidate_label": label,
            "paired_gpio_worker_switches": len(irq_pairs),
            "covered_entries": len(entries),
            "coverage_percent": (100 * len(entries) // len(irq_pairs)) if irq_pairs else 0,
            "latency_ns": _distribution(entries),
            "physical_sensor_edge_included": False,
            "i2c_wire_time_included": False,
        }

    if irq is not None and i2c_enabled:
        for name, metric in entry_metrics.items():
            if not metric["covered_entries"]:
                invalid.add("missing_entry_coverage:" + name)

    i2c_summary = None
    if schema == 2:
        i2c_summary = {
            "enabled": i2c_enabled,
            "worker": worker,
            "worker_pids": sorted(worker_pids),
            "scopes": {
                label: {"counts": scope_counts[label], "duration_ns": _distribution(scope_values[label])}
                for label in sorted(I2C_LABELS)
            },
            "irq_to_entry": None if irq is None else entry_metrics,
        }

    return {
        "schema": schema,
        "status": "valid" if not invalid else "invalid",
        "invalid_reasons": sorted(invalid),
        "scope": {
            "metric": "gpio_irq_dispatch_entry_to_post_irq_worker_switch" if irq is not None else None,
            "endpoint": None if irq is None else "NuttX synthesized sched_switch after irq_handler_exit",
            "clock": "perf_gettime-converted",
            "unit": "ns",
            "cpu_count": cpus,
            "irq": irq,
            "worker": worker,
            "window_ms": window_ms,
            "physical_sensor_edge_included": False,
            "i2c_wire_time_included": False,
            "first_worker_instruction_proven": False,
        },
        "counts": counts,
        "latency_ns": _distribution(latencies),
        **({"i2c": i2c_summary} if i2c_summary is not None else {}),
    }


def _cli_error(reason: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "status": "invalid",
        "invalid_reasons": [reason],
        "scope": {
            "metric": "gpio_irq_dispatch_entry_to_post_irq_worker_switch",
            "physical_sensor_edge_included": False,
            "i2c_wire_time_included": False,
            "first_worker_instruction_proven": False,
        },
        "counts": {},
        "latency_ns": {"p50_nearest_rank": None, "p95_nearest_rank": None,
                       "p99_nearest_rank": None, "max": None, "samples": 0},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="framed serial log containing NXRS_NOTE rows")
    parser.add_argument("--window-ms", type=int, default=EXPECTED_WINDOW_MS)
    args = parser.parse_args(argv)
    try:
        with args.capture.open("rb") as capture_file:
            raw = capture_file.read(MAX_CAPTURE_BYTES + 1)
        if len(raw) > MAX_CAPTURE_BYTES:
            raise CaptureError("capture exceeds one-MiB read bound")
        text = raw.decode("utf-8")
        result = analyze(text, expected_window_ms=args.window_ms)
    except (OSError, UnicodeDecodeError, CaptureError) as error:
        result = _cli_error(f"capture_error:{type(error).__name__}")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
