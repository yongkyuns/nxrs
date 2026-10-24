"""Private bounded RF/preprocessing wire reader and aggregate-only inspector.

Driver RX timestamps and local observation times are distinct. Pair only by
explicit numerical window and exact matrix bits, never arrival order/time.
"""

import math
import re
import struct

FIELDS = "v tag token window cols rows rows0 status log enqueued_us computed_us decision_status train before_basis action templates background sum basis wander jitter".split()
FLOATS = {"before_basis", "sum", "basis", "wander", "jitter"}
META = 22


def signed(word):
    return word - (1 << 32) if word >= 1 << 31 else word


def value(word):
    return struct.unpack("<f", struct.pack("<I", word))[0]


def hash_word(seed, word):
    for byte in struct.pack("<I", word & 0xFFFFFFFF):
        seed = ((seed ^ byte) * 16777619) & 0xFFFFFFFF
    return seed


def header(first):
    tokens = first.decode("ascii").split()
    if not tokens or tokens.pop(0) != "PRESENCE_TRACE":
        raise ValueError("not a trace")
    pairs = [x.split("=", 1) for x in tokens]
    if len(pairs) != len(FIELDS) or any(len(x) != 2 for x in pairs):
        raise ValueError("trace schema")
    result = dict(pairs)
    if set(result) != set(FIELDS):
        raise ValueError("trace duplicate or unknown field")
    for key in FIELDS:
        text = result[key]
        if key in FLOATS:
            if not re.fullmatch("[0-9a-f]{8}", text):
                raise ValueError("trace float bits")
            result[key] = int(text, 16)
        else:
            if not re.fullmatch(r"-?\d{1,20}", text):
                raise ValueError("trace integer")
            result[key] = int(text)
            low = -0x80000000 if key in ("status", "decision_status") else 0
            high = (
                (1 << 64) - 1
                if key in ("enqueued_us", "computed_us")
                else (0x7FFFFFFF if low < 0 else 0xFFFFFFFF)
            )
            if not low <= result[key] <= high:
                raise ValueError("trace integer bound")
    if (
        result["v"] != 1
        or not 0 <= result["tag"] <= 16
        or not result["token"]
        or not result["window"]
        or not 1 <= result["cols"] <= 16
        or not 1 <= result["rows"] <= 32
        or not 1 <= result["rows0"] <= result["rows"]
        or result["log"] not in (0, 1)
        or result["computed_us"] < result["enqueued_us"]
        or (result["status"] == 0 and result["tag"] == 0)
    ):
        raise ValueError("trace bounds or ownership")
    return result


def read(first, line):
    h = header(first)
    checksum = 2166136261
    for key in FIELDS:
        word = h[key]
        checksum = hash_word(checksum, word)
        if key in ("enqueued_us", "computed_us"):
            checksum = hash_word(checksum, word >> 32)
    rows = []
    for row in range(h["rows"]):
        words = []
        size = META + 3 * h["cols"]
        for offset in range(0, size, 16):
            match = re.fullmatch(
                rb"PRESENCE_TRACE_WORDS row="
                + str(row).encode()
                + rb" index="
                + str(offset).encode()
                + rb" bits=([0-9a-f,]+)",
                line(),
            )
            if not match:
                raise ValueError("trace fragment missing, reordered or interleaved")
            tokens = match[1].split(b",")
            if len(tokens) != min(16, size - offset) or any(
                not re.fullmatch(b"[0-9a-f]{8}", x) for x in tokens
            ):
                raise ValueError("trace fragment size/encoding")
            chunk = [int(x, 16) for x in tokens]
            for word in chunk:
                checksum = hash_word(checksum, word)
            words.extend(chunk)
        meta = words[:META]
        iq = list(map(signed, words[META : META + 2 * h["cols"]]))
        observed = meta[18] | meta[19] << 32
        processed = meta[20] | meta[21] << 32
        if (
            not -128 <= signed(meta[2]) <= 127
            or meta[3] > 255
            or not -128 <= signed(meta[4]) <= 127
            or meta[5] > 1
            or not 1 <= meta[6] <= 512
            or not meta[7]
            or signed(meta[8]) != 0
            or not math.isfinite(value(meta[9]))
            or value(meta[9]) <= 0
            or meta[12] > meta[14]
            or meta[13] > meta[14]
            or meta[14] != meta[6]
            or meta[15] != h["log"]
            or meta[16] != 1
            or not 1 <= meta[17] <= 255
            or not observed <= processed <= h["enqueued_us"]
            or any(
                not (-2048 <= x <= 2047 if meta[5] else -128 <= x <= 127) for x in iq
            )
        ):
            raise ValueError("trace row provenance or gain unavailable")
        if rows:
            previous = rows[-1][:META]
            delta = (meta[1] - previous[1]) & 0xFFFFFFFF
            if meta[0] <= previous[0] or not 0 < delta < 0x80000000:
                raise ValueError("trace duplicate/reversed RF identity")
        rows.append(words)
    if line() != f'PRESENCE_TRACE_END token={h["token"]} fnv={checksum:08x}'.encode():
        raise ValueError("trace completion/checksum")
    return {"kind": "rf_preprocessing_trace", "header": h, "rows": rows}


def inspect(trace, snapshot=None):
    """Return sanitized aggregates; raw words/IQ never enter the summary."""
    h, rows = trace["header"], trace["rows"]
    count = h["cols"]
    paired = False
    if snapshot is not None:
        s = snapshot["header"]
        matrix = [x for row in rows for x in row[META + 2 * count :]]
        if (
            s["window"] != h["window"]
            or s["tag"] != h["tag"]
            or s["cols"] != count
            or s["rows0"] != h["rows0"]
            or s["rows1"] != h["rows"] - h["rows0"]
            or s["status"] != h["status"]
            or snapshot["words"]["matrix"] != matrix
        ):
            raise ValueError("trace and numerical snapshot do not match exactly")
        paired = True
    errors, nonfinite = [], 0
    for row in rows:
        iq = list(map(signed, row[META : META + 2 * count]))
        for i, native_word in enumerate(row[META + 2 * count :]):
            expected = math.hypot(iq[2 * i], iq[2 * i + 1])
            if h["log"]:
                expected = math.log10(expected) if expected else -math.inf
            native = value(native_word)
            if math.isfinite(expected) and math.isfinite(native):
                errors.append(abs(expected - native))
            else:
                nonfinite += 1
    factors = [value(row[9]) for row in rows]
    observed = [row[18] | row[19] << 32 for row in rows]
    processed = [row[20] | row[21] << 32 for row in rows]
    return {
        "tag": h["tag"],
        "window": h["window"],
        "rows": h["rows"],
        "cols": count,
        "native_status": h["status"],
        "paired_snapshot_verified": paired,
        "gain_factor_min": min(factors),
        "gain_factor_max": max(factors),
        "rssi_min": min(signed(row[2]) for row in rows),
        "rssi_max": max(signed(row[2]) for row in rows),
        "compensation_changed_payload_rows": sum(row[10] != row[11] for row in rows),
        "preprocessing_max_absolute_error": max(errors) if errors else None,
        "preprocessing_nonfinite_pairs": nonfinite,
        "rx_to_preprocessing_max_us": max(b - a for a, b in zip(observed, processed)),
        "oldest_rx_age_at_compute_us": h["computed_us"] - min(observed),
        "queue_delay_us": h["computed_us"] - h["enqueued_us"],
        "training_diagnostic_available": h["decision_status"] == 0,
        "training_action": h["action"] if h["decision_status"] == 0 else None,
        "training_status": h["train"] if h["decision_status"] == 0 else None,
        "local_observation_clock_is_not_driver_rx_clock": True,
        "source_rf_progress_is_not_presence_accuracy": True,
    }
