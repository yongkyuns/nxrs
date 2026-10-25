"""Private exact-window evidence; public inspection emits aggregates only."""

import math
import re
import struct
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pair_trace_checks", Path(__file__).with_name("trace.py")
)
TRACE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TRACE)

PHASES = ["uncalibrated", "background", "holdout", "ready", "rejected"]
REASONS = {14: "training_complete", 15: "background_ready", 16: "first_rejected"}
FIELDS = "v tag window sequence reason phase features filled index average lower upper enter exit stack_free_bytes".split()
# Stable code ordering agrees with the Rust enum; legacy v1 has no attribution.
REJECTIONS = "none trace_fault native_error source_contention control_contention poisoned clock_rollback progress_expired invalid_feature training_incomplete gain_unavailable templates_changed invalid_rf_time rf_expired source_reversed saturated_envelope holdout_exceeded".split()
FLOATS = "average lower upper enter exit".split()


def number(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def f32(value):
    return number(struct.unpack("<I", struct.pack("<f", value))[0])


def hash_word(seed, word):
    for byte in struct.pack("<I", word):
        seed = ((seed ^ byte) * 16777619) & 0xFFFFFFFF
    return seed


def read(first, line):
    parts = [p.split("=", 1) for p in first.decode("ascii").split()[1:]]
    version2 = bool(parts and ["v", "2"] in parts)
    fields = FIELDS + (["rejection"] if version2 else [])
    if (
        not first.startswith(b"PRESENCE_PAIR ")
        or len(parts) != len(fields)
        or any(len(p) != 2 for p in parts)
    ):
        raise ValueError("calibration pair schema")
    h = dict(parts)
    if set(h) != set(fields) or h["phase"] not in PHASES:
        raise ValueError("calibration pair fields/phase")
    for key in set(fields) - {"phase", "reason", "rejection"}:
        pattern = "[0-9a-f]{8}" if key in FLOATS else "[0-9]{1,10}"
        if not re.fullmatch(pattern, h[key]):
            raise ValueError("calibration pair integer/bits")
        h[key] = int(h[key], 16 if key in FLOATS else 10)
        if h[key] > 0xFFFFFFFF:
            raise ValueError("calibration pair integer bound")
    if (
        h["v"] not in (1, 2)
        or REASONS.get(h["tag"]) != h["reason"]
        or not h["window"]
        or h["filled"] > 25
        or h["index"] >= 25
    ):
        raise ValueError("calibration pair identity/bounds")
    if version2 and (
        h["rejection"] not in REJECTIONS
        or (h["rejection"] != "none") != (h["phase"] == "rejected")
    ):
        raise ValueError("calibration rejection attribution")
    if h["features"] and h["filled"] != 25:
        raise ValueError("calibration pair incomplete history counted")
    if (
        (h["tag"] == 14 and h["phase"] not in ("uncalibrated", "background"))
        or (
            h["tag"] == 15
            and (h["phase"] != "background" or h["filled"] != 25 or h["features"] < 128)
        )
        or (h["tag"] == 16 and h["phase"] != "rejected")
    ):
        raise ValueError("calibration pair event/phase mismatch")
    for key in FLOATS:
        x = number(h[key])
        if not math.isfinite(x) or not 0 <= x <= (3 if key == "enter" else 1):
            raise ValueError("calibration pair invalid numerical evidence")
    words = [h[k] for k in ("v", "tag", "window", "sequence")]
    words += [PHASES.index(h["phase"])]
    words += [h[k] for k in ("features", "filled", "index", *FLOATS)]
    words += [h["stack_free_bytes"]]
    if version2:
        words += [REJECTIONS.index(h["rejection"])]
    checksum = 2166136261
    for word in words:
        checksum = hash_word(checksum, word)
    sequences, values = [], []
    for start in range(0, 25, 8):
        match = re.fullmatch(
            rb"PRESENCE_PAIR_HISTORY index="
            + str(start).encode()
            + rb" entries=([0-9a-f:,]+)",
            line(),
        )
        if not match:
            raise ValueError("calibration history fragment order")
        entries = match[1].split(b",")
        if len(entries) != min(8, 25 - start):
            raise ValueError("calibration history fragment size")
        for entry in entries:
            if not re.fullmatch(b"[0-9a-f]{8}:[0-9a-f]{8}", entry):
                raise ValueError("calibration history encoding")
            sequence, bits = [int(x, 16) for x in entry.split(b":")]
            if not math.isfinite(number(bits)) or not 0 <= number(bits) <= 1:
                raise ValueError("invalid calibration history value")
            sequences.append(sequence)
            values.append(bits)
            checksum = hash_word(hash_word(checksum, sequence), bits)
    if line() != f'PRESENCE_PAIR_END window={h["window"]} fnv={checksum:08x}'.encode():
        raise ValueError("calibration pair checksum/completion")
    if h["filled"] == 25:
        ordered = sequences[h["index"] :] + sequences[: h["index"]]
        if any(
            not 0 < (b - a) & 0xFFFFFFFF < 0x80000000
            for a, b in zip(ordered, ordered[1:])
        ):
            raise ValueError("calibration history duplicate/reversed source")
        total = 0.0
        for value in sorted(map(number, values))[:13]:
            total = f32(total + value)
        average = f32(total / 13)
        if struct.unpack("<I", struct.pack("<f", average))[0] != h["average"]:
            raise ValueError("calibration rolling statistic does not replay exactly")
    elif h["average"] != 0:
        raise ValueError("incomplete calibration history has statistic")
    return {
        "kind": "calibration_pair",
        "header": h,
        "sequences": sequences,
        "value_bits": values,
    }


def inspect(pair, trace, snapshot):
    h, t, s = pair["header"], trace["header"], snapshot["header"]
    traced = TRACE.inspect(trace, snapshot)
    if (
        any(h[key] != t[key] or h[key] != s[key] for key in ("tag", "window"))
        or h["sequence"] != trace["rows"][-1][0]
    ):
        raise ValueError("calibration evidence is not the same source/window")
    # The existing trace inspector separately checks every matrix bit, shape,
    # gain association and native status against the numerical snapshot.
    current = bool(
        h["filled"] and pair["sequences"][(h["index"] + 24) % 25] == h["sequence"]
    )
    if current and pair["value_bits"][(h["index"] + 24) % 25] != t["wander"]:
        raise ValueError("decision feature is not the history contributor")
    return {
        "tag": h["tag"],
        "window": h["window"],
        "reason": h["reason"],
        "phase": h["phase"],
        "rejection": h.get("rejection", "not_recorded_v1"),
        "filled": h["filled"],
        "features": h["features"],
        "average": number(h["average"]),
        "enter": number(h["enter"]),
        "exit": number(h["exit"]),
        "current_window_contributes_to_statistic": current,
        "history_statistic_exactly_replayed": h["filled"] == 25,
        "other_history_matrices_retained": False,
        "stack_free_bytes": h["stack_free_bytes"],
        "paired_snapshot_verified": traced["paired_snapshot_verified"],
    }
