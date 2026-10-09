"""Check the bounded-product identity and the independent execution oracle."""
import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    'mul_ranges', Path(__file__).parent / 'compiler/check_mul_ranges.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class MulRangeTests(unittest.TestCase):
    def test_all_nonnegative_unsigned_fitting_byte_products(self):
        checks = 0
        for a in range(128):
            for b in range(128):
                if a * b >= 256:
                    continue
                product, overflow = probe.oracle(a, b, 8)
                self.assertEqual(overflow, product < 0, (a, b))
                checks += 1
        self.assertGreater(checks, 1000)

    def test_unsigned_bound_is_required(self):
        product, overflow = probe.oracle(17, 17, 8)
        self.assertTrue(overflow)
        self.assertGreater(product, 0)

    def test_nonnegative_operands_are_required(self):
        product, overflow = probe.oracle(-1, 1, 8)
        self.assertFalse(overflow)
        self.assertLess(product, 0)

    def test_wrapped_product_and_signed_boundary(self):
        for bits in (8, 64, 128):
            limit = 1 << (bits - 1)
            self.assertEqual(probe.oracle(limit // 2, 2, bits), (-limit, True))
            self.assertEqual(probe.oracle(limit - 1, 1, bits), (limit - 1, False))
            self.assertEqual(probe.oracle(-limit, -1, bits), (-limit, True))

    def test_edge_values_cover_every_bit_boundary(self):
        for bits in (32, 40, 64, 65, 88):
            values = probe.edges(bits)
            self.assertEqual(values, sorted(set(values)))
            self.assertEqual((values[0], values[-1]), (0, (1 << bits) - 1))
            for bit in range(bits):
                self.assertIn(1 << bit, values)


if __name__ == '__main__':
    unittest.main()
