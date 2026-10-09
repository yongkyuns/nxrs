import json
import math
import tempfile
import unittest
from pathlib import Path

import latency_guard


def report(means, services=3):
    return {
        "schema": 1, "target": "ESP32-S3 / NuttX", "source": "messages",
        "period_us": 2000, "cycles_per_us": 240,
        "builds": {"c": {"config_identity": "config", "kernel_archive_inventory_sha256": "kernel"}},
        "runs": [
            {"matrix": i, "block": 0, "language": "c", "services": services, "events": 1000,
             "received": 1000, "mean_cycles": us * 240}
            for i, us in enumerate(means)
        ],
    }


class LatencyGuardTests(unittest.TestCase):
    def test_floor_applies_and_candidate_subset_is_reported(self):
        baseline = report([4, 4, 4])
        baseline["runs"] += [dict(row, services=20) for row in report([10, 10, 10], 20)["runs"]]
        candidate = report([9.1, 9.1, 9.1])
        result = latency_guard.compare(baseline, candidate)
        self.assertEqual(result["compared_cells"], 1)
        self.assertEqual(result["cells"][0]["allowed_added_us"], 5.0)
        self.assertFalse(result["pass"])

    def test_default_relative_limit_and_pass_boundary(self):
        base, candidate = report([100, 100, 100]), report([120, 120, 120])
        result = latency_guard.compare(base, candidate)
        self.assertEqual(result["cells"][0]["allowed_added_us"], 20)
        self.assertTrue(result["pass"])
        candidate["runs"][0]["mean_cycles"] = 120.1 * 240
        candidate["runs"][1]["mean_cycles"] = 120.1 * 240
        candidate["runs"][2]["mean_cycles"] = 120.1 * 240
        self.assertFalse(latency_guard.compare(base, candidate)["pass"])

    def test_missing_cell_too_few_samples_and_duplicate_identity_rejected(self):
        with self.assertRaisesRegex(ValueError, "no baseline"):
            latency_guard.compare(report([4, 4, 4], 20), report([4, 4, 4]))
        with self.assertRaisesRegex(ValueError, "at least 3"):
            latency_guard.compare(report([4, 4]), report([4, 4, 4]))
        duplicate = report([4, 4, 4])
        duplicate["runs"][2]["matrix"] = 1
        with self.assertRaisesRegex(ValueError, "duplicate"):
            latency_guard.compare(report([4, 4, 4]), duplicate)

    def test_nonfinite_mismatched_config_and_diagnostic_rejected(self):
        invalid = report([4, 4, 4])
        invalid["runs"][0]["mean_cycles"] = math.inf
        with self.assertRaisesRegex(ValueError, "finite"):
            latency_guard.compare(report([4, 4, 4]), invalid)
        for field in latency_guard.BUILD_ID:
            invalid = report([4, 4, 4])
            invalid["builds"]["c"][field] = "other"
            with self.assertRaisesRegex(ValueError, field):
                latency_guard.compare(report([4, 4, 4]), invalid)

    def test_diagnostic_and_failed_reports_rejected(self):
        for field, value in (("diagnostic_only", True), ("footprint_claim", False),
                             ("instrumentation", True), ("diagnostic_trace", True),
                             ("diagnostic_perfmon", True), ("diagnostic_hot_iram", True),
                             ("diagnostic_layout_padding_bytes", 0)):
            invalid = report([4, 4, 4])
            invalid[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "diagnostic"):
                latency_guard.compare(report([4, 4, 4]), invalid)
        invalid = report([4, 4, 4])
        invalid["runs"][0]["received"] = 999
        with self.assertRaisesRegex(ValueError, "failed"):
            latency_guard.compare(report([4, 4, 4]), invalid)
        for field, value in (("failure", "transport error"), ("restoration_verified", False)):
            invalid = report([4, 4, 4])
            invalid[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "failed/unrestored"):
                latency_guard.compare(report([4, 4, 4]), invalid)

    def test_cli_exit_status_and_json_output(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline, candidate = Path(directory) / "base.json", Path(directory) / "candidate.json"
            baseline.write_text(json.dumps(report([4, 4, 4])))
            candidate.write_text(json.dumps(report([10, 10, 10])))
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                status = latency_guard.main(["--baseline", str(baseline), "--candidate", str(candidate)])
            self.assertEqual(status, 1)
            self.assertFalse(json.loads(output.getvalue())["pass"])


if __name__ == "__main__":
    unittest.main()
