#!/usr/bin/env python3
"""Host parser controls; synthetic records are not target qualification evidence."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import unittest

import oracle

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RESULTS = HERE / "results"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def observations():
    rows = [{"kind": "begin", "schema": 1, "features": 3, "tick_us": 100}]
    for name, delta in oracle.PHASES:
        rows.append(dict(kind="phase", name=name, request_us=delta,
                         reference_ns=4_500_000_000, monotonic_ns=4_500_000_000,
                         wall_ns=4_500_000_000 + delta * 1000, uncertainty_ns=220_000,
                         reads=45_000, realtime_regressions=0, monotonic_regressions=0,
                         min_realtime_delta_ns=0, max_read_span_ns=20_000, pending_us=0,
                         slew_discontinuities=0, max_slew_excess_ns=0,
                         max_slew_uncertainty_ns=240_050))
        for index, remaining in enumerate([1500, 1000, 500, 0]):
            remaining = 0 if delta == 0 else remaining if delta > 0 else -remaining
            rows.append(dict(kind="pending", phase=name, index=index,
                             elapsed_ns=(index + 1) * 1_000_000_000,
                             remaining_us=remaining, uncertainty_us=2))
    for delta in (2_500_123, -2_500_123):
        rows.append(dict(kind="large", request_us=delta, pending_us=delta, cancelled_us=delta))
    rows.append(dict(kind="set_clears_adjustment", pending_us=0))
    for name, step, action in oracle.WAITS:
        requested = 250_000_000 if step else 1_250_000_000
        reference = requested + 100_000
        correction = action if step else 600_047 if action > 0 else -600_047
        rows.append(dict(kind="wait", name=name, step=step, action=action,
                         requested_ns=requested, reference_ns=reference, monotonic_ns=reference,
                         wall_ns=reference + correction, uncertainty_ns=220_000,
                         action_offset_ns=50_000_000, action_completed_offset_ns=50_010_000,
                         request_offset_ns=0 if step else 50_005_000,
                         request_span_ns=10_000 if step else 4_000,
                         pending_elapsed_ns=0 if step else 1_200_115_000,
                         pending_uncertainty_us=0 if step else 2,
                         pending_us=0 if step else 1400 if action > 0 else -1400))
    rows.append(dict(kind="complete", schema=1, phases=6, waits=4, large=2,
                     reference_elapsed_ns=33_000_000_000))
    return rows


def transcript(rows):
    return "\n".join(oracle.PREFIX + json.dumps(row) for row in rows)


class OracleTests(unittest.TestCase):
    def test_complete_contract(self):
        result = oracle.analyze(transcript(observations()), 100)
        self.assertTrue(result["qualified"])
        self.assertEqual(len(result["pending"]), 24)
        self.assertEqual(result["waits"]["negative"]["expected_applied_ns"], -600_047)

    def test_inventory_and_types(self):
        rows = observations()
        bad = [rows[:-1], rows + [rows[-1]], rows[1:], [rows[0], rows[2], rows[1], *rows[3:]]]
        for values in bad:
            with self.subTest(inventory=len(values)), self.assertRaises(ValueError):
                oracle.analyze(transcript(values), 100)
        with self.assertRaises(ValueError):
            oracle.analyze(transcript(rows), 1000)

    def test_large_query_may_take_more_than_four_milliseconds(self):
        rows = observations()
        rows[31].update(pending_us=2_500_118, cancelled_us=2_500_117)
        rows[32].update(pending_us=-2_500_118, cancelled_us=-2_500_117)
        self.assertTrue(oracle.analyze(transcript(rows), 100)["qualified"])

    def test_uncertainty_cannot_hide_failure(self):
        changes = [(1, dict(monotonic_ns=0, wall_ns=0, uncertainty_ns=4_500_000_000,
                            max_read_span_ns=2_250_000_000)),
                   (36, dict(wall_ns=250_100_000, uncertainty_ns=3_000_000_000)),
                   (7, dict(remaining_us=2000, uncertainty_us=999))]
        for index, update in changes:
            rows = observations()
            rows[index].update(update)
            with self.subTest(index=index), self.assertRaises(ValueError):
                oracle.analyze(transcript(rows), 100)

    def test_reject_false_qualification(self):
        cases = [(0, "features", 1), (0, "schema", True), (1, "reads", True), (1, "reads", 2),
                 (1, "realtime_regressions", 1), (1, "monotonic_regressions", 1),
                 (1, "min_realtime_delta_ns", -1), (1, "monotonic_ns", 4_501_000_000),
                 (1, "slew_discontinuities", 1), (1, "max_slew_excess_ns", 1),
                 (1, "max_slew_uncertainty_ns", 500_000), (6, "wall_ns", 4_500_000_000),
                 (6, "name", "baseline"), (7, "remaining_us", 1700),
                 (7, "uncertainty_us", 1000), (7, "index", 1), (7, "elapsed_ns", 0),
                 (31, "pending_us", 500_123), (32, "cancelled_us", 2_500_123),
                 (33, "pending_us", 1), (34, "action_offset_ns", 0),
                 (34, "action_completed_offset_ns", 1_250_100_000),
                 (34, "request_span_ns", 100_000), (34, "pending_elapsed_ns", 0),
                 (34, "pending_us", 2000), (34, "wall_ns", 1_250_100_000),
                 (36, "reference_ns", 300_000_000), (36, "pending_us", 1),
                 (37, "wall_ns", 3_250_100_000), (38, "phases", 5)]
        for index, field, value in cases:
            rows = copy.deepcopy(observations())
            rows[index][field] = value
            with self.subTest(index=index, field=field), self.assertRaises(ValueError):
                oracle.analyze(transcript(rows), 100)


class RetainedEvidenceTests(unittest.TestCase):
    def test_exact_file_manifests(self):
        for directory in (RESULTS, RESULTS / "icount-control"):
            manifest_path = directory / "manifest.json"
            manifest = read_json(manifest_path)
            self.assertEqual(manifest["schema"], 1)
            actual = {str(path.relative_to(directory)): sha(path) for path in directory.rglob("*")
                      if path.is_file() and path != manifest_path}
            self.assertEqual(actual, manifest["files"])
        manifest = read_json(RESULTS / "manifest.json")
        self.assertEqual(len(manifest["files"]), 35)
        build = read_json(RESULTS / "public-100us/build.json")
        self.assertEqual(manifest["omitted"],
                         {"public-100us/" + build["image"]: build["artifacts"][build["image"]]})
        for name in manifest["omitted"]:
            self.assertFalse((RESULTS / name).exists())

    def test_public_build_and_three_fresh_boots(self):
        directory = RESULTS / "public-100us"
        build = read_json(directory / "build.json")
        analysis = read_json(directory / "analysis.json")
        boots = read_json(directory / "captures/results.json")
        self.assertEqual(build["schema"], 1)
        self.assertIs(build["build_complete"], True)
        self.assertEqual(build["sources_before"], build["sources_after"])
        self.assertTrue(build["sources_after"])
        for source, digest in build["sources_after"].items():
            self.assertEqual(sha(ROOT / source), digest)
        self.assertEqual(analysis["build_sha256"], sha(directory / "build.json"))
        self.assertEqual(analysis["oracle_sha256"], sha(HERE / "oracle.py"))
        self.assertEqual(analysis["workflow_sha256"], sha(HERE / "workflow.py"))
        self.assertEqual(build["workflow_sha256"], analysis["workflow_sha256"])
        self.assertEqual(analysis["harness_sha256"], sha(HERE.parent / "run.py"))
        self.assertEqual(analysis["artifacts"], build["artifacts"])
        self.assertEqual(len(build["artifacts"]), 8)
        for name, digest in build["artifacts"].items():
            if name != build["image"]:
                self.assertEqual(sha(directory / name), digest)
        config = directory / "firmware/nuttx/.config"
        lines = set(config.read_text().splitlines())
        self.assertLessEqual({"CONFIG_ARCH_BOARD_MPS2_AN521=y", "CONFIG_TIMER_ARCH=y",
                              "CONFIG_CLOCK_TIMEKEEPING=y", "CONFIG_CLOCK_ADJTIME=y",
                              "CONFIG_USEC_PER_TICK=100", "CONFIG_CLOCK_ADJTIME_SLEWLIMIT_PPM=20000",
                              "CONFIG_CLOCK_ADJTIME_PERIOD_MS=970"}, lines)
        self.assertFalse({"CONFIG_RTC=y", "CONFIG_RTC_HIRES=y"} & lines)
        abi = read_json(directory / "firmware/abi-report.json")
        self.assertIs(abi["probe_abi_compatible"], True)
        self.assertEqual(abi["errors"], [])
        self.assertTrue(abi["checks"])
        self.assertEqual(abi["target"], "thumbv8m.main-nuttx-eabi")
        self.assertEqual(abi["config_sha256"], sha(config))
        ledger = read_json(directory / "firmware/nuttx-patches.json")
        self.assertEqual(ledger["upstream_revision"], build["identities"]["nuttx"])
        patch_hashes = {entry["name"]: entry["sha256"] for entry in ledger["patches"]}
        self.assertEqual(len(patch_hashes), len(ledger["patches"]))
        for source, digest in build["sources_after"].items():
            if source.startswith("upstream/nuttx/patches/"):
                self.assertEqual(patch_hashes[Path(source).name], digest)
        self.assertEqual(build["tick_us"], analysis["tick_us"])
        self.assertEqual(analysis["tick_us"], 100)
        self.assertIs(analysis["characterization_complete"], True)
        self.assertIs(analysis["qualified"], True)
        self.assertIs(analysis["physical_rtc_retention_qualified"], False)
        self.assertEqual(analysis["qemu_clock_options"], ["-icount", "shift=3,sleep=off"])
        self.assertEqual(len(boots), 3)
        self.assertEqual(len(analysis["runs"]), 3)
        logs = sorted((directory / "captures").glob("console-*.log"))
        self.assertEqual([path.name for path in logs], [f"console-{i}-public.log" for i in range(3)])
        for index, path in enumerate(logs):
            text = path.read_text()
            self.assertEqual(re.findall(r"^NXRS_STATUS_([0-9]+)\s*$", text.replace("\r", ""), re.M), ["0"])
            self.assertIs(boots[index]["success"], True)
            self.assertEqual(boots[index]["mode"], "public")
            self.assertEqual(boots[index]["image_sha256"], build["artifacts"][build["image"]])
            self.assertEqual(boots[index]["command"][-2:], analysis["qemu_clock_options"])
            self.assertIn(build["identities"]["nuttx"][:12], boots[index]["uname"])
            self.assertEqual(oracle.analyze(text, build["tick_us"]), analysis["runs"][index])

    def test_unchanged_phase1_controls_replay(self):
        phase1 = HERE.parent / "clock-control"
        spec = importlib.util.spec_from_file_location("phase1_retained_oracle", phase1 / "oracle.py")
        previous = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(previous)
        directory = RESULTS / "icount-control"
        summary = read_json(directory / "summary.json")
        self.assertEqual(set(summary["controls"]),
                         {"software-corrected", "software-fine100us", "software-fine100us-shift3"})
        for name, control in summary["controls"].items():
            base = directory / name
            analysis = read_json(base / "analysis.json")
            original = phase1 / "results" / analysis["variant"]
            build = read_json(original / "build.json")
            self.assertEqual(control["analysis_sha256"], sha(base / "analysis.json"))
            self.assertEqual(analysis["oracle_sha256"], sha(phase1 / "oracle.py"))
            self.assertEqual(analysis["build_sha256"], sha(original / "build.json"))
            self.assertEqual(analysis["phase1_analysis_sha256"], sha(original / "analysis.json"))
            self.assertEqual(analysis["artifacts"], build["artifacts"])
            for source, digest in analysis["fixture_sources"].items():
                self.assertEqual(sha(ROOT / source), digest)
            boots = read_json(base / "captures/results.json")
            logs = sorted((base / "captures").glob("console-*.log"))
            self.assertEqual((len(logs), len(boots), len(analysis["runs"])), (1, 1, 1))
            self.assertIs(boots[0]["success"], True)
            self.assertEqual(boots[0]["image_sha256"], build["artifacts"][build["image"]])
            self.assertEqual(boots[0]["command"], control["qemu_command"])
            self.assertEqual(boots[0]["command"][-2:], analysis["qemu_clock_options"])
            self.assertEqual(previous.analyze(logs[0].read_text(), analysis["mode"], analysis["tick_us"]),
                             analysis["runs"][0])
            self.assertIs(analysis["characterization_complete"], True)
            self.assertIs(analysis["qualified"], False)


if __name__ == "__main__":
    unittest.main()
