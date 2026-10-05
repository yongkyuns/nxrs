"""Regression tests for the bounded scheduling comparison sidecar."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix = load("test_scheduling_matrix", "scheduling_matrix.py")
report = load("test_scheduling_report", "scheduling_report.py")


def sched_marker(**edits):
    values = {
        "policy": "event", "work_mode": "monolithic", "budget_events": 4,
        "budget_us": 500, "chunk_iterations": 10000, "io_wait_us": 3000,
        "yields": 2, "io_jobs": 0, "io_p99_us": 0, "io_max_us": 0,
        "work_digest": 0, "diagnostic_bytes": 276,
    }
    values.update(edits)
    return "ES_SCHED " + " ".join(f"{k}={v}" for k, v in values.items())


class SchedulingValidationTests(unittest.TestCase):
    def setUp(self):
        self.configuration = {
            "publication_timer_resolution_ms": 1, "embassy_scheduling": "event",
            "work_mode": "monolithic", "scheduling_budget_events": 4,
            "scheduling_budget_us": 500, "work_chunk_iterations": 10000,
            "io_wait_us": 3000,
        }

    def test_sched_marker_checks_policy_config_latency_and_io_profile(self):
        normal = sched_marker()
        parsed = matrix.validate_sched_row(normal, "normal", self.configuration, "event")
        self.assertEqual(parsed["io_jobs"], 0)
        io_wait = sched_marker(io_jobs=117, io_p99_us=3000, io_max_us=3200)
        self.assertEqual(matrix.validate_sched_row(io_wait, "io-wait", self.configuration, "event")["io_jobs"], 117)
        self.assertEqual(matrix.validate_sched_row(sched_marker(work_digest=12345), "work-short",
                                                   self.configuration, "event")["work_digest"], 12345)
        for damaged in (
            sched_marker(policy="budget"), sched_marker(budget_us=501),
            sched_marker(io_p99_us=3201, io_max_us=3200),
            sched_marker(io_jobs=0, io_max_us=3000),
            sched_marker(io_jobs=117, io_p99_us=2999, io_max_us=3000),
            sched_marker(diagnostic_bytes=272), sched_marker(work_digest=1),
        ):
            with self.subTest(damaged=damaged), self.assertRaises(ValueError):
                matrix.validate_sched_row(damaged, "io-wait", self.configuration, "event")

    def test_io_wait_requires_exactly_117_jobs_and_other_profiles_zero(self):
        with self.assertRaises(ValueError):
            matrix.validate_sched_row(sched_marker(io_jobs=116, io_p99_us=3000, io_max_us=3000),
                                      "io-wait", self.configuration, "event")
        with self.assertRaises(ValueError):
            matrix.validate_sched_row(sched_marker(io_jobs=1, io_p99_us=3000, io_max_us=3000),
                                      "normal", self.configuration, "event")

    def test_variant_policy_is_bound_to_case_label(self):
        self.assertEqual(matrix.CASE_POLICIES["embassy-three-budget"][1], "budget")
        self.assertEqual(matrix.CASE_POLICIES["embassy-three-natural"][1], "natural")
        with self.assertRaises(ValueError):
            matrix._variant(Path("/missing"), "embassy-three-budget")
        # The report rejects tampering before it can relabel one policy as another.
        entry = {"case": "embassy-three-event", "platform": "embassy", "layout": "three",
                 "policy": "budget", "work_mode": "monolithic"}
        expected = matrix.CASE_POLICIES[entry["case"]]
        self.assertNotEqual((entry["platform"], entry["policy"], entry["work_mode"]),
                            (expected[0], expected[1], expected[2]))

    def test_native_chunked_variant_is_not_supported(self):
        self.assertNotIn("nuttx-c-three-chunked", matrix.CASE_POLICIES)
        self.assertEqual(matrix.CASE_POLICIES["embassy-three-chunked"][1], "budget")
        with self.assertRaisesRegex(ValueError, "monolithic"):
            matrix.validate_work_mode("nuttx-c", "chunked")
        matrix.validate_work_mode("nuttx-c", "monolithic")

    def test_artifact_metadata_requires_leaf_names_and_real_integers(self):
        good = {"image.bin": 10, "app.elf": 20}
        self.assertEqual(report._validated_artifact_bytes(good), good)
        for damaged in (
            {"nested/image.bin": 10, "app.elf": 20},
            {"image.bin": True, "app.elf": 20},
            {"image.bin": 10, "app.elf": 20.0},
        ):
            with self.subTest(damaged=damaged), self.assertRaises(ValueError):
                report._validated_artifact_bytes(damaged)

    def test_rotated_case_block_order_is_enforced(self):
        prepared = [
            {"case": "nuttx-c-three", "block": 0},
            {"case": "embassy-three-event", "block": 0},
            {"case": "embassy-three-event", "block": 1},
            {"case": "nuttx-c-three", "block": 1},
        ]
        report._validate_rotated_order(prepared, 2)
        prepared[2], prepared[3] = prepared[3], prepared[2]
        with self.assertRaisesRegex(ValueError, "rotated schedule"):
            report._validate_rotated_order(prepared, 2)

    def test_build_identity_drift_across_blocks_is_rejected(self):
        first = {"image_sha256": "a" * 64, "elf_sha256": "b" * 64,
                 "source_sha256": {"main.rs": "c" * 64},
                 "raw_configuration": {"publication_timer_resolution_ms": 1},
                 "sections": {"resident_ram_bytes": 200},
                 "artifact_bytes": {"image.bin": 300, "app.elf": 400},
                 "kernel_config_identity": None}
        changed = dict(first, image_sha256="d" * 64)
        with self.assertRaisesRegex(ValueError, "image_sha256"):
            report._require_case_metadata_identity(first, changed)
        changed = dict(first, raw_configuration={"publication_timer_resolution_ms": 10})
        with self.assertRaisesRegex(ValueError, "configuration"):
            report._require_case_metadata_identity(first, changed)
        for key, value, label in (
            ("sections", {"resident_ram_bytes": 201}, "section accounting"),
            ("artifact_bytes", {"image.bin": 301, "app.elf": 400}, "artifact sizes"),
            ("kernel_config_identity", "e" * 64, "kernel configuration"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, label):
                report._require_case_metadata_identity(first, dict(first, **{key: value}))

    def test_report_requires_matched_firmware_inputs_and_common_configuration(self):
        fixed = {
            "cpu_mhz": 240, "cores": 1, "flash_mode": "DIO",
            "flash_frequency_mhz": 40, "queues": 60, "slots": 480,
            "event_bytes": 64, "duration_us": 2_000_000, "drain_us": 500_000,
            "application_opt_level": "O2", "work_short_iterations": 10_000,
            "work_medium_iterations": 100_000, "work_long_iterations": 400_000,
            "work_service": 0, "work_kind": 1,
        }
        entries = [{"source_sha256": {"main.rs": "a" * 64},
                    "raw_configuration": {**fixed, "embassy_scheduling": policy},
                    "kernel_config_identity": None, "platform": "embassy"}
                   for policy in ("event", "natural")]
        report._validate_matched_inputs(entries)
        entries[1]["source_sha256"] = {"main.rs": "b" * 64}
        with self.assertRaisesRegex(ValueError, "different firmware inputs"):
            report._validate_matched_inputs(entries)
        entries[1]["source_sha256"] = entries[0]["source_sha256"]
        entries[1]["raw_configuration"]["work_long_iterations"] = 123
        with self.assertRaisesRegex(ValueError, "common workload"):
            report._validate_matched_inputs(entries)
        entries[0].update(platform="nuttx-c", kernel_config_identity="c" * 64)
        entries[1].update(platform="nuttx-rust", kernel_config_identity="d" * 64)
        entries[1]["raw_configuration"]["work_long_iterations"] = 400_000
        with self.assertRaisesRegex(ValueError, "kernel configurations differ"):
            report._validate_matched_inputs(entries)

    def test_sanitization_removes_paths_and_mac_addresses(self):
        cleaned = report._clean_configuration({
            "path": "/Users/alice/private/build", "device": "02:11:22:33:44:55",
            "publication_timer_resolution_ms": 1,
        })
        serialized = json.dumps(cleaned)
        self.assertNotIn("/Users/alice", serialized)
        self.assertNotIn("02:11:22:33:44:55", serialized)
        self.assertIn("[redacted-path]", serialized)
        self.assertIn("[redacted-mac]", serialized)

    def test_public_projection_must_match_reparsed_measurement(self):
        checked = {"profile": "normal", "result": {"accepted": 3900},
                   "services": [], "resources": {}, "memory": {}, "control": {},
                   "free_memory": None, "raw_output_sha256": "a" * 64,
                   "scheduling": {"io_jobs": 0}}
        report._compare_projection(dict(checked), checked)
        stored = dict(checked)
        stored["scheduling"] = {"io_jobs": 1}
        with self.assertRaisesRegex(ValueError, "disagrees"):
            report._compare_projection(stored, checked)

    def test_report_combines_four_runs_across_two_blocks(self):
        entries = [self._prepared_report_entry(block, [100 + block * 200, 200 + block * 200])
                   for block in range(2)]
        with patch.object(report, "validate_matrix", return_value=(
                {"backup_sha256": "d" * 64}, entries, ["normal"], 2, 2)):
            output = report.build_report("unused.json")
        case = output["cases"]["embassy-three-event"]
        summarized = case["profiles"]["normal"]
        self.assertEqual(case["blocks"], [0, 1])
        self.assertEqual(len(summarized["runs"]), 4)
        self.assertEqual(summarized["metrics"]["attempted"], {"median": 250, "max": 400})
        self.assertEqual(summarized["runs"][0]["work_digest"], 0)
        self.assertEqual(summarized["runs"][0]["diagnostic_bytes"], 276)
        self.assertIn("protocol_errors", summarized["runs"][0])
        self.assertIn("peer_control_missed_total", summarized["runs"][0])

    def test_nuttx_free_memory_maxused_contributes_to_peak_heap(self):
        entry = self._prepared_report_entry(0, [100])
        entry.update(case="nuttx-c-three", platform="nuttx-c", policy="native",
                     configuration={"embassy_scheduling": "native"})
        entry["runs"][0]["free_memory"] = {"maxused": 900}
        with patch.object(report, "validate_matrix", return_value=(
                {"backup_sha256": "d" * 64}, [entry], ["normal"], 1, 1)):
            output = report.build_report("unused.json")
        self.assertEqual(output["cases"]["nuttx-c-three"]["peak_heap_bytes"], 900)

    @staticmethod
    def _prepared_report_entry(block, attempts):
        rows = []
        for value in attempts:
            result = {key: value for key in ("attempted", "accepted", "received", "rejected", "missed",
                                              "errors", "start_p99_us", "start_max_us", "queue_p99_us",
                                              "queue_max_us", "control_p99_us", "control_max_us")}
            services = [{"start_p99_us": value, "start_max_us": value + 1,
                         "queue_p99_us": value, "queue_max_us": value + 1,
                         "control_p99_us": value, "missed_control": 1} for _ in range(20)]
            rows.append({"profile": "normal", "result": result, "services": services,
                         "control": {"work_iterations": 0, "work_jobs": 0,
                                     "work_p99_us": 0, "work_max_us": 0},
                         "scheduling": {"yields": 0, "io_jobs": 0, "io_p99_us": 0,
                                        "io_max_us": 0, "work_digest": 0,
                                        "diagnostic_bytes": 276},
                         "memory": {"heap_live": 12}, "resources": {"heap_allocated": 8},
                         "free_memory": None})
        return {"case": "embassy-three-event", "platform": "embassy", "policy": "event",
                "work_mode": "monolithic", "configuration": {"embassy_scheduling": "event"},
                "image_sha256": "a" * 64, "elf_sha256": "b" * 64,
                "source_sha256": {"main.rs": "c" * 64}, "sections": {
                    "loadbearing_flash_bytes": 100, "resident_ram_bytes": 200},
                "artifact_bytes": {"image.bin": 300, "app.elf": 400},
                "runs": rows, "block": block}

    def test_backup_restore_runs_when_measurement_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            args = ["scheduling_matrix.py", "--artifacts", str(root), "--out", str(out),
                    "--backup", str(root / "backup"), "--port", "PORT", "--flasher", "FLASH",
                    "--case", "embassy-three-event", "--runs", "1", "--blocks", "1",
                    "--profile", "normal"]
            variant = {"image": root / "image.bin", "elf": root / "app.elf",
                       "provenance": {"image_file": "image.bin", "elf_file": "app.elf",
                                      "source_sha256": {}}, "artifact_bytes": {},
                       "platform": "embassy", "policy": "event", "work_mode": "monolithic",
                       "configuration": {}}
            with patch.object(matrix.sys, "argv", args), \
                 patch.object(matrix.matrix, "backup_identity", return_value="f" * 64), \
                 patch.object(matrix, "_variant", return_value=variant), \
                 patch.object(matrix, "validate_matched_inputs", return_value=None), \
                 patch.object(matrix.subprocess, "run", side_effect=RuntimeError("measurement failed")), \
                 patch.object(matrix.matrix, "restore") as restore:
                with self.assertRaisesRegex(RuntimeError, "measurement failed"):
                    matrix.main()
            restore.assert_called_once()
            saved = json.loads((out / "scheduling-matrix.json").read_text())
            self.assertTrue(saved["restore_verified"])
            self.assertIn("measurement failed", saved["failure"])


if __name__ == "__main__":
    unittest.main()
