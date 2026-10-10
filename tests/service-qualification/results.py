"""Strict parser for service qualification runtime output."""

import re


_ANSI_ESCAPE = re.compile(
    r"\x1B(?:"
    r"\[[0-?]*[ -/]*[@-~]|"  # CSI
    r"\][^\x07]*(?:\x07|\x1B\\)|"  # OSC
    r"[@-_]"  # Other two-byte escape sequences
    r")"
)
_PROTOCOL_PREFIX = re.compile(r"^\s*(SQ_MEMORY|SQ_RESULT|SQ_DONE)(?![A-Za-z0-9_])")
_ROWS = {
    "SQ_MEMORY": (
        "before", "full", "delta", "metadata", "payload", "stacks",
    ),
    "SQ_RESULT": (
        "services", "queues", "events", "received", "errors", "mean_cycles",
        "max_cycles", "misses_1ms", "source",
    ),
    "SQ_DONE": ("status", "heap_after"),
}
_CYCLES_PER_US = 240
_DEADLINE_US = 1000


def _parse_row(kind, line):
    fields = _ROWS[kind]
    parts = line.split()
    if not parts or parts[0] != kind or len(parts) != len(fields) + 1:
        raise ValueError(f"malformed {kind} line")

    values = {}
    for field, part in zip(fields, parts[1:]):
        key, separator, value = part.partition("=")
        if not separator or key != field or not value:
            raise ValueError(f"malformed {kind} field {field}")
        if field == "source":
            values[field] = value
        else:
            if not value.isascii() or not value.isdecimal():
                raise ValueError(f"{kind} field {field} must be an unsigned integer")
            values[field] = int(value)
    return values


def _positive_integer(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def validate_memory(memory, services):
    """Check the shared full-capacity record, including failed runtime calls."""
    _positive_integer("services", services)
    if memory["stacks"] != services * 4096:
        raise ValueError("stack footprint does not match service count")
    if memory["payload"] != services * (1 + 8 + 8) * 16:
        raise ValueError("payload footprint does not match service count")
    if memory["full"] < memory["before"]:
        raise ValueError("full memory measurement is below baseline")
    if memory["delta"] != memory["full"] - memory["before"]:
        raise ValueError("memory delta does not match full minus baseline")


def parse(text, *, services, events, source):
    """Parse and validate one complete service qualification transcript.

    Unrelated shell output is ignored. Every protocol marker that appears must
    be well-formed, and exactly one of each required marker must be present.
    """
    if not isinstance(text, str):
        raise ValueError("transcript must be text")
    _positive_integer("services", services)
    _positive_integer("events", events)
    if source not in ("messages", "gpio"):
        raise ValueError("source must be 'messages' or 'gpio'")

    rows = {}
    for raw_line in _ANSI_ESCAPE.sub("", text).splitlines():
        line = raw_line.strip()
        match = _PROTOCOL_PREFIX.match(line)
        if not match:
            continue
        kind = match.group(1)
        if kind in rows:
            raise ValueError(f"duplicate {kind} line")
        rows[kind] = _parse_row(kind, line)

    missing = [kind for kind in _ROWS if kind not in rows]
    if missing:
        raise ValueError(f"missing protocol line(s): {', '.join(missing)}")

    memory = rows["SQ_MEMORY"]
    result = rows["SQ_RESULT"]
    done = rows["SQ_DONE"]

    if result["services"] != services:
        raise ValueError("service count does not match request")
    if result["events"] != events:
        raise ValueError("event count does not match request")
    if result["source"] != source:
        raise ValueError("source does not match request")
    if result["errors"] != 0:
        raise ValueError("runtime reported errors")
    if done["status"] != 0:
        raise ValueError("runtime reported failure status")
    if result["received"] != events:
        raise ValueError("received count does not match event count")
    if result["queues"] != services * 3:
        raise ValueError("queue count does not match service footprint")
    validate_memory(memory, services)
    if result["mean_cycles"] > result["max_cycles"]:
        raise ValueError("mean cycles exceed maximum cycles")
    if result["max_cycles"] <= 0:
        raise ValueError("maximum cycles must be positive")
    if result["misses_1ms"] > events:
        raise ValueError("deadline misses exceed event count")

    return {
        "memory": memory,
        "result": result,
        "done": done,
        "mean_us": result["mean_cycles"] / _CYCLES_PER_US,
        "max_us": result["max_cycles"] / _CYCLES_PER_US,
        "deadline_us": _DEADLINE_US,
    }
