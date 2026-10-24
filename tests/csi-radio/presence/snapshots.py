"""Bounded private frozen-input wire format; no device operations or raw export.

Words retain exact IEEE bits, including nonfinite inputs. Window/us identify a
numerical call, not an RF acquisition. Gain is the last completed observed gain
call at numerical admission, not metadata attached to each matrix row.
"""

import math
import re
import struct

FIELDS = "v kind tag window us cols rows0 rows1 status templates gain_seq agc fft gain_status factor".split()
LIMITS = {
    "tag": (0, 16),
    "window": (1, 0xFFFFFFFF),
    "us": (0, 0xFFFFFFFFFFFFFFFF),
    "cols": (1, 64),
    "rows0": (0, 1024),
    "rows1": (0, 1024),
    "status": (-0x80000000, 0x7FFFFFFF),
    "templates": (0, 10),
    "gain_seq": (0, 0xFFFFFFFF),
    "agc": (0, 255),
    "fft": (-128, 127),
    "gain_status": (-0x80000000, 0x7FFFFFFF),
}


def header(line):
    fields = line.decode("ascii").split()
    if not fields or fields.pop(0) != "PRESENCE_SNAPSHOT":
        raise ValueError("not a snapshot")
    pairs = [x.split("=", 1) for x in fields]
    if len(pairs) != len(FIELDS) or any(len(x) != 2 for x in pairs):
        raise ValueError("snapshot schema")
    result = dict(pairs)
    if (
        set(result) != set(FIELDS)
        or result["v"] != "1"
        or result["kind"] not in ("failure", "sample")
    ):
        raise ValueError("snapshot schema/version")
    for key, (low, high) in LIMITS.items():
        if not re.fullmatch(r"-?\d{1,20}", result[key]):
            raise ValueError("snapshot integer")
        result[key] = int(result[key])
        if not low <= result[key] <= high:
            raise ValueError("snapshot integer bound")
    if not re.fullmatch("[0-9a-f]{8}", result["factor"]):
        raise ValueError("snapshot factor")
    result["factor"] = int(result["factor"], 16)
    result["v"] = 1
    rows = result["rows0"] + result["rows1"]
    if not 1 <= rows * result["cols"] <= 1024:
        raise ValueError("snapshot matrix bound")
    if (
        (result["kind"] == "failure") != (result["status"] != 0)
        or (result["kind"] == "sample" and result["tag"] == 0)
        or (result["status"] != 0 and result["templates"] != 0)
    ):
        raise ValueError("snapshot status/ownership mismatch")
    return result


def hash_words(words, seed=2166136261):
    for word in words:
        for byte in struct.pack("<I", word & 0xFFFFFFFF):
            seed = ((seed ^ byte) * 16777619) & 0xFFFFFFFF
    return seed


def read(first, line):
    h = header(first)
    count = h["cols"]
    lengths = {
        "matrix": (h["rows0"] + h["rows1"]) * count,
        "output": count if h["status"] == 0 else 0,
        "templates": h["templates"] * count,
        "wander": h["templates"],
    }
    words = [
        h["tag"],
        h["window"],
        h["us"] & 0xFFFFFFFF,
        h["us"] >> 32,
        count,
        h["rows0"],
        h["rows1"],
        h["status"],
        h["templates"],
        h["gain_seq"],
        h["agc"],
        h["fft"],
        h["gain_status"],
        h["factor"],
        1,
    ]
    checksum = hash_words(words)
    parts = {}
    for name, length in lengths.items():
        values = []
        for index in range(0, length, 16):
            expected = min(16, length - index)
            match = re.fullmatch(
                rb"PRESENCE_WORDS part="
                + name.encode()
                + rb" index="
                + str(index).encode()
                + rb" bits=([0-9a-f,]+)",
                line(),
            )
            if not match:
                raise ValueError("snapshot fragment missing, reordered or interleaved")
            tokens = match[1].split(b",")
            if len(tokens) != expected or any(
                not re.fullmatch(b"[0-9a-f]{8}", x) for x in tokens
            ):
                raise ValueError("snapshot fragment size/encoding")
            chunk = [int(x, 16) for x in tokens]
            checksum = hash_words(chunk, checksum)
            values.extend(chunk)
        parts[name] = values
    expected = f'PRESENCE_SNAPSHOT_END window={h["window"]} fnv={checksum:08x}'.encode()
    if line() != expected:
        raise ValueError("snapshot completion/checksum mismatch")
    return {"kind": "snapshot", "header": h, "words": parts}


def floats(words):
    return [struct.unpack("<f", struct.pack("<I", x))[0] for x in words]


def inspect(snapshot):
    """Independent double correlations; returns aggregates, never the RF arrays."""
    h = snapshot["header"]
    p = snapshot["words"]
    output = floats(p["output"])
    template = floats(p["templates"])
    results = []
    for i in range(h["templates"]):
        a = template[i * h["cols"] : (i + 1) * h["cols"]]
        b = output
        ma = sum(a) / len(a)
        mb = sum(b) / len(b)
        aa = sum((x - ma) ** 2 for x in a)
        bb = sum((x - mb) ** 2 for x in b)
        correlation = (
            sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(aa * bb)
            if aa > 0 and bb > 0
            else None
        )
        independent = (
            1 - abs(max(-1.0, min(1.0, correlation)))
            if correlation is not None and math.isfinite(correlation)
            else None
        )
        native = floats(p["wander"])[i]
        results.append(
            {
                "native": native if math.isfinite(native) else None,
                "double": independent,
                "absolute_error": (
                    abs(native - independent)
                    if independent is not None and math.isfinite(native)
                    else None
                ),
                "template_centered_energy": aa if math.isfinite(aa) else None,
                "current_centered_energy": bb if math.isfinite(bb) else None,
            }
        )
    values = floats(p["matrix"])
    finite = [x for x in values if math.isfinite(x)]
    return {
        "window": h["window"],
        "tag": h["tag"],
        "status": h["status"],
        "shape": [h["rows0"] + h["rows1"], h["cols"]],
        "finite_matrix": len(finite) == len(values),
        "matrix_min": min(finite) if finite else None,
        "matrix_max": max(finite) if finite else None,
        "template_comparisons": results,
        "numerical_call_clock_is_not_rf_freshness": True,
        "gain_is_latest_observation_not_per_row_metadata": True,
        "gain_factor_available": bool(
            h["gain_seq"]
            and h["gain_status"] == 0
            and math.isfinite(floats([h["factor"]])[0])
            and floats([h["factor"]])[0] > 0
        ),
    }
