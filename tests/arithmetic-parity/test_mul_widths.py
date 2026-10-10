"""Coverage and ABI checks for the stratified checked-i64 multiply corpus."""
from pathlib import Path
import json
import statistics
import tempfile
import unittest
from unittest.mock import patch

import build
import integers
import measure
import mul_widths


def signed(raw):
    return integers.signed_value(raw, 64)


def near(value, edges, distance=4):
    return min(abs(value - edge) for edge in edges) <= distance


class MultiplyWidthCorpusTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="aq-mul-widths-")
        self.generated = Path(self.folder.name) / "generated"
        self.proof = mul_widths.generate_corpus(self.generated)

    def tearDown(self):
        self.folder.cleanup()

    def test_manifest_is_accepted_by_existing_build_and_measure_validators(self):
        self.assertEqual(len(build.validate_generated(self.generated)["cases"]), len(mul_widths.GROUPS))
        self.assertEqual(len(measure._manifest(self.proof)), len(mul_widths.GROUPS))
        self.assertEqual(self.proof["samples_per_case"], 64)
        self.assertIn("mul_widths.py", self.proof["sources"])

    def test_generator_restores_providers_if_emission_fails(self):
        original_catalog = mul_widths.generate.catalog
        original_vector = integers.vector
        with patch.object(mul_widths.generate, "generate", side_effect=ValueError("fixture")):
            with self.assertRaisesRegex(ValueError, "fixture"):
                mul_widths.generate_corpus(self.generated)
        self.assertIs(mul_widths.generate.catalog, original_catalog)
        self.assertIs(integers.vector, original_vector)

    def test_groups_are_stratified_by_actual_operands_and_exact_product(self):
        cases = self.proof["cases"]
        self.assertEqual([case["mul_width_group"] for case in cases], list(mul_widths.GROUPS))
        for case in cases:
            group = case["mul_width_group"]
            self.assertEqual(len(case["inputs"]), 64, group)
            self.assertEqual(len(case["expected"]), 64, group)
            for inputs, actual_expected in zip(case["inputs"], case["expected"]):
                a, b = signed(inputs[0]), signed(inputs[1])
                product = a * b
                fits32 = mul_widths.I32_MIN <= a <= mul_widths.I32_MAX
                fits32_b = mul_widths.I32_MIN <= b <= mul_widths.I32_MAX
                fits64_product = mul_widths.I64_MIN <= product <= mul_widths.I64_MAX
                self.assertEqual(actual_expected, list(integers.expected(case, inputs)), (group, a, b))
                if group == "both_signed32fit":
                    self.assertTrue(fits32 and fits32_b, (a, b))
                elif group == "only_a_signed32fit":
                    self.assertTrue(fits32 and not fits32_b, (a, b))
                elif group == "only_b_signed32fit":
                    self.assertTrue(not fits32 and fits32_b, (a, b))
                elif group == "both_wide_product_fits":
                    self.assertTrue(not fits32 and not fits32_b and fits64_product, (a, b, product))
                elif group == "both_wide_product_overflows":
                    self.assertTrue(not fits32 and not fits32_b and not fits64_product, (a, b, product))
                elif group == "near_32_boundary":
                    edges = (mul_widths.I32_MIN, mul_widths.I32_MAX + 1)
                    self.assertTrue(near(a, edges) and near(b, edges), (a, b))
                elif group == "near_64_boundary":
                    edges = (mul_widths.I64_MIN, mul_widths.I64_MAX)
                    self.assertTrue(near(a, edges, 3), (a, b))
                elif group == "mixed_randomized_widths":
                    self.assertTrue(mul_widths.I64_MIN <= a <= mul_widths.I64_MAX)
                    self.assertTrue(mul_widths.I64_MIN <= b <= mul_widths.I64_MAX)
        mixed = next(case for case in cases if case["mul_width_group"] == "mixed_randomized_widths")
        width_pairs = {(abs(signed(row[0])).bit_length(), abs(signed(row[1])).bit_length())
                       for row in mixed["inputs"]}
        self.assertGreaterEqual(len(width_pairs), 32)

    def test_explicit_corner_vectors_and_deterministic_generation(self):
        by_group = {case["mul_width_group"]: case for case in self.proof["cases"]}
        near64 = by_group["near_64_boundary"]["inputs"]
        pairs = {(signed(row[0]), signed(row[1])) for row in near64}
        self.assertIn((mul_widths.I64_MIN, -1), pairs)
        self.assertIn((mul_widths.I64_MIN, 0), pairs)
        self.assertIn((mul_widths.I64_MAX, 1), pairs)
        self.assertIn((0, 0), {(signed(row[0]), signed(row[1]))
                               for row in by_group["both_signed32fit"]["inputs"]})
        self.assertEqual(mul_widths.vector(by_group["mixed_randomized_widths"], 17),
                         mul_widths.vector(by_group["mixed_randomized_widths"], 17))

    def test_emitted_kernels_keep_inputs_runtime_and_use_ordinary_checked_multiply(self):
        c_source = (self.generated / "kernels.c").read_text()
        r_source = (self.generated / "kernels.rs").read_text()
        self.assertEqual(c_source.count("__builtin_mul_overflow(a,b,&value)"), len(mul_widths.GROUPS))
        self.assertEqual(r_source.count("a.checked_mul(b)"), len(mul_widths.GROUPS))
        self.assertIn("const struct aq_input *p=&inputs[i]", c_source)
        self.assertIn("let a=((p.a as u128)) as i64", r_source)
        self.assertTrue(all(case["kind"] == "integer" and case["op"] == "mul" and
                            case["mode"] == "checked" and case["bits"] == 64 and
                            case["c_available"] for case in self.proof["cases"]))


class MultiplyWidthCompilerEvidenceTests(unittest.TestCase):
    @staticmethod
    def masked_us_per_input(evidence, case, backend):
        repeats = (value for run in case["cycles"]["masked"][backend] for value in run)
        median_cycles = statistics.median(repeats)
        return median_cycles / evidence["cpu_mhz"] / evidence["samples_per_case"]

    def test_focused_width_reports_bind_catalog_and_recompute_cycle_summaries(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        pair = (
            json.loads((reports / "esp32s3-mul-width-control-2026-10-06.json").read_text()),
            json.loads((reports / "esp32s3-mul-width-candidate-2026-10-06.json").read_text()),
        )
        expected_names = [case["name"] for case in mul_widths.catalog()]
        for evidence in pair:
            self.assertTrue(evidence["passed"])
            self.assertTrue(evidence["restoration_verified"])
            self.assertEqual(evidence["cpu_mhz"], 240)
            self.assertEqual(evidence["samples_per_case"], 64)
            self.assertEqual(evidence["repeats_per_mode_per_run"], 5)
            self.assertEqual(evidence["runs"], 3)
            self.assertEqual([case["name"] for case in evidence["cases"]], expected_names)
            self.assertEqual(evidence["totals"]["raw_pair_differences"], 0)
            for case in evidence["cases"]:
                self.assertEqual(case["kind"], "integer")
                self.assertEqual(case["bits"], 64)
                self.assertTrue(case["c_available"])
                for backend in ("c", "rust"):
                    batches = case["cycles"]["masked"][backend]
                    self.assertEqual(len(batches), evidence["runs"])
                    self.assertTrue(all(len(batch) == evidence["repeats_per_mode_per_run"]
                                        for batch in batches))
                    median_cycles = statistics.median(value for batch in batches for value in batch)
                    self.assertEqual(case["masked_median_cycles"][backend], median_cycles)
                    self.assertAlmostEqual(
                        median_cycles / evidence["cpu_mhz"] / evidence["samples_per_case"],
                        self.masked_us_per_input(evidence, case, backend), places=12)
                self.assertAlmostEqual(
                    case["rust_c_ratio"],
                    case["masked_median_cycles"]["rust"] / case["masked_median_cycles"]["c"],
                    places=12)

    def test_qualification_binds_the_preserved_chain_and_actual_driver(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        evidence = json.loads((reports / "compiler-mul-width-2026-10-06.json").read_text())
        parent = json.loads((reports / "compiler-mixed-mul-2026-10-06.json").read_text())
        self.assertEqual(evidence["patches"][:-1], parent["patches"])
        self.assertEqual(len(evidence["patches"]), 19)
        for index, row in enumerate(evidence["patches"]):
            folder = "patches" if index < 6 else "proposals"
            self.assertEqual(build.digest(root / "upstream/rust-llvm" / folder / row["name"]), row["sha256"])
        self.assertEqual(evidence["suite_results"], {
            "xtensa": {"PASS": 115},
            "optimizer": {"PASS": 409, "UNSUPPORTED": 50, "XFAIL": 3},
            "x86": {"PASS": 5}})
        self.assertEqual(evidence["public_source_verification"], {"files": 59, "exact_match": True})
        host = evidence["host_execution"]
        self.assertEqual(host["architecture"], "x86_64")
        self.assertEqual(host["transformed_by"], "Xtensa automatic runtime-width pass")
        self.assertRegex(host["transformed_ir_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(host["limitation"],
                         "Host execution checks transformed IR, not Xtensa machine instructions or speed")
        self.assertEqual(host["dynamic_pairs"], 40441)
        self.assertEqual(host["sequential_pairs"], 40441)
        self.assertEqual(host["mismatches"], 0)
        verifier = evidence["full_corpus_machine_verifier"]
        self.assertEqual(verifier["cases"], 1136)
        self.assertTrue(verifier["passed"])
        self.assertRegex(verifier["input_ir_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(verifier["limitation"],
                         "Native backend object verification, not a new full-corpus device execution")
        self.assertIn("Host IR execution is not Xtensa instruction execution or timing",
                      evidence["limitations"])
        self.assertIn("No production FP/preemption/interrupt qualification is added",
                      evidence["limitations"])
        control = json.loads((reports / "esp32s3-mul-width-control-2026-10-06.json").read_text())
        self.assertNotEqual({str(Path("lib") / name): sha for name, sha in
                            evidence["driver_libraries_sha256"].items()},
                           control["build"]["rust_driver_libraries_sha256"])
        self.assertNotIn("/Users/", json.dumps(evidence))
        self.assertNotIn("/home/", json.dumps(evidence))


if __name__ == "__main__":
    unittest.main()
