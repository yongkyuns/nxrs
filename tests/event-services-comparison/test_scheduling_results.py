"""Consistency checks for the dated, sanitized hardware evidence."""
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent


class SchedulingEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (HERE / "results/esp32s3-scheduling-2026-10-04.json").read_text()
        cls.report = json.loads(cls.text)

    def test_complete_matched_matrix_and_delivery_accounting(self):
        report = self.report
        self.assertEqual(report["kind"], "event-services-scheduling-report")
        self.assertEqual((report["blocks"], report["runs_per_profile"]), (2, 2))
        self.assertEqual(set(report["cases"]), {
            "nuttx-c-three", "nuttx-rust-three", "zephyr-c-three",
            "embassy-three-event", "embassy-three-natural",
            "embassy-three-budget", "embassy-three-chunked",
        })
        source_maps, count, attempts = [], 0, 0
        for case in report["cases"].values():
            source_maps.append(case["source_sha256"])
            self.assertEqual(case["blocks"], [0, 1])
            self.assertEqual(set(case["profiles"]), {
                "normal", "burst", "work-short", "work-medium", "work-long", "io-wait",
            })
            for profile, observations in case["profiles"].items():
                self.assertEqual(len(observations["runs"]), 4)
                for row in observations["runs"]:
                    self.assertEqual(row["attempted"], 6240 if profile == "burst" else 3900)
                    self.assertEqual(row["accepted"], row["received"])
                    self.assertEqual(row["accepted"] + row["rejected"], row["attempted"])
                    self.assertEqual(row["protocol_errors"], 0)
                    if profile != "work-long":
                        self.assertEqual((row["rejected"], row["missed"]), (0, 0))
                    count += 1
                    attempts += row["attempted"]
        self.assertTrue(all(source == source_maps[0] for source in source_maps))
        self.assertEqual((count, attempts), (168, 720720))

    def test_natural_io_and_light_work_qualify_without_explicit_handoffs(self):
        natural = self.report["cases"]["embassy-three-natural"]
        for profile, observations in natural["profiles"].items():
            for row in observations["runs"]:
                self.assertEqual(row["yields"], 0)
                if profile != "work-long":
                    self.assertEqual((row["rejected"], row["missed"]), (0, 0))
                if profile == "io-wait":
                    self.assertEqual(row["io_jobs"], 117)
                    self.assertGreaterEqual(row["io_p99_us"], 3000)
        for case in self.report["cases"].values():
            for row in case["profiles"]["io-wait"]["runs"]:
                self.assertEqual((row["rejected"], row["missed"], row["io_jobs"]), (0, 0, 117))

    def test_public_evidence_has_no_raw_transcripts_or_private_paths(self):
        self.assertNotIn('"raw_output"', self.text)
        for private in ("/Users/", "/home/", "usbmodem", "device-before.bin"):
            self.assertNotIn(private, self.text)


if __name__ == "__main__":
    unittest.main()
