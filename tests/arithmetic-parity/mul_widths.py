#!/usr/bin/env python3
"""Generate a checked signed-64 multiply corpus stratified by operand width."""
import argparse
import hashlib
import json
from pathlib import Path
import random

import generate
import integers

HERE = Path(__file__).resolve().parent
MASK64 = (1 << 64) - 1
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
I32_MIN = -(1 << 31)
I32_MAX = (1 << 31) - 1
SAMPLES = 64
GROUPS = (
    "both_signed32fit",
    "only_a_signed32fit",
    "only_b_signed32fit",
    "both_wide_product_fits",
    "both_wide_product_overflows",
    "near_32_boundary",
    "near_64_boundary",
    "mixed_randomized_widths",
)


def _raw(value):
    return value & MASK64


def _near32(rng):
    edge = rng.choice((I32_MIN, I32_MAX + 1))
    return edge + rng.randint(-4, 4)


def _near64(rng):
    edge = rng.choice((I64_MIN, I64_MAX))
    return edge + rng.randint(-3, 3)


def _pair(group, index, rng):
    if group == "both_signed32fit":
        values = (0, 1, -1, 2, -2, I32_MIN, I32_MAX)
        explicit = ((0, 0), (0, 1), (1, 0), (1, 1), (-1, -1),
                    (I32_MIN, -1), (I32_MAX, 1))
        if index < len(explicit):
            return explicit[index]
        return rng.choice(values), rng.choice(values)
    if group == "only_a_signed32fit":
        return rng.choice((0, 1, -1, I32_MIN, I32_MAX)), rng.choice((I32_MIN - 1, I32_MAX + 1, 1 << 40, -(1 << 40)))
    if group == "only_b_signed32fit":
        return rng.choice((I32_MIN - 1, I32_MAX + 1, 1 << 40, -(1 << 40))), rng.choice((0, 1, -1, I32_MIN, I32_MAX))
    if group == "both_wide_product_fits":
        # Both magnitudes exceed signed32, while their product remains in i64.
        a = rng.choice((1, -1)) * ((1 << 31) + rng.randint(1, 4))
        b = rng.choice((1, -1)) * ((1 << 31) + rng.randint(1, 4))
        return a, b
    if group == "both_wide_product_overflows":
        a = rng.choice((1, -1)) * ((1 << 32) + rng.randint(1, 7))
        b = rng.choice((1, -1)) * ((1 << 31) + rng.randint(1, 7))
        return a, b
    if group == "near_32_boundary":
        return _near32(rng), _near32(rng)
    if group == "near_64_boundary":
        # Explicitly include the signed division-style corner and zero products.
        special = ((I64_MIN, -1), (I64_MIN, 0), (I64_MAX, 1),
                   (I64_MAX, -1), (I64_MIN, 1), (I64_MAX, 2))
        if index < len(special):
            return special[index]
        return _near64(rng), rng.choice((0, 1, -1, 2, -2))
    if group == "mixed_randomized_widths":
        def signed_width():
            width = rng.randint(1, 64)
            raw = rng.getrandbits(width)
            return raw - (1 << width) if raw >> (width - 1) else raw
        return signed_width(), signed_width()
    raise ValueError(f"unknown multiplication width group: {group}")


def vector(case, index):
    group = case["mul_width_group"]
    rng = random.Random(0x6D554C + GROUPS.index(group) * 0x10000 + index)
    a, b = _pair(group, index, rng)
    c = rng.getrandbits(64)
    return (_raw(a), _raw(b), c, 0, 0, 0)


def catalog():
    return [dict(id=index, name=f"i64_checked_mul_width_{group}",
                 kind="integer", type="i64", bits=64, signed=True,
                 op="mul", mode="checked", ulp_limit=0, c_available=True,
                 mul_width_group=group)
            for index, group in enumerate(GROUPS)]


def generate_corpus(out):
    """Reuse generate.py's kernel emitter and integers.py's exact oracle."""
    original_catalog, original_vector = generate.catalog, integers.vector
    try:
        generate.catalog = lambda include_float=True: catalog()
        integers.vector = vector
        proof = generate.generate(Path(out), include_float=False)
    finally:
        generate.catalog, integers.vector = original_catalog, original_vector

    # The generic emitter records its own inputs; bind this extension source too.
    proof["sources"]["mul_widths.py"] = hashlib.sha256((HERE / "mul_widths.py").read_bytes()).hexdigest()
    coverage_path = Path(out) / "coverage.json"
    coverage_path.write_text(json.dumps(proof, separators=(",", ":")) + "\n")
    return json.loads(coverage_path.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = generate_corpus(args.out)
    print(f"ARITHMETIC_MUL_WIDTHS_PASS groups={len(report['cases'])} vectors={len(report['cases']) * SAMPLES}")
