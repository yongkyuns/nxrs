"""Mathematical qualification of proposal 0021, not application arithmetic.

This models its wrapped-product and overflow outputs. Exact Python integers
provide the independent reference. Passing this does not execute the compiler
lowering or establish device performance.
"""
import argparse
import itertools
import json
import random

from integers import signed_value


def product(a, b, bits):
    wrapped = signed_value(a * b, bits)
    normalized_a = a ^ (-1 if a < 0 else 0)
    normalized_b = b ^ (-1 if b < 0 else 0)
    count = 2 * bits - normalized_a.bit_length() - normalized_b.bit_length()
    wrong_sign = (wrapped < 0) != ((a < 0) != (b < 0))
    overflow = count <= bits if wrapped == 0 else count < bits or wrong_sign
    return wrapped, overflow


def boundary_values(bits):
    values = {0, 1, -1}
    for shift in range(bits):
        for offset in (-1, 0, 1):
            for sign in (-1, 1):
                value = sign * ((1 << shift) + offset)
                if -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
                    values.add(value)
    return sorted(values)


def check(a, b, bits):
    exact = a * b
    expected = (signed_value(exact, bits),
                not (-(1 << (bits - 1)) <= exact < (1 << (bits - 1))))
    if product(a, b, bits) != expected:
        raise ValueError(f"signed overflow mismatch: {bits=} {a=} {b=}")


def qualify(random_pairs=100000):
    counts = {}
    for bits in range(2, 9):
        values = range(-(1 << (bits - 1)), 1 << (bits - 1))
        for a, b in itertools.product(values, repeat=2):
            check(a, b, bits)
        counts[str(bits)] = len(values) ** 2
    for bits in (16, 32, 64, 128):
        values = boundary_values(bits)
        for a, b in itertools.product(values, repeat=2):
            check(a, b, bits)
        rng = random.Random(0x434C5A + bits)
        for _ in range(random_pairs):
            def operand():
                width = rng.randint(1, bits)
                return signed_value(rng.getrandbits(width), width)
            check(operand(), operand(), bits)
        counts[str(bits)] = len(values) ** 2 + random_pairs
    return dict(schema=1, algorithm="leading-sign-bits", checks=counts,
                random_pairs_per_wide_type=random_pairs, mismatches=0,
                limitation="Mathematical model, not compiler or device execution")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--random-pairs", type=int, default=100000)
    args = parser.parse_args()
    if args.random_pairs < 0:
        parser.error("random-pairs must be nonnegative")
    print(json.dumps(qualify(args.random_pairs), indent=2))
