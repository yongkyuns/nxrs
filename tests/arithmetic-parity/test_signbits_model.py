"""Protect the signed-range boundaries used by the compiler RFC model."""
import itertools
import random
import unittest

import integers
import signbits_model


class SignBitsModelTests(unittest.TestCase):
    def test_every_signed_eight_bit_pair_including_wrapped_overflow(self):
        for a, b in itertools.product(range(-128, 128), repeat=2):
            signbits_model.check(a, b, 8)

    def test_wide_signed_limits_zero_and_exact_modulus_products(self):
        for bits in (16, 32, 64, 128):
            minimum = -(1 << (bits - 1))
            for a, b in ((minimum, -1), (minimum, 0), (minimum, 1),
                         (minimum, minimum), (0, -1), (-1, -1),
                         (-(1 << (bits // 2)), -(1 << (bits // 2))),
                         (1 << (bits // 2 - 1), 1 << (bits // 2))):
                with self.subTest(bits=bits, a=a, b=b):
                    signbits_model.check(a, b, bits)

    def test_random_widths_match_existing_overflowing_multiply_oracle(self):
        rng = random.Random(0x434C5A)
        for bits in (8, 16, 32, 64, 128):
            case = dict(bits=bits, signed=True, op="mul", mode="overflowing")
            mask = (1 << 64) - 1
            for _ in range(1000):
                a = integers.signed_value(rng.getrandbits(bits), bits)
                b = integers.signed_value(rng.getrandbits(bits), bits)
                inputs = (a & mask, b & mask, 0, a >> 64, b >> 64, 0)
                low, high, overflow, _ = integers.expected(case, inputs)
                expected = integers.signed_value(low | high << 64, bits), bool(overflow)
                self.assertEqual(signbits_model.product(a, b, bits), expected)


if __name__ == "__main__":
    unittest.main()
