"""Bounded, stratified qualification vectors for float-to-i64/u64 casts."""
import argparse
import hashlib
import json
from fractions import Fraction
from pathlib import Path
import random

import conversions
import floating
import generate

HERE = Path(__file__).resolve().parent
SAMPLES = 64
GROUPS = ("in_range_finite", "low_negative_overflow", "high_overflow", "nan")
FAMILIES = ((32, "i64", True), (32, "u64", False),
            (64, "i64", True), (64, "u64", False))


def _format(bits):
    return (8, 23, 127) if bits == 32 else (11, 52, 1023)


def _encode(value, bits, sign=0):
    return floating.encode_fraction(Fraction(value), bits, sign)


def _nextafter_bits(bits, raw, upward):
    """Return the adjacent IEEE encoding in the requested numeric direction."""
    sign = 1 << (bits - 1)
    magnitude = raw & (sign - 1)
    if magnitude == 0:
        return 1 if upward else sign | 1
    if raw & sign:
        return raw - 1 if upward else raw + 1
    return raw + 1 if upward else raw - 1


def _edge_values(case, group):
    source, signed = case["source_bits"], case["signed"]
    sign_mask = 1 << (source - 1)
    exponent_mask = 0x7f800000 if source == 32 else 0x7ff0000000000000
    quiet_bit = 1 << (22 if source == 32 else 51)
    inf = exponent_mask
    if group == "in_range_finite":
        values = [0, sign_mask]  # +0 and -0
        if signed:
            lower = _encode(-(1 << 63), source)
            upper = _encode(1 << 63, source)
            values += [lower, _nextafter_bits(source, lower, True),
                       _nextafter_bits(source, upper, False)]
        else:
            upper = _encode(1 << 64, source)
            values += [1, _nextafter_bits(source, 0, True),
                       _nextafter_bits(source, upper, False)]
        return values
    if group == "low_negative_overflow":
        if signed:
            lower = _encode(-(1 << 63), source)
            return [inf | sign_mask,
                    _nextafter_bits(source, lower, False)]
        return [inf | sign_mask, sign_mask | 1, _encode(-1, source)]
    if group == "high_overflow":
        threshold = 1 << (63 if signed else 64)
        upper = _encode(threshold, source)
        return [inf, upper, _nextafter_bits(source, upper, True)]
    if group == "nan":
        return [sign | exponent_mask | quiet_bit | 1
                for sign in (0, sign_mask)] + [
                    sign | exponent_mask | 1 for sign in (0, sign_mask)]
    raise ValueError(f"unknown conversion range group: {group}")


def _random_finite(case, group, rng):
    source, signed = case["source_bits"], case["signed"]
    if group == "in_range_finite":
        # Vary exponents rather than concentrating near the 64-bit limits,
        # where even binary64 cannot represent fractional values.
        exponent = rng.randint(-12, 61 if signed else 62)
        magnitude = Fraction(rng.randint(1, 65535), 65536)
        scale = Fraction(1 << exponent) if exponent >= 0 else Fraction(1, 1 << -exponent)
        value = magnitude * scale
        if signed and rng.randrange(2):
            value = -value
        return _encode(value, source)
    if group == "low_negative_overflow":
        if signed:
            precision = 24 if source == 32 else 53
            step = 1 << (63 - precision + 1)
            return _encode(-(Fraction(1 << 63) + step * rng.randint(1, 100000)), source)
        return _encode(-rng.randint(1, 1 << 40), source)
    if group == "high_overflow":
        exponent = 63 if signed else 64
        precision = 24 if source == 32 else 53
        step = 1 << (exponent - precision + 1)
        return _encode(Fraction(1 << exponent) + step * rng.randint(1, 100000), source)
    if group == "nan":
        _, mantissa_bits, _ = _format(source)
        quiet_bit = 1 << (mantissa_bits - 1)
        exponent_mask = 0x7f800000 if source == 32 else 0x7ff0000000000000
        fraction_mask = (1 << mantissa_bits) - 1
        payload = rng.randint(1, fraction_mask)
        if rng.randrange(2):
            payload |= quiet_bit
        else:
            payload &= ~quiet_bit
            payload |= 1
        return (rng.randrange(2) << (source - 1)) | exponent_mask | payload
    raise ValueError(f"unknown conversion range group: {group}")


def catalog():
    cases = []
    for source, target, signed in FAMILIES:
        for group in GROUPS:
            name = f"f{source}_as_{target}_range_{group}"
            cases.append(dict(
                id=len(cases), name=name, kind="conversion", op="float_to_int", bits=64,
                source_bits=source, signed=signed, type=target, float_bits=0,
                ulp_limit=0, c_available=True, conversion_family=f"f{source}_{target}",
                conversion_range_group=group))
    return cases


def vector(case, index):
    group = case["conversion_range_group"]
    seed = (case["source_bits"] << 24) ^ (int(case["signed"]) << 20) ^ (GROUPS.index(group) << 16) ^ index
    rng = random.Random(seed)
    edges = _edge_values(case, group)
    raw = edges[index] if index < len(edges) else _random_finite(case, group, rng)
    return raw, 0, 0, 0, 0, 0


def generate_corpus(out):
    """Use the common emitter/build contract while substituting this corpus."""
    original_catalog, original_vector = generate.catalog, conversions.vector
    try:
        generate.catalog = lambda include_float=True: catalog()
        conversions.vector = vector
        proof = generate.generate(Path(out), include_float=False)
    finally:
        generate.catalog, conversions.vector = original_catalog, original_vector

    for name in ("conversion_ranges.py", "conversions.py", "floating.py"):
        proof["sources"][name] = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
    coverage_path = Path(out) / "coverage.json"
    coverage_path.write_text(json.dumps(proof, separators=(",", ":")) + "\n")
    return json.loads(coverage_path.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = generate_corpus(args.out)
    print(f"ARITHMETIC_CONVERSION_RANGES_PASS families={len(FAMILIES)} groups={len(GROUPS)} vectors={len(report['cases']) * SAMPLES}")
