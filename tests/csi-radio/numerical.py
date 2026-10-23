"""Host-only numerical witnesses; synthetic input, never a presence classifier.

The float32 model follows the pinned archive's decoded Gram/power iteration
rounding and absolute stopping policy. The separate double reference uses a
relative eigenvalue tolerance. Actual archive behavior must be checked in QEMU.
"""

import json
import math
import struct


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


DELTA = struct.unpack("<f", bytes.fromhex("17b7d138"))[0]


def gram(samples, rounded=True):
    R = len(samples)
    C = len(samples[0])
    cast = f32 if rounded else float
    out = [[0.0] * R for _ in range(R)]
    for i in range(R):
        for j in range(i + 1):
            total = 0.0
            for k in range(C):
                total = cast(total + cast(samples[i][k] * samples[j][k]))
            out[i][j] = out[j][i] = cast(total / (R * C))
    return out


def power(samples, limit=30, rounded=True):
    cast = f32 if rounded else float
    a = gram(samples, rounded)
    n = len(a)
    v = [1.0] * n
    current = 0.0
    last = 1.0
    trace = []
    while abs(last - current) > DELTA and len(trace) < limit:
        last = current
        temp = [sum(cast(a[i][j] * v[j]) for j in range(n)) for i in range(n)]
        current = max(0.0, max(temp))
        v = [cast(x / current) if current else float("nan") for x in temp]
        trace.append(current)
    return {
        "status": -1 if len(trace) == limit else 0,
        "iterations": len(trace),
        "tail": trace[-8:],
        "vector": v,
        "delta": abs(last - current),
    }


def sample(seed):
    x = seed
    out = []
    for r in range(10):
        row = []
        for c in range(13):
            x = (1664525 * x + 1013904223) & 0xFFFFFFFF
            row.append(float(10 + (x >> 24) % 111))
        out.append(row)
    return out


def reference(samples):
    matrix = gram(samples, rounded=False)
    vector = [1.0] * len(matrix)
    previous = None
    for iteration in range(1, 1025):
        product = [
            sum(row[j] * vector[j] for j in range(len(vector))) for row in matrix
        ]
        eigenvalue = max(product)
        if not math.isfinite(eigenvalue) or eigenvalue <= 0:
            raise ValueError("reference matrix has no positive finite normalization")
        vector = [value / eigenvalue for value in product]
        if previous is not None and abs(eigenvalue - previous) <= 1e-12 * max(
            abs(eigenvalue), 1.0
        ):
            projection = [
                sum(samples[r][c] * vector[r] for r in range(len(samples)))
                / len(samples)
                for c in range(len(samples[0]))
            ]
            return dict(
                eigenvalue=eigenvalue,
                vector=vector,
                projection=projection,
                iterations=iteration,
            )
        previous = eigenvalue
    raise ValueError("double reference did not converge")


def report():
    data = sample(785)
    scaled = [[f32(value / 16) for value in row] for row in data]
    return {
        "synthetic_seed": 785,
        "shape": {"rows": 10, "cols": 13},
        "delta_float32": DELTA,
        "unscaled": power(data),
        "unscaled_budget300": power(data, 300),
        "scaled_by_1_over_16": power(scaled),
        "double_reference_unscaled": reference(data),
        "double_reference_scaled": reference(scaled),
        "physical_failing_inputs_replayed": False,
    }


if __name__ == "__main__":
    print(json.dumps(report(), indent=2))
