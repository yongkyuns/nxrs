"""Protect oracle semantics and the defined C/Rust coverage contract."""
import unittest
import integers


def case(name):
    return next(value for value in integers.operations() if value["name"] == name)


class IntegerOracleTests(unittest.TestCase):
    def value(self, name, a, b=0):
        mask = (1 << 64) - 1
        return integers.expected(case(name), (a & mask, b & mask, 0, a >> 64, b >> 64, 0))

    def test_wrapping_checked_saturating_overflowing_are_distinct(self):
        self.assertEqual(self.value("i8_wrapping_add", 127, 1), (128, 0, 0, 0))
        self.assertEqual(self.value("i8_checked_add", 127, 1), (0, 0, 1, 0))
        self.assertEqual(self.value("i8_saturating_add", 127, 1), (127, 0, 0, 0))
        self.assertEqual(self.value("i8_overflowing_add", 127, 1), (128, 0, 1, 0))

    def test_min_division_and_zero_have_explicit_contracts(self):
        self.assertEqual(self.value("i8_checked_div", 128, 255), (0, 0, 1, 0))
        self.assertEqual(self.value("i8_wrapping_div", 128, 255), (128, 0, 0, 0))
        self.assertEqual(self.value("i8_saturating_div", 128, 255), (127, 0, 0, 0))
        self.assertEqual(self.value("u64_checked_rem", 123, 0), (0, 0, 1, 0))

    def test_euclidean_and_truncated_division_differ(self):
        self.assertEqual(self.value("i8_wrapping_div", 249, 3), (254, 0, 0, 0))
        self.assertEqual(self.value("i8_wrapping_div_euclid", 249, 3), (253, 0, 0, 0))
        self.assertEqual(self.value("i8_wrapping_rem_euclid", 249, 253), (2, 0, 0, 0))

    def test_invalid_shift_flag_is_about_count_not_discarded_value_bits(self):
        self.assertEqual(self.value("u8_overflowing_shl", 255, 1), (254, 0, 0, 0))
        self.assertEqual(self.value("u8_overflowing_shl", 255, 8), (255, 0, 1, 0))
        self.assertEqual(self.value("i8_wrapping_shr", 128, 7), (255, 0, 0, 0))

    def test_u128_has_exact_multiword_expectations_but_no_c_ratio(self):
        self.assertEqual(self.value("u128_overflowing_add", (1 << 128)-1, 1), (0, 0, 1, 0))
        self.assertFalse(case("u128_wrapping_add")["c_available"])
        self.assertFalse(case("i64_as_u128")["c_available"])

    def test_zero_counts_and_bit_reversal_are_defined(self):
        self.assertEqual(self.value("u8_leading_zeros", 0), (8, 0, 0, 0))
        self.assertEqual(self.value("u64_trailing_ones", (1 << 64)-1), (64, 0, 0, 0))
        self.assertEqual(self.value("u16_reverse_bits", 1), (32768, 0, 0, 0))

    def test_catalog_has_unique_names_and_no_nonexistent_unsigned_saturating_neg(self):
        cases = integers.operations()
        names = [value["name"] for value in cases]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("u32_saturating_neg", names)
        self.assertTrue(all(value["bits"] in (8, 16, 32, 64, 128) for value in cases))

    def test_division_boundary_vectors_include_zero_and_min_over_minus_one(self):
        test = case("i32_checked_div")
        vectors = [integers.vector(test, i) for i in range(64)]
        self.assertTrue(any(v[1] == 0 for v in vectors))
        self.assertTrue(any(v[:2] == (1 << 31, (1 << 32)-1) for v in vectors))
        wrapped = case("i32_wrapping_div")
        self.assertTrue(all(integers.vector(wrapped, i)[1] for i in range(64)))


if __name__ == "__main__": unittest.main()
