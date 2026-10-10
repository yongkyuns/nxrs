"""Focused tests for floating kernel generation and oracle edge semantics."""

import unittest

import mpmath

import floating


def case(name: str) -> dict:
    return next(item for item in floating.operations() if item["name"] == name)


def sample32(a: int, b: int = 0, c: int = 0) -> tuple[int, int, int, int, int, int]:
    return a, b, c, 0, 0, 0


def sample64(a: int, b: int = 0, c: int = 0) -> tuple[int, int, int, int, int, int]:
    return a, b, c, 0, 0, 0


class FloatingGeneratorTests(unittest.TestCase):
    def test_manifest_has_required_shapes_and_libm_cases(self):
        items = floating.operations()
        self.assertEqual(len(items), 84)
        self.assertTrue(all({"name", "kind", "bits", "op", "ulp_limit"} <= set(item) for item in items))
        names = {item["name"] for item in items}
        for name in ("f32_fma", "f64_sqrt", "f32_powf", "f64_atan2", "f64_hypot"):
            self.assertIn(name, names)
        self.assertIn("conversions.py", floating.DELEGATED_OPERATIONS["powi"])
        self.assertFalse(case("f32_min")["zero_sign_required"])
        self.assertFalse(case("f64_max")["zero_sign_required"])

    def test_generated_bodies_use_contract_fields_and_clear_result(self):
        c = floating.c_body(case("f32_fma"))
        r = floating.r_body(case("f64_fma"))
        self.assertNotIn("const struct aq_input *p = &inputs[i];", c)
        self.assertNotIn("struct aq_result *q = &out[i];", c)
        self.assertIn("aq_bits_f32(fmaf(", c)
        self.assertIn("q->reserved = 0;", c)
        self.assertNotIn("let p = &*inputs.add(i);", r)
        self.assertNotIn("let q = &mut *out.add(i);", r)
        self.assertIn("f64::from_bits(p.a as u64)", r)
        self.assertIn(".mul_add(", r)
        self.assertIn("q.reserved = 0;", r)
        self.assertIn("pow(", floating.c_body(case("f64_powf")))
        self.assertIn(".powf(", floating.r_body(case("f32_powf")))

    def test_vector_generation_is_stable_and_has_directed_specials(self):
        k = case("f32_add")
        self.assertEqual(floating.vector(k, 17), floating.vector(k, 17))
        rows = [floating.vector(k, i) for i in range(12)]
        self.assertIn(0x80000000, [row[0] for row in rows])
        self.assertIn(0x7FC12345, [row[0] for row in rows])
        self.assertIn(0x00000001, [row[0] for row in rows])
        self.assertEqual(rows[4][3:], (0, 0, 0))
        self.assertNotEqual(rows[0][:3], rows[1][:3])
        self.assertEqual(floating.vector(case("f64_add"), 4)[0], 0x0000000000000001)

    def test_exact_add_preserves_zero_sign_and_canonicalizes_nan(self):
        self.assertEqual(floating.expected(case("f32_add"), sample32(0x80000000, 0x80000000)),
                         (0x80000000, 0, 0, 0))
        self.assertEqual(floating.expected(case("f32_add"), sample32(0x7FC12345, 0x3F800000)),
                         (0x7FC00000, 0, 0, 0))
        self.assertEqual(floating.expected(case("f32_round"), sample32(0xFFC12345)),
                         (0x7FC00000, 0, 0, 0))
        self.assertEqual(floating.expected(case("f32_abs"), sample32(0xFFC12345)),
                         (0x7FC00000, 0, 0, 0))
        self.assertEqual(floating.expected(case("f64_add"), sample64(0x8000000000000000, 0x8000000000000000)),
                         (0x8000000000000000, 0, 0, 0))

    def test_fma_is_single_rounded_and_sqrt_is_correctly_rounded(self):
        # (1 + 2^-23) * (1 - 2^-23) - 1 = -2^-46 exactly.
        result = floating.expected(case("f32_fma"), sample32(0x3F800001, 0x3F7FFFFE, 0xBF800000))
        self.assertEqual(result, (0xA8800000, 0, 0, 0))
        self.assertEqual(floating.expected(case("f32_sqrt"), sample32(0x40800000)),
                         (0x40000000, 0, 0, 0))

    def test_rounding_and_minmax_keep_ieee_zero_semantics(self):
        self.assertEqual(floating.expected(case("f32_round"), sample32(0xBFC00000))[0], 0xC0000000)
        self.assertEqual(floating.expected(case("f32_round_ties_even"), sample32(0x40200000))[0], 0x40000000)
        self.assertEqual(floating.expected(case("f32_min"), sample32(0x00000000, 0x80000000))[0], 0x80000000)
        self.assertEqual(floating.expected(case("f32_max"), sample32(0x00000000, 0x80000000))[0], 0x00000000)
        # NaN payload selection is not the semantic comparison contract; raw
        # C/Rust output bits remain separately observable by the driver.
        self.assertEqual(floating.expected(case("f32_min"), sample32(0x7FC12345, 0xFFC54321))[0], 0x7FC00000)

    def test_transcendentals_use_required_high_precision_oracle(self):
        self.assertEqual(mpmath.__version__, "1.3.0")
        self.assertEqual(floating.expected(case("f64_exp"), sample64(0))[0], 0x3FF0000000000000)
        self.assertEqual(floating.expected(case("f32_ln"), sample32(0x3F800000))[0], 0)
        self.assertEqual(floating.expected(case("f32_powf"), sample32(0xC0000000, 0x3F000000))[0], 0x7FC00000)
        self.assertEqual(floating.expected(case("f32_sin"), sample32(0x80000000))[0], 0x80000000)

    def test_public_exact_ieee_helpers(self):
        self.assertEqual(floating.classify(0x80000000, 32), ("finite", floating.Fraction(0), 1))
        self.assertEqual(floating.classify(0x7F800000, 32), ("inf", None, 0))
        self.assertEqual(floating.classify(0x7FC00000, 32), ("nan", None, 0))
        self.assertEqual(floating.encode_fraction(floating.Fraction(0), 32, 1), 0x80000000)
        self.assertEqual(floating.encode_fraction(floating.Fraction(1, 3), 32), 0x3EAAAAAB)

    def test_sqrt_and_fract_special_values(self):
        self.assertEqual(floating.expected(case("f32_sqrt"), sample32(0x80000000))[0], 0x80000000)
        self.assertEqual(floating.expected(case("f32_fract"), sample32(0x7F800000))[0], 0x7FC00000)
        self.assertEqual(floating.expected(case("f64_fract"), sample64(0xFFF0000000000000))[0],
                         0x7FF8000000000000)

    def test_powf_ieee_nan_zero_and_signed_zero_rules(self):
        def pow32(x: int, y: int) -> int:
            return floating.expected(case("f32_powf"), sample32(x, y))[0]

        self.assertEqual(pow32(0x7FC12345, 0), 0x3F800000)  # NaN ** 0
        self.assertEqual(pow32(0x3F800000, 0x7FC12345), 0x3F800000)  # 1 ** NaN
        self.assertEqual(pow32(0, 0xC0000000), 0x7F800000)  # +0 ** -2
        self.assertEqual(pow32(0x80000000, 0xC0400000), 0xFF800000)  # -0 ** -3
        self.assertEqual(pow32(0x80000000, 0x40400000), 0x80000000)  # -0 ** 3
        self.assertEqual(pow32(0, 0x7F800000), 0)
        self.assertEqual(pow32(0, 0xFF800000), 0x7F800000)

    def test_atan2_signed_zero_and_infinity_quadrants(self):
        def atan2_32(y: int, x: int) -> int:
            return floating.expected(case("f32_atan2"), sample32(y, x))[0]

        self.assertEqual(atan2_32(0, 0), 0)
        self.assertEqual(atan2_32(0x80000000, 0), 0x80000000)
        self.assertEqual(atan2_32(0, 0x80000000), 0x40490FDB)
        self.assertEqual(atan2_32(0x80000000, 0x80000000), 0xC0490FDB)
        self.assertEqual(atan2_32(0x7F800000, 0x7F800000), 0x3F490FDB)
        self.assertEqual(atan2_32(0x7F800000, 0xFF800000), 0x4016CBE4)

    def test_atanh_and_hyperbolic_infinities(self):
        self.assertEqual(floating.expected(case("f32_atanh"), sample32(0x3F800000))[0], 0x7F800000)
        self.assertEqual(floating.expected(case("f32_atanh"), sample32(0xBF800000))[0], 0xFF800000)
        self.assertEqual(floating.expected(case("f32_sinh"), sample32(0x7F800000))[0], 0x7F800000)
        self.assertEqual(floating.expected(case("f32_cosh"), sample32(0xFF800000))[0], 0x7F800000)
        self.assertEqual(floating.expected(case("f32_tanh"), sample32(0xFF800000))[0], 0xBF800000)

    def test_oracle_does_not_overflow_on_large_mpmath_exponents(self):
        self.assertEqual(floating.expected(case("f32_exp"), sample32(0x7F7FFFFF))[0], 0x7F800000)
        self.assertEqual(floating.expected(case("f64_cosh"), sample64(0x7FEFFFFFFFFFFFFF))[0],
                         0x7FF0000000000000)

    def test_unary_libm_ignores_unused_second_operand_nan(self):
        nan = 0x7FC12345
        with_nan = floating.expected(case("f32_sin"), sample32(0x3F000000, nan))[0]
        without_nan = floating.expected(case("f32_sin"), sample32(0x3F000000, 0))[0]
        self.assertEqual(with_nan, without_nan)

    def test_libm_device_vectors_include_special_and_invalid_domains(self):
        for name in ("f32_powf", "f64_ln", "f32_sin", "f64_atan2"):
            rows = [floating.vector(case(name), index) for index in range(64)]
            kinds = {floating.classify(row[0], case(name)["bits"])[0] for row in rows}
            self.assertIn("inf", kinds)
            self.assertIn("nan", kinds)
            self.assertTrue(any(row[0] == 1 << (case(name)["bits"]-1) for row in rows))

    def test_full_catalog_generates_and_evaluates_all_vectors(self):
        for item in floating.operations():
            self.assertTrue(floating.c_body(item))
            self.assertTrue(floating.r_body(item))
            for index in range(64):
                inputs = floating.vector(item, index)
                self.assertEqual(len(inputs), 6)
                self.assertEqual(inputs[3:], (0, 0, 0))
                result = floating.expected(item, inputs)
                self.assertEqual(len(result), 4)


if __name__ == "__main__":
    unittest.main()
