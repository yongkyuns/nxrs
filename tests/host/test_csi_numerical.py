"""Finite-input regression candidates; actual vendor results require the emulator."""

import importlib.util
import math
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "numerical_oracle", ROOT / "tests/csi-radio/numerical.py"
)
NUM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NUM)
spec = importlib.util.spec_from_file_location(
    "numerical_qemu", ROOT / "tests/csi-radio/qemu.py"
)
QEMU = importlib.util.module_from_spec(spec)
spec.loader.exec_module(QEMU)


class NumericalFailureTests(unittest.TestCase):
    def test_candidate_is_positive_finite_and_matches_device_shape(self):
        data = NUM.sample(785)
        self.assertEqual((len(data), len(data[0])), (10, 13))
        self.assertTrue(
            all(math.isfinite(x) and 10 <= x <= 120 for row in data for x in row)
        )

    def test_float32_absolute_tolerance_can_cycle_on_valid_data(self):
        result = NUM.power(NUM.sample(785))
        self.assertEqual((result["status"], result["iterations"]), (-1, 30))
        self.assertEqual(result["delta"], 0.0001220703125)
        self.assertGreater(result["delta"], NUM.DELTA)
        self.assertEqual(result["tail"][-4:], result["tail"][-2:] * 2)

    def test_more_iterations_do_not_remove_the_rounding_cycle(self):
        result = NUM.power(NUM.sample(785), limit=300)
        self.assertEqual((result["status"], result["iterations"]), (-1, 300))
        self.assertEqual(result["delta"], 0.0001220703125)

    def test_scaling_preserves_reference_direction_but_changes_native_policy(self):
        data = NUM.sample(785)
        scaled = [[x / 16 for x in row] for row in data]
        result = NUM.power(scaled)
        self.assertEqual(result["status"], 0)
        self.assertLess(result["iterations"], 30)
        original_ref, scaled_ref = NUM.reference(data), NUM.reference(scaled)
        for a, b in zip(original_ref["vector"], scaled_ref["vector"]):
            self.assertAlmostEqual(a, b, places=8)
        for a, b in zip(original_ref["projection"], scaled_ref["projection"]):
            self.assertAlmostEqual(a / 16, b, places=8)
        for actual, reference in zip(result["vector"], scaled_ref["vector"]):
            self.assertAlmostEqual(actual, reference, places=6)

    def test_large_scale_is_not_always_a_failure(self):
        self.assertEqual(NUM.power(NUM.sample(1))["status"], 0)


class NumericalRunnerTests(unittest.TestCase):
    @staticmethod
    def transcript():
        lines = ["CSI_PROBE_ENTERED"]
        for scale, status, iterations in (("1", -1, 30), ("1over16", 0, 5)):
            for rows in (10, 7, 3, 1):
                for repeat in range(3):
                    lines.extend(
                        [
                            f"iterate: {iterations}, itrs_max: 30",
                            f"CSI_NUMERIC_CASE scale={scale} split={rows},{10-rows} repeat={repeat} status={status} allocations=4 live=0 guards=0",
                        ]
                    )
        lines.extend(
            [
                "iterate: 6, itrs_max: 30",
                "CSI_NUMERICAL_REPORT checks=398 failures=0 guards=0 live=0",
            ]
        )
        return "\n".join(lines) + "\n"

    def test_complete_native_evidence_is_accepted(self):
        QEMU.verify_numerical(self.transcript(), "CSI_NSH_STATUS_0\n")

    def test_missing_forged_or_incomplete_native_evidence_is_rejected(self):
        valid = self.transcript()
        mutations = [
            "",
            valid + valid,
            valid.replace("checks=398", "checks=397"),
            valid.replace("failures=0", "failures=1"),
            valid.replace("status=-1", "status=0", 1),
            valid.replace("allocations=4", "allocations=3", 1),
            valid.replace("live=0", "live=1", 1),
            valid.replace("guards=0", "guards=1", 1),
            valid.replace("split=7,3", "split=7,2", 1),
            valid.replace("repeat=2", "repeat=1", 1),
            valid.replace("iterate: 30, itrs_max: 30\n", "", 1),
            valid.replace("iterate: 30, itrs_max: 30", "iterate: 29, itrs_max: 30", 1),
            valid.replace("iterate: 5, itrs_max: 30", "iterate: 30, itrs_max: 30", 1),
            valid + "iterate: 5, itrs_max: 30\n",
            valid + "CSI_CHECK_FAIL output oracle\n",
            valid + "PANIC\n",
        ]
        for index, result in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                QEMU.verify_numerical(result, "CSI_NSH_STATUS_0\n")
        for status in (
            "",
            "CSI_NSH_STATUS_1\n",
            "CSI_NSH_STATUS_0\nCSI_NSH_STATUS_0\n",
        ):
            with self.subTest(status=status), self.assertRaises(ValueError):
                QEMU.verify_numerical(valid, status)


if __name__ == "__main__":
    unittest.main()
