"""Boundary tests for casts that differ from C's unguarded cast semantics."""
from fractions import Fraction
import unittest

import conversions
import floating


def case(name):
    return next(row for row in conversions.operations() if row["name"] == name)


class ConversionTests(unittest.TestCase):
    def check(self, name, raw, expected):
        self.assertEqual(conversions.expected(case(name), (raw, 0, 0, 0, 0, 0)), expected)

    def test_float_to_integer_nan_infinity_and_negative_unsigned(self):
        self.check("f32_as_u32", 0x7fc00000, (0, 0, 0, 0))
        self.check("f32_as_u32", 0x7f800000, (0xffffffff, 0, 0, 0))
        self.check("f32_as_u32", 0xbf800000, (0, 0, 0, 0))
        self.check("f64_as_i64", 0xfff0000000000000, (1 << 63, 0, 0, 0))

    def test_fractional_cast_truncates_toward_zero(self):
        self.check("f32_as_i8", 0xbfc00000, (255, 0, 0, 0))
        self.check("f32_as_i8", 0x3fc00000, (1, 0, 0, 0))

    def test_integer_float_cast_rounds_once(self):
        self.check("u32_as_f32", (1 << 24)+1, (0x4b800000, 0, 0, 0))
        self.check("i32_as_f64", 0xffffffff, (0xbff0000000000000, 0, 0, 0))

    def test_float_width_cast_preserves_negative_zero(self):
        self.check("f32_as_f64", 0x80000000, (1 << 63, 0, 0, 0))
        self.check("f64_as_f32", 1 << 63, (0x80000000, 0, 0, 0))

    def test_f64_to_f32_subnormal_rounding_boundary(self):
        raw = floating.encode_fraction(Fraction(1, 1 << 150), 64)
        self.check("f64_as_f32", raw, (0, 0, 0, 0))
        raw = floating.encode_fraction(Fraction(3, 1 << 150), 64)
        self.check("f64_as_f32", raw, (2, 0, 0, 0))

    def test_native_c_128bit_cast_is_not_invented(self):
        self.assertFalse(case("u128_as_f64")["c_available"])
        self.assertFalse(case("f32_as_i128")["c_available"])

    def test_catalog_vector_shapes_and_generated_range_guards(self):
        self.assertEqual(len(conversions.operations()), 52)
        for row in conversions.operations():
            self.assertTrue(conversions.r_body(row))
            if row["c_available"]:
                self.assertTrue(conversions.c_body(row))
            for index in range(64):
                value = conversions.expected(row, conversions.vector(row, index))
                self.assertEqual(len(value), 4)
        body = conversions.c_body(case("f64_as_i64"))
        self.assertIn("value!=value", body)
        self.assertIn("value>=0x1p63", body)


if __name__ == "__main__":
    unittest.main()
