"""Offline gain counterfactuals using independent double math and a host model.

This does not run the native binary or reconstruct pre-compensation integer I/Q.
No variants alter detector settings, thresholds, or the captured template bank.
"""

import hashlib
import importlib.util
import math
from pathlib import Path
import struct

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NUM = load("gain_replay_numerical", HERE.parent / "numerical.py")
SNAP = load("gain_replay_snapshots", HERE / "snapshots.py")
TRACE = load("gain_replay_trace", HERE / "trace.py")


def distance(a, b):
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    aa = sum((x - ma) ** 2 for x in a)
    bb = sum((x - mb) ** 2 for x in b)
    if aa <= 0 or bb <= 0:
        return None
    correlation = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(aa * bb)
    return 1 - abs(max(-1, min(1, correlation)))


def variants(matrix, factors):
    if (
        not matrix
        or not matrix[0]
        or any(len(row) != len(matrix[0]) for row in matrix)
        or any(not math.isfinite(x) or x < 0 for row in matrix for x in row)
    ):
        raise ValueError(
            "replay requires finite nonnegative rectangular amplitude matrix"
        )
    if len(matrix) != len(factors) or any(
        not math.isfinite(v) or v <= 0 for v in factors
    ):
        raise ValueError("replay requires per-row finite gain factors")
    rms = [math.sqrt(sum(v * v for v in row) / len(row)) for row in matrix]
    if any(v == 0 for v in rms):
        raise ValueError("zero-energy row cannot be RMS equalized")
    target = sum(rms) / len(rms)
    return {
        "baseline": [row[:] for row in matrix],
        "uniform_1over16": [[NUM.f32(v / 16) for v in row] for row in matrix],
        "continuous_divide_reported_gain": [
            [NUM.f32(v / f) for v in row] for row, f in zip(matrix, factors)
        ],
        "equal_row_rms": [
            [NUM.f32(v * target / r) for v in row] for row, r in zip(matrix, rms)
        ],
    }


def inspect(snapshot, trace):
    TRACE.inspect(trace, snapshot)
    h = snapshot["header"]
    words = snapshot["words"]
    cols = h["cols"]
    values = SNAP.floats(words["matrix"])
    matrix = [values[i : i + cols] for i in range(0, len(values), cols)]
    templates = SNAP.floats(words["templates"])
    bank = [templates[i : i + cols] for i in range(0, len(templates), cols)]
    output = SNAP.floats(words["output"])
    if (
        h["status"] != 0
        or not bank
        or any(not math.isfinite(v) for v in templates + output)
    ):
        raise ValueError(
            "replay requires successful finite projection and actual templates"
        )
    results = []
    for name, data in variants(
        matrix, [TRACE.value(row[9]) for row in trace["rows"]]
    ).items():
        reference = NUM.reference(data)
        model = NUM.power(data)
        distances = [distance(t, reference["projection"]) for t in bank]
        d = {
            "case": name,
            "shape": [len(data), cols],
            "input_sha256": hashlib.sha256(
                b"".join(struct.pack("<f", v) for row in data for v in row)
            ).hexdigest(),
            "double_reference_iterations": reference["iterations"],
            "double_nearest_template_wander": (
                min(v for v in distances if v is not None)
                if any(v is not None for v in distances)
                else None
            ),
            "float32_policy_model_status": model["status"],
            "float32_policy_model_iterations": model["iterations"],
            "float32_policy_model_nonfinite_vector_values": sum(
                not math.isfinite(v) for v in model["vector"]
            ),
            "native_binary_replayed": False,
        }
        if name == "baseline":
            d["captured_native_projection_max_absolute_error"] = max(
                abs(a - b) for a, b in zip(output, reference["projection"])
            )
        results.append(d)
    return {
        "window": h["window"],
        "tag": h["tag"],
        "template_bank_sha256": hashlib.sha256(
            b"".join(struct.pack("<I", v) for v in words["templates"])
        ).hexdigest(),
        "cases": results,
        "native_binary_replayed": False,
        "gain_counterfactual_is_continuous_rescaling_not_reconstructed_IQ": True,
        "physical_occupancy_or_causal_fix_qualified": False,
    }
