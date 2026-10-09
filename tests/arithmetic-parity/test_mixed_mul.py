"""Arithmetic proof for the mixed-width LLVM proposal, not target execution."""
import itertools
import random
import unittest

from integers import signed_value


def product_words(a, b, bits, a_signed, b_signed):
    """Model native unsigned low/high multiply with two's-complement correction."""
    mask = (1 << bits) - 1
    a, b = a & mask, b & mask
    low, high = (a * b) & mask, (a * b) >> bits
    if a_signed:
        high -= (signed_value(a, bits) >> (bits - 1)) & b
    if b_signed:
        high -= (signed_value(b, bits) >> (bits - 1)) & a
    return low, high & mask


def full_product(a, b, bits, a_signed, b_signed):
    low, high = product_words(a, b, bits, a_signed, b_signed)
    return low | (high << bits)


class MixedMultiplyTests(unittest.TestCase):
    def check_pair(self, signed_raw, unsigned_raw, bits):
        expected = (signed_value(signed_raw, bits) * unsigned_raw) & ((1 << (2 * bits)) - 1)
        self.assertEqual(full_product(signed_raw, unsigned_raw, bits, True, False), expected)
        self.assertEqual(full_product(unsigned_raw, signed_raw, bits, False, True), expected)

    def test_exhaustive_8bit_in_both_operand_orders(self):
        for a, b in itertools.product(range(256), repeat=2):
            self.check_pair(a, b, 8)

    def test_native_width_boundaries_and_seeded_random(self):
        rng = random.Random(0x4D554C)
        for bits in (32, 64):
            mask, sign = (1 << bits) - 1, 1 << (bits - 1)
            edges = (0, 1, 2, sign - 1, sign, sign + 1, mask - 1, mask)
            for a, b in itertools.product(edges, repeat=2):
                self.check_pair(a, b, bits)
            for _ in range(10000):
                self.check_pair(rng.getrandbits(bits), rng.getrandbits(bits), bits)

    def test_signed64_partial_products_preserve_result_and_overflow(self):
        rng = random.Random(0x4D554C)
        mask32, mask64 = (1 << 32) - 1, (1 << 64) - 1
        edges = (0, 1, mask32, 1 << 32, (1 << 63) - 1, 1 << 63, mask64)
        pairs = itertools.chain(itertools.product(edges, repeat=2),
                                ((rng.getrandbits(64), rng.getrandbits(64))
                                 for _ in range(10000)))
        for a, b in pairs:
            al, ah, bl, bh = a & mask32, a >> 32, b & mask32, b >> 32
            # Unsigned low limbs; signed high limbs. Reconstruct all 128 bits.
            p00 = full_product(al, bl, 32, False, False)
            p10 = signed_value(full_product(ah, bl, 32, True, False), 64)
            p01 = signed_value(full_product(al, bh, 32, False, True), 64)
            p11 = full_product(ah, bh, 32, True, True)
            full = (p00 + ((p10 + p01) << 32) + (p11 << 64)) & ((1 << 128) - 1)
            exact = signed_value(a, 64) * signed_value(b, 64)
            self.assertEqual(full, exact & ((1 << 128) - 1))
            low, high = full & mask64, full >> 64
            overflow = high != (mask64 if low >> 63 else 0)
            self.assertEqual(overflow, not -(1 << 63) <= exact < (1 << 63))


if __name__ == "__main__":
    unittest.main()
