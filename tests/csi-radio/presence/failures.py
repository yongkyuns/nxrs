"""Strict private first-anomaly wire reader; inspection exports aggregates only."""

import math
import re
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "failure_snapshot_words", Path(__file__).with_name("snapshots.py")
)
_words = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_words)
floats, hash_words = _words.floats, _words.hash_words

FIELDS = "v source reason window us cols rows0 rows1 status".split()
SOURCES = {"selected": 0, "other": 1}
REASONS = {"native_status": 1, "nonfinite_input": 2, "nonfinite_output": 3}


def read(first, line):
    pairs = [v.split("=", 1) for v in first.decode("ascii").split()[1:]]
    if (
        not first.startswith(b"PRESENCE_NUMERIC_FAILURE ")
        or len(pairs) != len(FIELDS)
        or any(len(p) != 2 for p in pairs)
    ):
        raise ValueError("numeric failure schema")
    h = dict(pairs)
    if (
        set(h) != set(FIELDS)
        or h["source"] not in SOURCES
        or h["reason"] not in REASONS
    ):
        raise ValueError("numeric failure source/reason")
    for key in set(FIELDS) - {"source", "reason"}:
        if not re.fullmatch(r"-?\d{1,20}", h[key]):
            raise ValueError("numeric failure integer")
        h[key] = int(h[key])
        low = -0x80000000 if key == "status" else 0
        high = (
            0x7FFFFFFF
            if key == "status"
            else 0xFFFFFFFFFFFFFFFF if key == "us" else 0xFFFFFFFF
        )
        if not low <= h[key] <= high:
            raise ValueError("numeric failure integer bound")
    n = (h["rows0"] + h["rows1"]) * h["cols"]
    if h["v"] != 1 or not h["window"] or not 1 <= h["cols"] <= 64 or not 1 <= n <= 1024:
        raise ValueError("numeric failure shape/version")
    checksum = hash_words(
        [
            1,
            SOURCES[h["source"]],
            REASONS[h["reason"]],
            h["window"],
            h["us"] & 0xFFFFFFFF,
            h["us"] >> 32,
            h["cols"],
            h["rows0"],
            h["rows1"],
            h["status"],
        ]
    )
    parts = {}
    for name, count in [
        ("matrix", n),
        ("output", h["cols"] if h["status"] == 0 else 0),
    ]:
        values = []
        for start in range(0, count, 16):
            match = re.fullmatch(
                rb"PRESENCE_FAILURE_WORDS part="
                + name.encode()
                + rb" index="
                + str(start).encode()
                + rb" bits=([0-9a-f,]+)",
                line(),
            )
            if not match:
                raise ValueError("numeric failure fragment order")
            tokens = match[1].split(b",")
            if len(tokens) != min(16, count - start) or any(
                not re.fullmatch(b"[0-9a-f]{8}", v) for v in tokens
            ):
                raise ValueError("numeric failure fragment encoding")
            words = [int(v, 16) for v in tokens]
            checksum = hash_words(words, checksum)
            values.extend(words)
        parts[name] = values
    if (
        line()
        != f"PRESENCE_NUMERIC_FAILURE_END window={h['window']} fnv={checksum:08x}".encode()
    ):
        raise ValueError("numeric failure checksum/completion")
    finite_input = all(math.isfinite(v) for v in floats(parts["matrix"]))
    finite_output = all(math.isfinite(v) for v in floats(parts["output"]))
    expected = (
        "native_status"
        if h["status"] != 0
        else (
            "nonfinite_input"
            if not finite_input
            else "nonfinite_output" if not finite_output else None
        )
    )
    if h["reason"] != expected:
        raise ValueError("numeric failure reason does not match actual bits/status")
    return {"kind": "numeric_failure", "header": h, "words": parts}


def inspect(record):
    h = record["header"]
    matrix = floats(record["words"]["matrix"])
    output = floats(record["words"]["output"])
    finite = [v for v in matrix if math.isfinite(v)]
    return {
        "source": h["source"],
        "reason": h["reason"],
        "window": h["window"],
        "shape": [h["rows0"] + h["rows1"], h["cols"]],
        "native_status": h["status"],
        "nonfinite_input_values": len(matrix) - len(finite),
        "nonfinite_output_values": sum(not math.isfinite(v) for v in output),
        "finite_input_min": min(finite) if finite else None,
        "finite_input_max": max(finite) if finite else None,
        "all_input_values_zero": bool(matrix) and all(v == 0 for v in matrix),
        "other_scope_is_all_nonselected_sdk_calls": True,
        "rf_row_identity_available": False,
    }
