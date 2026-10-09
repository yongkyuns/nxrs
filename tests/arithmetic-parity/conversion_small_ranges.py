"""Small, stratified float-to-integer conversion qualification vectors."""
import argparse
import hashlib
import json
from fractions import Fraction
from pathlib import Path

import conversion_ranges
import conversions
import floating
import generate

HERE = Path(__file__).resolve().parent
SAMPLES = conversion_ranges.SAMPLES
GROUPS = conversion_ranges.GROUPS
FAMILIES = tuple(
    (source, f"{prefix}{width}", signed, width)
    for source in (32, 64)
    for width in (8, 16, 32)
    for prefix, signed in (("i", True), ("u", False))
)


def _sign_mask(source):
    return 1 << (source - 1)


def _edge_values(case, group):
    source, width, signed = case["source_bits"], case["bits"], case["signed"]
    sign_mask = _sign_mask(source)
    exponent_mask = 0x7f800000 if source == 32 else 0x7ff0000000000000
    inf = exponent_mask
    if group == "in_range_finite":
        values = [0, sign_mask]
        if signed:
            lower = conversion_ranges._encode(-(1 << (width - 1)), source)
            upper = conversion_ranges._encode(1 << (width - 1), source)
            values += [lower, conversion_ranges._nextafter_bits(source, lower, True),
                       conversion_ranges._nextafter_bits(source, upper, False)]
        else:
            upper = conversion_ranges._encode(1 << width, source)
            values += [conversion_ranges._nextafter_bits(source, 0, True),
                       conversion_ranges._nextafter_bits(source, upper, False)]

        # These maxima and in-between values are exactly representable in both
        # source formats for the 8- and 16-bit targets.
        if width in (8, 16):
            maximum = (1 << (width - 1)) - 1 if signed else (1 << width) - 1
            values += [conversion_ranges._encode(maximum, source),
                       conversion_ranges._encode(Fraction(2 * maximum + 1, 2), source)]
        return values
    if group == "low_negative_overflow":
        if signed:
            lower = conversion_ranges._encode(-(1 << (width - 1)), source)
            return [inf | sign_mask,
                    conversion_ranges._nextafter_bits(source, lower, False)]
        return [inf | sign_mask, sign_mask | 1,
                conversion_ranges._encode(-1, source)]
    if group == "high_overflow":
        upper = conversion_ranges._encode(1 << (width - 1 if signed else width), source)
        return [inf, upper, conversion_ranges._nextafter_bits(source, upper, True)]
    if group == "nan":
        base = conversion_ranges.catalog()[0]
        base["source_bits"] = source
        base["signed"] = signed
        base["conversion_range_group"] = "nan"
        return [conversion_ranges.vector(base, index)[0] for index in range(4)]
    raise ValueError(f"unknown conversion range group: {group}")


def _scaled_base_raw(case, index):
    source, width = case["source_bits"], case["bits"]
    base = next(row for row in conversion_ranges.catalog()
                if row["source_bits"] == source and row["signed"] == case["signed"])
    base["conversion_range_group"] = case["conversion_range_group"]
    raw = conversion_ranges.vector(base, index)[0]
    kind, value, sign = floating.classify(raw, source)
    if kind != "finite":
        return raw

    exponent = width - 64
    scale = Fraction(1 << exponent) if exponent >= 0 else Fraction(1, 1 << -exponent)
    scaled = floating.encode_fraction(value * scale, source, sign)
    scaled_kind, scaled_value, _ = floating.classify(scaled, source)
    if value < 0 and scaled_kind == "finite" and scaled_value == 0:
        # Keep negative underflow in the unsigned low group instead of turning
        # it into negative zero, which belongs to the in-range group.
        return _sign_mask(source) | 1
    return scaled


def catalog():
    cases = []
    for source, target, signed, width in FAMILIES:
        for group in GROUPS:
            cases.append(dict(
                id=len(cases), name=f"f{source}_as_{target}_range_{group}",
                kind="conversion", op="float_to_int", bits=width,
                source_bits=source, signed=signed, type=target, float_bits=0,
                ulp_limit=0, c_available=True,
                conversion_family=f"f{source}_{target}",
                conversion_range_group=group))
    return cases


def vector(case, index):
    group = case["conversion_range_group"]
    edges = _edge_values(case, group)
    raw = edges[index] if index < len(edges) else _scaled_base_raw(case, index)
    return raw, 0, 0, 0, 0, 0


def generate_corpus(out):
    """Emit this corpus through the shared generator, restoring providers."""
    original_catalog, original_vector = generate.catalog, conversions.vector
    try:
        generate.catalog = lambda include_float=True: catalog()
        conversions.vector = vector
        proof = generate.generate(Path(out), include_float=False)
    finally:
        generate.catalog, conversions.vector = original_catalog, original_vector

    for name in ("conversion_small_ranges.py", "conversion_ranges.py",
                 "conversions.py", "floating.py"):
        proof["sources"][name] = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
    coverage_path = Path(out) / "coverage.json"
    coverage_path.write_text(json.dumps(proof, separators=(",", ":")) + "\n")
    return json.loads(coverage_path.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = generate_corpus(args.out)
    print(f"ARITHMETIC_SMALL_CONVERSION_RANGES_PASS families={len(FAMILIES)} "
          f"groups={len(GROUPS)} vectors={len(report['cases']) * SAMPLES}")
