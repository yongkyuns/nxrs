"""Coverage and oracle checks for bounded float-to-i64/u64 qualification."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build
import conversion_ranges
import conversions
import floating
import generate
import measure


MASK64 = (1 << 64) - 1


def independent_cast(case, raw):
    """Compute Rust's saturating float cast without using conversions.expected."""
    kind, value, sign = floating.classify(raw, case["source_bits"])
    lower, upper = ((-(1 << 63), (1 << 63) - 1) if case["signed"]
                    else (0, (1 << 64) - 1))
    if kind == "nan":
        result = 0
    elif kind == "inf":
        result = lower if sign else upper
    else:
        magnitude = abs(value.numerator) // value.denominator
        result = -magnitude if value < 0 else magnitude
        result = min(upper, max(lower, result))
    return result & MASK64, 0, 0, 0


class ConversionRangeCorpusTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="aq-conversion-ranges-")
        self.generated = Path(self.folder.name) / "generated"
        self.proof = conversion_ranges.generate_corpus(self.generated)

    def tearDown(self):
        self.folder.cleanup()

    def case(self, name):
        return next(row for row in self.proof["cases"] if row["name"] == name)

    def raw_values(self, case):
        return [row[0] for row in case["inputs"]]

    def test_common_build_and_measure_contract_and_exact_source_hashes(self):
        self.assertEqual(len(self.proof["cases"]), 16)
        self.assertEqual(len(build.validate_generated(self.generated)["cases"]), 16)
        self.assertEqual(len(measure._manifest(self.proof)), 16)
        self.assertEqual(self.proof["samples_per_case"], conversion_ranges.SAMPLES)
        for name in ("conversion_ranges.py", "conversions.py", "floating.py"):
            expected = hashlib.sha256((conversion_ranges.HERE / name).read_bytes()).hexdigest()
            self.assertEqual(self.proof["sources"][name], expected)

    def test_deterministic_64_sample_groups_are_pure_and_independently_correct(self):
        second_folder = tempfile.TemporaryDirectory(prefix="aq-conversion-ranges-repeat-")
        try:
            second = conversion_ranges.generate_corpus(Path(second_folder.name) / "generated")
            self.assertEqual([case["inputs"] for case in self.proof["cases"]],
                             [case["inputs"] for case in second["cases"]])
        finally:
            second_folder.cleanup()

        for case in self.proof["cases"]:
            group = case["conversion_range_group"]
            self.assertEqual(len(case["inputs"]), 64, case["name"])
            self.assertEqual(len(case["expected"]), 64, case["name"])
            for inputs, expected in zip(case["inputs"], case["expected"]):
                kind, value, sign = floating.classify(inputs[0], case["source_bits"])
                lower = -(1 << 63) if case["signed"] else 0
                upper_exclusive = 1 << (63 if case["signed"] else 64)
                if group == "in_range_finite":
                    self.assertEqual(kind, "finite", (case["name"], inputs[0]))
                    self.assertLessEqual(lower, value, (case["name"], inputs[0]))
                    self.assertLess(value, upper_exclusive, (case["name"], inputs[0]))
                elif group == "low_negative_overflow":
                    self.assertTrue((kind == "inf" and sign == 1) or
                                    (kind == "finite" and value < lower),
                                    (case["name"], inputs[0], kind, value, sign))
                elif group == "high_overflow":
                    self.assertTrue((kind == "inf" and sign == 0) or
                                    (kind == "finite" and value >= upper_exclusive),
                                    (case["name"], inputs[0], kind, value, sign))
                else:
                    self.assertEqual(group, "nan")
                    self.assertEqual(kind, "nan", (case["name"], inputs[0]))

                independent = independent_cast(case, inputs[0])
                self.assertEqual(expected, list(independent), (case["name"], inputs[0]))
                self.assertEqual(expected, list(conversions.expected(case, inputs)),
                                 ("conversion oracle", case["name"], inputs[0]))

    def test_adjacent_thresholds_infinities_signed_zero_and_nan_encodings(self):
        for source in (32, 64):
            for target, signed in (("i64", True), ("u64", False)):
                family = f"f{source}_as_{target}"
                ordinary = self.raw_values(self.case(f"{family}_range_in_range_finite"))
                low = self.raw_values(self.case(f"{family}_range_low_negative_overflow"))
                high = self.raw_values(self.case(f"{family}_range_high_overflow"))
                nan = self.raw_values(self.case(f"{family}_range_nan"))
                sign_mask = 1 << (source - 1)
                exp_mask = 0x7f800000 if source == 32 else 0x7ff0000000000000
                quiet_bit = 1 << (22 if source == 32 else 51)

                self.assertIn(0, ordinary)
                self.assertIn(sign_mask, ordinary)  # negative zero remains in-range
                self.assertIn(sign_mask | exp_mask, low)  # -infinity
                self.assertIn(exp_mask, high)  # +infinity
                self.assertTrue(any(raw & quiet_bit for raw in nan))
                self.assertTrue(any(not raw & quiet_bit for raw in nan))
                self.assertTrue(any(not raw & sign_mask for raw in nan))
                self.assertTrue(any(raw & sign_mask for raw in nan))

                threshold = 1 << (63 if signed else 64)
                upper = conversion_ranges._encode(threshold, source)
                self.assertIn(conversion_ranges._nextafter_bits(source, upper, False), ordinary)
                self.assertIn(upper, high)
                self.assertIn(conversion_ranges._nextafter_bits(source, upper, True), high)
                if signed:
                    lower = conversion_ranges._encode(-(1 << 63), source)
                    self.assertIn(lower, ordinary)
                    self.assertIn(conversion_ranges._nextafter_bits(source, lower, True), ordinary)
                    self.assertIn(conversion_ranges._nextafter_bits(source, lower, False), low)
                else:
                    positive_min = conversion_ranges._nextafter_bits(source, 0, True)
                    negative_min = conversion_ranges._nextafter_bits(source, 0, False)
                    self.assertIn(positive_min, ordinary)
                    self.assertIn(negative_min, low)

    def test_finite_groups_include_fractional_small_and_large_values(self):
        for case in self.proof["cases"]:
            if case["conversion_range_group"] != "in_range_finite":
                continue
            values = [floating.classify(raw, case["source_bits"])[1]
                      for raw in self.raw_values(case)]
            self.assertTrue(any(0 < abs(value) < 1 for value in values), case["name"])
            self.assertTrue(any(value.denominator > 1 for value in values), case["name"])
            self.assertTrue(any(abs(value) > 1 << 32 for value in values), case["name"])

    def test_emitted_rust_cast_and_matched_c_range_guard_use_all_sixteen_cases(self):
        c_source = (self.generated / "kernels.c").read_text()
        rust_source = (self.generated / "kernels.rs").read_text()
        self.assertEqual(c_source.count("double value=(double)aq_from_f"), 16)
        self.assertEqual(rust_source.count(" as i64) as u128"), 8)
        self.assertEqual(rust_source.count(" as u64) as u128"), 8)
        self.assertEqual(c_source.count("if(value!=value)v=0"), 16)
        self.assertIn("else if(value<=-0x1p63)v=INT64_MIN", c_source)
        self.assertIn("else if(value>=0x1p63)v=INT64_MAX", c_source)
        self.assertIn("else if(value<=0)v=0", c_source)
        self.assertIn("else if(value>=0x1p64)v=UINT64_MAX", c_source)

    def test_generator_restores_catalog_and_vector_providers_after_failure(self):
        original_catalog = generate.catalog
        original_vector = conversions.vector
        with patch.object(generate, "generate", side_effect=ValueError("fixture")):
            with self.assertRaisesRegex(ValueError, "fixture"):
                conversion_ranges.generate_corpus(self.generated)
        self.assertIs(generate.catalog, original_catalog)
        self.assertIs(conversions.vector, original_vector)


if __name__ == "__main__":
    unittest.main()
