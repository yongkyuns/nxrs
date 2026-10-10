"""Report validation, aggregation and privacy tests using frozen matrix JSON."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "event_services_report_tests", Path(__file__).with_name("report.py"))
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


def write_matrix(root, *, platform="nuttx-c", layout="three", blocks=2,
                 runs_per_profile=2, profiles=None):
    profiles = list(profiles or report.PROFILES)
    case = f"{platform}-{layout}"
    resident = 2048
    section_accounting = {
        "loadbearing_flash_bytes": 12000,
        "resident_ram_bytes": resident,
        "flash_sections": [".text", ".rodata"],
        "resident_ram_sections": [".text", ".data", ".bss"],
        "excluded_dummy_padding": [{"name": ".dram0.dummy", "size": 32}],
        "note": "fictional test section accounting",
    }
    entries = []
    for block in range(blocks):
        runs = []
        for profile in profiles:
            for run_index in range(runs_per_profile):
                rejected = int(profile == "normal" and block == 0 and run_index == 0)
                missed = 1 if profile == "burst" and block == 1 and run_index == 1 else 0
                attempted = report.EXPECTED_ATTEMPTS[profile]
                accepted = attempted - rejected
                received_by_service = [0] * 20
                received_by_service[0] = accepted
                missed_by_service = [0] * 20
                missed_by_service[0] = missed
                start_p99 = 10 + block * 100 + run_index * 10
                queue_p99 = 5 + block * 20 + run_index * 2
                services = [{
                    "id": service_id,
                    "received": received_by_service[service_id],
                    "start_p99_us": start_p99 if service_id == 0 else 0,
                    "start_max_us": start_p99 + 2 if service_id == 0 else 0,
                    "finish_p99_us": start_p99 + 1 if service_id == 0 else 0,
                    "control_p99_us": start_p99 if service_id == 0 else 0,
                    "queue_p99_us": queue_p99 if service_id == 0 else 0,
                    "queue_max_us": queue_p99 + 5 if service_id == 0 else 0,
                    "missed_control": missed_by_service[service_id],
                    "missed_data": 0,
                    "missed_status": 0,
                    "rejected": rejected if service_id == 0 else 0,
                } for service_id in range(20)]
                result_row = {
                    "platform": platform,
                    "profile": profile,
                    "services": 20,
                    "queues": 20 if layout == "one" else 60,
                    "event_bytes": 64,
                    "slots": 480,
                    "attempted": attempted,
                    "accepted": accepted,
                    "received": accepted,
                    "rejected": rejected,
                    "errors": 0,
                    "missed": missed,
                    "publication_p99_us": start_p99 + 2,
                    "publication_max_us": start_p99 + 4,
                    "start_p99_us": start_p99,
                    "start_max_us": start_p99 + 2,
                    "finish_p99_us": start_p99 + 1,
                    "finish_max_us": start_p99 + 3,
                    "control_p99_us": start_p99,
                    "control_max_us": start_p99 + 2,
                    "queue_p99_us": queue_p99,
                    "queue_max_us": queue_p99 + 5,
                    "worst_service_p99_us": start_p99,
                    "queue_peak_observed": 2,
                    "last_finish_us": 500000 + block * 1000 + run_index,
                    "delivery_ok": 1,
                    "capacity_ok": int(rejected == 0),
                    "deadlines_ok": int(missed == 0),
                }
                resources = {
                    "queue_buffers": 30720, "queue_objects": 3200,
                    "thread_objects": 800, "entry_storage": 320,
                    "stack_storage": 81920, "heap_allocated": 1024 + run_index,
                    "diagnostics_queue_peaks": 4,
                    "note": "queue peak may underestimate",
                }
                memory = {"application_state": 400, "diagnostics": 200,
                          "heap_before": 1024, "heap_live": 1200 + run_index}
                free_memory = None
                if platform.startswith("nuttx-"):
                    used = 3000 + block * 100 + run_index
                    free_memory = {"total": 10000, "used": used,
                                   "free": 10000 - used,
                                   "maxused": 500 + block * 100 + run_index,
                                   "maxfree": 7000, "nused": 10, "nfree": 20}
                runs.append({
                    "profile": profile, "result": result_row, "services": services,
                    "resources": resources, "memory": memory,
                    "free_memory": free_memory,
                    "raw_output_sha256": f"{block + 1:064x}",
                    # These injected values must never be copied into the report.
                    "raw_output": "ES_PASS /dev/cu.usbserial-SECRET MAC=AA:BB:CC:DD:EE:FF",
                    "serial_path": "/dev/cu.usbserial-SECRET",
                })
        entries.append({
            "case": case, "block": block, "platform": platform, "layout": layout,
            "image_sha256": "a" * 64, "elf_sha256": "b" * 64,
            "artifact_bytes": {"app.elf": 50000, "image.bin": 23000},
            "configuration": {"cpu_mhz": 240, "cores": 1, "slots": 480},
            "kernel_config_identity": "c" * 64,
            "source_sha256": {"platform_nuttx.c": "d" * 64,
                              "../service-footprint/relink_rust.py": "e" * 64},
            "section_accounting": section_accounting,
            "resolved_configuration": {"CONFIG_TEST": "y"},
            "runs": runs,
        })
    record = {
        "schema": 1, "backup_sha256": "f" * 64,
        "runs_per_profile": runs_per_profile, "blocks": blocks,
        "profiles": profiles, "order": entries, "failure": None,
        "restore_verified": True, "restore_error": None,
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "matrix.json"
    path.write_text(json.dumps(record))
    return path, record


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_case_profile_medians_maxima_qualification_and_full_numeric_rows(self):
        matrix, _ = write_matrix(self.root / "matrix")
        output = report.build_report(matrix)
        case = output["cases"]["nuttx-c-three"]
        normal = case["profiles"]["normal"]
        self.assertEqual(normal["run_count"], 4)
        self.assertEqual(normal["qualified_run_count"], 3)
        self.assertEqual(normal["result_metrics"]["start_p99_us"],
                         {"median": 65, "max": 120})
        self.assertEqual(normal["result_metrics"]["start_max_us"],
                         {"median": 67, "max": 122})
        self.assertEqual(normal["result_metrics"]["queue_p99_us"],
                         {"median": 16, "max": 27})
        self.assertEqual(normal["result_metrics"]["queue_max_us"],
                         {"median": 21, "max": 32})
        self.assertEqual(len(normal["service_metrics"]), 20)
        self.assertEqual(normal["service_metrics"][0]["metrics"]["start_p99_us"],
                         {"median": 65, "max": 120})
        self.assertEqual(normal["service_metrics"][0]["metrics"]["queue_p99_us"],
                         {"median": 16, "max": 27})
        self.assertEqual(len(normal["runs"]), 4)
        self.assertEqual(len(normal["runs"][0]["services"]), 20)
        self.assertEqual(normal["resource_metrics"]["heap_allocated"],
                         {"median": 1024.5, "max": 1025})
        self.assertEqual(normal["resource_ledger"]["free_memory_maxused_bytes"],
                         {"median": 550.5, "max": 601})
        self.assertEqual(normal["resource_ledger"]["whole_ram_footprint_bytes"],
                         {"median": 2598.5, "max": 2649})
        self.assertEqual(case["image_bytes"], 23000)
        self.assertEqual(case["section_accounting"]["loadbearing_flash_bytes"], 12000)
        self.assertEqual(case["section_accounting"]["resident_ram_bytes"], 2048)
        self.assertEqual(case["source_sha256"]["platform_nuttx.c"], "d" * 64)
        self.assertEqual(set(case["profiles"]), {"normal", "burst"})
        self.assertEqual(output["cases"]["nuttx-c-three"]["profiles"]["burst"]
                         ["qualified_run_count"], 3)
        self.assertIn("no pooled p99", output["method"]["quantiles"])
        self.assertIn("100 Hz tick quantization", output["method"]["queue_latency"])

    def test_zephyr_uses_resident_sections_without_adding_static_arena_twice(self):
        matrix, _ = write_matrix(self.root / "matrix", platform="zephyr-c", layout="one",
                                 profiles=["normal"])
        case = report.build_report(matrix)["cases"]["zephyr-c-one"]
        ledger = case["profiles"]["normal"]["resource_ledger"]
        self.assertEqual(ledger["whole_ram_footprint_bytes"],
                         {"median": 2048, "max": 2048})
        self.assertIsNone(ledger["free_memory_maxused_bytes"])
        self.assertIn("static arena is already included", ledger["arena_accounting"])

    def test_nuttx_whole_ram_is_unavailable_without_maxused_observations(self):
        matrix, record = write_matrix(self.root / "matrix", profiles=["normal"])
        for entry in record["order"]:
            for run in entry["runs"]:
                run["free_memory"] = None
        matrix.write_text(json.dumps(record))
        ledger = report.build_report(matrix)["cases"]["nuttx-c-three"]
        ledger = ledger["profiles"]["normal"]["resource_ledger"]
        self.assertIsNone(ledger["whole_ram_footprint_bytes"])
        self.assertIsNone(ledger["free_memory_maxused_bytes"])
        self.assertEqual(ledger["free_memory_observed_run_count"], 0)
        self.assertEqual(ledger["whole_ram_unavailable_run_count"], 4)

        record["order"][0]["runs"][0]["free_memory"] = {
            "total": 10000, "used": 3000, "free": 7000,
            "maxused": 500, "maxfree": 7000, "nused": 10, "nfree": 20,
        }
        matrix.write_text(json.dumps(record))
        ledger = report.build_report(matrix)["cases"]["nuttx-c-three"]
        ledger = ledger["profiles"]["normal"]["resource_ledger"]
        self.assertIsNone(ledger["whole_ram_footprint_bytes"])
        self.assertEqual(ledger["free_memory_observed_run_count"], 1)
        self.assertEqual(ledger["whole_ram_unavailable_run_count"], 3)


class ValidationAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_failed_restore_and_incomplete_counts_are_rejected(self):
        matrix, record = write_matrix(self.root / "restore")
        record["restore_verified"] = False
        matrix.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "successful and restore_verified"):
            report.build_report(matrix)

        matrix, record = write_matrix(self.root / "incomplete")
        record["order"] = record["order"][:1]
        matrix.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "missing one or more case blocks"):
            report.build_report(matrix)

        matrix, record = write_matrix(self.root / "bad-count")
        record["order"][0]["runs"] = []
        matrix.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "incomplete run count"):
            report.build_report(matrix)

        matrix, record = write_matrix(self.root / "overload", profiles=["normal"])
        record["profiles"] = ["overload"]
        matrix.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "normal/burst profiles only"):
            report.build_report(matrix)

    def test_inconsistent_rows_and_absolute_source_paths_are_rejected(self):
        matrix, record = write_matrix(self.root / "bad-service", profiles=["normal"])
        record["order"][0]["runs"][0]["services"].pop()
        matrix.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "all 20 service rows"):
            report.build_report(matrix)

        matrix, record = write_matrix(self.root / "bad-source", profiles=["normal"])
        record["order"][0]["source_sha256"] = {"/Users/private/adapter.c": "d" * 64}
        matrix.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "absolute source paths"):
            report.build_report(matrix)

        matrix, record = write_matrix(self.root / "old-pilot", profiles=["normal"])
        del record["order"][0]["runs"][0]["result"]["queue_p99_us"]
        matrix.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "result.queue_p99_us"):
            report.build_report(matrix)

    def test_cli_writes_fresh_private_report_without_raw_serial_or_device_paths(self):
        matrix, record = write_matrix(self.root / "input", profiles=["normal"])
        for entry in record["order"]:
            entry["configuration"]["local_hint"] = "/Users/private/device MAC=AA:BB:CC:DD:EE:FF"
        matrix.write_text(json.dumps(record))
        output = self.root / "private" / "report.json"
        report.main(["--matrix", str(matrix), "--out", str(output)])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        serialized = output.read_text()
        self.assertNotIn("ES_PASS", serialized)
        self.assertNotIn("cu.usbserial", serialized)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", serialized)
        self.assertNotIn("/Users/", serialized)
        exported = json.loads(serialized)
        self.assertEqual(exported["input"]["restore_verified"], True)
        hint = exported["cases"]["nuttx-c-three"]["configuration"]["local_hint"]
        self.assertEqual(hint, "[redacted-path] MAC=[redacted-mac]")
        with self.assertRaises(SystemExit):
            report.main(["--matrix", str(matrix), "--out", str(output)])


if __name__ == "__main__":
    unittest.main()
