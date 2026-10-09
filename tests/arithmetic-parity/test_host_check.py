"""Negative controls for host-only false passes found by independent review."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import host_check


F32 = dict(name="f32_test", kind="float", bits=32, float_bits=32, ulp_limit=0)


class HostVerifierTests(unittest.TestCase):
    def test_f32_rejects_upper_bits_even_for_nan_and_relaxed_zero(self):
        for raw in (0xbf800000, 0x7fc00000, 0):
            for relaxed in (False, True):
                case = {**F32, "zero_sign_required": not relaxed}
                with self.subTest(raw=raw, relaxed=relaxed):
                    expected = (raw, 0, 0, 0)
                    self.assertTrue(host_check.matches(expected, expected, case))
                    corrupt = (raw | 0xffffffff00000000, 0, 0, 0)
                    self.assertFalse(host_check.matches(corrupt, expected, case))

    def test_result_counts_must_match_before_comparing_values(self):
        value = (0, 0, 0, 0)
        case = dict(name="u8_test", kind="integer", bits=8, float_bits=0)
        self.assertEqual(host_check.case_failures(case, [value], [value]), [])
        for actual, expected in (([value, value], [value]), ([value], [value, value])):
            with self.subTest(actual_count=len(actual), expected_count=len(expected)):
                with self.assertRaisesRegex(ValueError, "result count"):
                    host_check.case_failures(case, actual, expected)
        self.assertEqual(host_check.case_failures(case, [(1, 0, 0, 0)], [value]),
                         [(0, (1, 0, 0, 0), value)])

    def test_short_manifest_is_rejected_before_compiling(self):
        for input_count, expected_count in ((64, 63), (63, 64), (63, 63)):
            with self.subTest(input_count=input_count, expected_count=expected_count):
                with tempfile.TemporaryDirectory(prefix="aq-host-") as folder:
                    root = Path(folder)
                    proof = dict(samples_per_case=64, cases=[dict(
                        name="u8_test", inputs=[[0]*6]*input_count,
                        expected=[[0]*4]*expected_count)])
                    (root/"coverage.json").write_text(json.dumps(proof))
                    with patch.object(host_check.subprocess, "run") as compiler:
                        with self.assertRaisesRegex(ValueError, "vector count"):
                            host_check.verify(root)
                        compiler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
