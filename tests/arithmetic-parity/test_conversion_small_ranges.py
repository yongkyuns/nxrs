"""Independent correctness and emitter checks for small cast ranges."""
import hashlib
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build
import conversion_small_ranges as small
import conversions
import floating
import generate


def independent_cast(case, raw):
    """Compute saturating casts using exact Fractions, independently."""
    kind, value, sign = floating.classify(raw, case["source_bits"])
    width = case["bits"]
    lower, upper = ((-(1 << (width - 1)), (1 << (width - 1)) - 1)
                    if case["signed"] else (0, (1 << width) - 1))
    if kind == "nan":
        result = 0
    elif kind == "inf":
        result = lower if sign else upper
    else:
        magnitude = abs(value.numerator) // value.denominator
        result = -magnitude if value < 0 else magnitude
        result = min(upper, max(lower, result))
    return result & ((1 << width) - 1), 0, 0, 0


class SmallConversionRangeCorpusTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="aq-small-casts-")
        self.generated = Path(self.folder.name) / "generated"
        self.proof = small.generate_corpus(self.generated)

    def tearDown(self):
        self.folder.cleanup()

    def case(self, name):
        return next(row for row in self.proof["cases"] if row["name"] == name)

    def test_48_cases_64_inputs_each_and_masks_per_target_width(self):
        self.assertEqual(len(self.proof["cases"]), 48)
        self.assertEqual(self.proof["samples_per_case"], 64)
        self.assertEqual(len({case["name"] for case in self.proof["cases"]}), 48)
        for width in (8, 16, 32):
            cases = [row for row in self.proof["cases"] if row["bits"] == width]
            self.assertEqual(len(cases), 16)
            mask = (1 << width) - 1
            self.assertTrue(all(value[0] <= mask for case in cases
                                for value in case["expected"]))
            self.assertTrue(all(value[1] == 0 for case in cases
                                for value in case["expected"]))

    def test_common_build_contract_and_source_hashes(self):
        validated = build.validate_generated(self.generated)
        self.assertEqual(len(validated["cases"]), 48)
        self.assertEqual(validated["samples_per_case"], 64)
        for name in ("conversion_small_ranges.py", "conversion_ranges.py",
                     "conversions.py", "floating.py"):
            digest = hashlib.sha256((small.HERE / name).read_bytes()).hexdigest()
            self.assertEqual(self.proof["sources"][name], digest)

    def test_cases_are_deterministic_pure_and_match_independent_fraction_oracle(self):
        second_folder = tempfile.TemporaryDirectory(prefix="aq-small-casts-repeat-")
        try:
            second = small.generate_corpus(Path(second_folder.name) / "generated")
            self.assertEqual([row["inputs"] for row in self.proof["cases"]],
                             [row["inputs"] for row in second["cases"]])
        finally:
            second_folder.cleanup()

        for case in self.proof["cases"]:
            width = case["bits"]
            lower = -(1 << (width - 1)) if case["signed"] else 0
            upper_exclusive = 1 << (width - 1 if case["signed"] else width)
            self.assertEqual(len(case["inputs"]), 64)
            for vector, expected in zip(case["inputs"], case["expected"]):
                kind, value, sign = floating.classify(vector[0], case["source_bits"])
                group = case["conversion_range_group"]
                if group == "in_range_finite":
                    self.assertEqual(kind, "finite", (case["name"], vector[0]))
                    self.assertGreaterEqual(value, lower)
                    self.assertLess(value, upper_exclusive)
                elif group == "low_negative_overflow":
                    self.assertTrue((kind == "inf" and sign == 1) or
                                    (kind == "finite" and value < lower))
                elif group == "high_overflow":
                    self.assertTrue((kind == "inf" and sign == 0) or
                                    (kind == "finite" and value >= upper_exclusive))
                else:
                    self.assertEqual((group, kind), ("nan", "nan"))
                self.assertEqual(expected, list(independent_cast(case, vector[0])))

    def test_negative_zero_and_subnormals_keep_group_classification(self):
        for source in (32, 64):
            sign = 1 << (source - 1)
            for target in ("i8", "u8", "i16", "u16", "i32", "u32"):
                in_range = self.case(f"f{source}_as_{target}_range_in_range_finite")
                low = self.case(f"f{source}_as_{target}_range_low_negative_overflow")
                in_raw = [row[0] for row in in_range["inputs"]]
                low_raw = [row[0] for row in low["inputs"]]
                self.assertIn(sign, in_raw)
                self.assertIn(0, in_raw)
                if target.startswith("u"):
                    self.assertIn(sign | 1, low_raw)
                self.assertTrue(all(floating.classify(raw, source)[0] in ("inf", "finite")
                                    and (floating.classify(raw, source)[0] == "inf"
                                         or floating.classify(raw, source)[1] < 0)
                                    for raw in low_raw))

    def test_exact_small_maxima_and_fractional_values_below_upper_exclusive(self):
        for source in (32, 64):
            for width in (8, 16):
                for signed, target in ((True, f"i{width}"), (False, f"u{width}")):
                    case = self.case(f"f{source}_as_{target}_range_in_range_finite")
                    values = [floating.classify(row[0], source)[1] for row in case["inputs"]]
                    maximum = (1 << (width - 1)) - 1 if signed else (1 << width) - 1
                    self.assertIn(Fraction(maximum), values)
                    self.assertIn(Fraction(2 * maximum + 1, 2), values)
                    self.assertEqual(independent_cast(case,
                                     next(row[0] for row in case["inputs"]
                                          if floating.classify(row[0], source)[1]
                                          == Fraction(2 * maximum + 1, 2)))[0], maximum)

    def test_signed_bounds_and_truncation_near_minimum(self):
        for source in (32, 64):
            for width in (8, 16, 32):
                case = self.case(f"f{source}_as_i{width}_range_in_range_finite")
                low = self.case(f"f{source}_as_i{width}_range_low_negative_overflow")
                in_values = [floating.classify(row[0], source)[1] for row in case["inputs"]]
                low_values = [floating.classify(row[0], source)[1]
                              for row in low["inputs"] if floating.classify(row[0], source)[0] == "finite"]
                minimum = -(1 << (width - 1))
                self.assertIn(Fraction(minimum), in_values)
                self.assertTrue(any(value < minimum for value in low_values))
                near_min_raw = small.conversion_ranges._encode(Fraction(minimum) + Fraction(1, 2), source)
                near_min = floating.classify(near_min_raw, source)[1]
                truncated = abs(near_min.numerator) // near_min.denominator
                truncated = -truncated if near_min < 0 else truncated
                self.assertEqual(independent_cast(case, near_min_raw)[0],
                                 truncated & ((1 << width) - 1))

    def test_emitter_keeps_ordinary_casts_and_c_guard_for_all_cases(self):
        c_source = (self.generated / "kernels.c").read_text()
        rust_source = (self.generated / "kernels.rs").read_text()
        self.assertEqual(c_source.count("double value=(double)aq_from_f"), 48)
        self.assertEqual(c_source.count("if(value!=value)v=0"), 48)
        for width in (8, 16, 32):
            self.assertEqual(rust_source.count(f" as i{width}) as u128"), 8)
            self.assertEqual(rust_source.count(f" as u{width}) as u128"), 8)
            self.assertIn(f"INT{width}_MIN", c_source)
            self.assertIn(f"UINT{width}_MAX", c_source)

    def test_generator_restores_catalog_and_vector_even_after_failure(self):
        original_catalog, original_vector = generate.catalog, conversions.vector
        with patch.object(generate, "generate", side_effect=ValueError("fixture")):
            with self.assertRaisesRegex(ValueError, "fixture"):
                small.generate_corpus(self.generated)
        self.assertIs(generate.catalog, original_catalog)
        self.assertIs(conversions.vector, original_vector)


if __name__ == "__main__":
    unittest.main()
