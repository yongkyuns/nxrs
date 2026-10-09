"""Parse application-path trace rows from service qualification output."""

import re

from results import _ANSI_ESCAPE, parse as parse_results


_TRACE_PREFIX = re.compile(r"^\s*SQ_TRACE(?![A-Za-z0-9_])")
_TRACE_FIELDS = (
    "id", "events", "transit_cycles", "worker_cycles", "led_cycles",
    "end_cycles", "errors",
)
_CYCLES_PER_US = 240


def verify_workers(text, *, services, language):
    """Prove each diagnostic thread actually ran the requested worker."""
    if language not in ("c", "rust"):
        raise ValueError("unknown worker language")
    selected = {}
    for line in _ANSI_ESCAPE.sub("", text).splitlines():
        if not line.strip().startswith("SQ_WORKER_SELECTED"):
            continue
        match = re.fullmatch(r"SQ_WORKER_SELECTED id=([0-9]+) language=(c|rust)", line.strip())
        if not match:
            raise ValueError("malformed worker selection marker")
        service_id, actual = int(match[1]), match[2]
        if service_id in selected or actual != language:
            raise ValueError("duplicate or incorrect worker selection")
        selected[service_id] = actual
    if set(selected) != set(range(services)):
        raise ValueError("incomplete worker selection evidence")
    return True


def verify_entry(text, *, language):
    markers = [line.strip() for line in _ANSI_ESCAPE.sub("", text).splitlines()
               if line.strip().startswith("SQ_ENTRY_SELECTED")]
    if markers != ["SQ_ENTRY_SELECTED language=" + language]:
        raise ValueError("incorrect or incomplete entry selection evidence")
    return True


def _parse_trace_row(line):
    parts = line.split()
    if len(parts) != len(_TRACE_FIELDS) + 1 or parts[0] != "SQ_TRACE":
        raise ValueError("malformed SQ_TRACE line")

    row = {}
    for field, part in zip(_TRACE_FIELDS, parts[1:]):
        key, separator, value = part.partition("=")
        if not separator or key != field or not value:
            raise ValueError(f"malformed SQ_TRACE field {field}")
        if not value.isascii() or not value.isdecimal():
            raise ValueError(f"SQ_TRACE field {field} must be an unsigned integer")
        row[field] = int(value)
    return row


def parse_trace(text, *, services, events):
    """Parse base qualification output and validate its per-service trace.

    Trace cycle values are raw unsigned sums. Python integers intentionally
    retain totals wider than the firmware's cycle counter so counter wrapping
    across multiple events does not truncate the accumulated measurement.
    """
    result = parse_results(text, services=services, events=events, source="messages")
    if services < 2:
        raise ValueError("trace requires at least two services")

    rows_by_id = {}
    for raw_line in _ANSI_ESCAPE.sub("", text).splitlines():
        line = raw_line.strip()
        if not _TRACE_PREFIX.match(line):
            continue
        row = _parse_trace_row(line)
        service_id = row["id"]
        if service_id in rows_by_id:
            raise ValueError(f"duplicate SQ_TRACE id={service_id}")
        if row["events"] != events:
            raise ValueError(f"SQ_TRACE id={service_id} event count does not match request")
        rows_by_id[service_id] = row

    expected_ids = set(range(services))
    if set(rows_by_id) != expected_ids:
        raise ValueError("SQ_TRACE ids do not match requested services")

    led_id = services - 2
    rows = [rows_by_id[service_id] for service_id in range(services)]
    for row in rows:
        service_id = row["id"]
        if row["errors"] != 0:
            raise ValueError(f"SQ_TRACE id={service_id} reported errors")
        if row["led_cycles"] and service_id != led_id:
            raise ValueError("LED cycles may only be reported by the LED service")
        if row["led_cycles"] > row["worker_cycles"]:
            raise ValueError(f"SQ_TRACE id={service_id} LED cycles exceed worker cycles")
        if row["end_cycles"] < row["worker_cycles"]:
            raise ValueError(f"SQ_TRACE id={service_id} end cycles are below worker cycles")

    first = rows[0]
    if first["end_cycles"] != first["transit_cycles"] + first["worker_cycles"]:
        raise ValueError("SQ_TRACE stage 0 end cycles do not close")

    led = rows[led_id]
    path_cycles = sum(
        row["transit_cycles"] + row["worker_cycles"]
        for row in rows[:led_id + 1]
    )
    if path_cycles != led["end_cycles"]:
        raise ValueError("SQ_TRACE path cycles do not close at the LED stage")

    denominator = events * _CYCLES_PER_US
    result.update(
        trace_rows=rows,
        path_us={
            "transit": sum(row["transit_cycles"] for row in rows[:led_id + 1]) / denominator,
            "worker_excluding_led": sum(
                row["worker_cycles"] - row["led_cycles"]
                for row in rows[:led_id + 1]
            ) / denominator,
            "led": sum(row["led_cycles"] for row in rows[:led_id + 1]) / denominator,
            "end": led["end_cycles"] / denominator,
        },
        instrumentation=True,
    )
    return result
