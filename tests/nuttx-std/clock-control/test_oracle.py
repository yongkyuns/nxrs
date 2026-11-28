#!/usr/bin/env python3
"""Recheck retained observations and reject incomplete or misleading evidence."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

import oracle

HERE = Path(__file__).resolve().parent


def records(text):
    return [json.loads(line[len(oracle.PREFIX):])
            for line in text.replace("\r", "").splitlines() if line.startswith(oracle.PREFIX)]


def transcript(items):
    return "\n".join(oracle.PREFIX + json.dumps(item) for item in items)


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = records((HERE / "results/baseline/captures/console-1-baseline.log").read_text())
        cls.software = records((HERE / "results/software-fine100us/captures/console-0-software.log").read_text())

    def test_all_retained_captures(self):
        count = 0
        for directory in sorted((HERE / "results").iterdir()):
            if not directory.is_dir():
                continue
            build = json.loads((directory / "build.json").read_text())
            analysis = json.loads((directory / "analysis.json").read_text())
            boots = json.loads((directory / "captures/results.json").read_text())
            self.assertEqual(analysis["build_sha256"], hashlib.sha256((directory / "build.json").read_bytes()).hexdigest())
            self.assertEqual(analysis["oracle_sha256"], hashlib.sha256((HERE / "oracle.py").read_bytes()).hexdigest())
            self.assertTrue(analysis["characterization_complete"])
            self.assertEqual(len(analysis["runs"]), len(boots))
            for source, sha in build["sources_after"].items():
                self.assertEqual(hashlib.sha256((HERE.parents[2] / source).read_bytes()).hexdigest(), sha)
            for artifact, sha in build["artifacts"].items():
                if artifact != build["image"]:  # Large ELF is identified, not committed.
                    self.assertEqual(hashlib.sha256((directory / artifact).read_bytes()).hexdigest(), sha)
            for path in sorted((directory / "captures").glob("console-*.log")):
                result = oracle.analyze(path.read_text(), build["mode"], build["tick_us"])
                index = int(path.name.split("-")[1])
                self.assertTrue(boots[index]["success"])
                self.assertEqual(boots[index]["image_sha256"], build["artifacts"][build["image"]])
                self.assertEqual(result, analysis["runs"][index])
                self.assertTrue(result["characterization_complete"])
                self.assertFalse(result["qualified"])
                count += 1
        self.assertEqual(count, 9)

    def test_retained_file_manifest(self):
        root = HERE / "results"
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertEqual(manifest["schema"], 1)
        actual = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in root.rglob("*") if p.is_file() and p.name != "manifest.json"}
        self.assertEqual(actual, manifest["files"])

    def test_independent_rate_is_not_normalized(self):
        result = oracle.analyze(transcript(self.baseline), "baseline", 1000)
        rate = result["phases"]["baseline"]["monotonic_rate"]
        self.assertTrue(.60 < rate < .65)
        self.assertFalse(result["gates"]["monotonic_rate_within_one_percent"])
        self.assertFalse(result["gates"]["sleep_reference_within_ten_percent"])

    def test_truncated_or_reordered_evidence(self):
        cases = [self.baseline[:-1], self.baseline[1:], self.baseline + [self.baseline[-1]],
                 [self.baseline[0], *self.baseline[2:]]]
        reordered = copy.deepcopy(self.baseline)
        first_summary = next(i for i, r in enumerate(reordered) if r.get("kind") == "set")
        reordered[first_summary - 1], reordered[first_summary] = reordered[first_summary], reordered[first_summary - 1]
        cases.append(reordered)
        for case in cases:
            with self.subTest(case=len(case)), self.assertRaises(ValueError):
                oracle.analyze(transcript(case), "baseline", 1000)

    def test_wrong_identity_or_corrupt_statistics(self):
        for field, value in (("tick_us", 100), ("reference_hz", 32_000_000),
                             ("rtc", True), ("timekeeping", True)):
            items = copy.deepcopy(self.baseline)
            items[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                oracle.analyze(transcript(items), "baseline", 1000)
        for field, value in (("reads", False), ("realtime_regressions", 999999999),
                             ("max_read_span_ns", 0)):
            items = copy.deepcopy(self.baseline)
            next(r for r in items if r.get("kind") == "phase")[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                oracle.analyze(transcript(items), "baseline", 1000)

    def test_forward_instant_jump_is_not_continuity(self):
        items = copy.deepcopy(self.baseline)
        started = False
        for item in items:
            started |= item.get("phase") == "forward" and item.get("point") == "after"
            if started and item.get("kind") == "sample":
                item["monotonic_ns"] += oracle.SECOND
        result = oracle.analyze(transcript(items), "baseline", 1000)
        self.assertTrue(result["gates"]["monotonic_non_decreasing"])
        self.assertFalse(result["gates"]["monotonic_stable_across_clock_controls"])

    def test_backward_adjustment_call_is_not_smooth(self):
        items = copy.deepcopy(self.baseline)
        next(r for r in items if r.get("phase") == "initial_query"
             and r.get("point") == "after")["realtime_ns"] -= oracle.SECOND
        result = oracle.analyze(transcript(items), "baseline", 1000)
        self.assertFalse(result["gates"]["no_unrequested_realtime_regression"])

    def test_dense_regression_cannot_be_hidden_by_counter(self):
        items = copy.deepcopy(self.software)
        phase = next(r for r in items if r.get("kind") == "phase"
                     and r.get("realtime_regressions", 0) > 0)
        phase["realtime_regressions"] = 0
        phase["min_realtime_delta_ns"] = 0
        with self.assertRaises(ValueError):
            oracle.analyze(transcript(items), "software", 100)


if __name__ == "__main__":
    unittest.main()
