"""Keep frozen message/restart cohorts consistent, not a speed gate."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from evidence import kernel_header_identity, psram_identity


RESULTS = Path(__file__).resolve().parent / "results"
# Identity of the frozen 29-patch compiler measured here, not a toolchain default.
COMPILER_PACKAGE_SHA256 = "ece17db7d42f3852049df12570821b1411bb2617085270ad1a4f2234339896e9"
PAIRED_INPUTS = ("config_identity", "kernel_archive_inventory_sha256",
                 "c_compiler_sha256", "c_flags")


def record(kind):
    return json.loads((RESULTS / f"esp32s3-{kind}-2026-10-09.json").read_text())


class CurrentDeviceEvidenceTests(unittest.TestCase):
    def assert_paired_inputs(self, builds):
        for field in PAIRED_INPUTS:
            self.assertEqual(builds["c"][field], builds["rust"][field], field)
        self.assertEqual(builds["rust"]["compiler"]["package_sha256"], COMPILER_PACKAGE_SHA256)

    def assert_flat_heap(self, row):
        self.assertIs(row["observed_steady_heap_flat"], True)
        self.assertEqual(row["steady_heap_net_change_bytes"], 0)
        self.assertEqual(row["steady_heap_min_bytes"], row["steady_heap_max_bytes"])
        self.assertEqual(row["steady_allocations_min"], row["steady_allocations_max"])

    def test_capture_modes_use_the_same_frozen_images_and_verified_headers(self):
        matrix, restart = record("message"), record("message-restart")
        self.assertEqual(matrix["builds"], restart["builds"])
        for item in (matrix, restart, record("message-no-psram"), record("message-restart-no-psram")):
            self.assert_paired_inputs(item["builds"])
            self.assertIs(item["paired_kernel_headers_verified"], True)
            kernel_header_identity(item["builds"])
            self.assertFalse(item["interrupt_qualified"])
            for language, build in item["builds"].items():
                self.assertEqual(build["thread_stack_bytes"], 4096)
                self.assertEqual(set(build["artifacts"]), {"app.elf", "image.bin", "resolved.config"})
                self.assertIn("tests/service-qualification/pulse_snapshot.h", build["source_sha256"])
                for inventory in (build["artifacts"], build["source_sha256"]):
                    for name, digest in inventory.items():
                        self.assertFalse(Path(name).is_absolute())
                        self.assertNotIn("..", Path(name).parts)
                        self.assertRegex(digest, r"^[0-9a-f]{64}$")
                if language == "rust":
                    self.assertEqual(len(build["compiler"]["patches"]), 29)
            for marker in ("/Users/", "/home/", "/private/", "backup_sha256", "transcript", "rustflags"):
                self.assertNotIn(marker, json.dumps(item))

    def test_input_guard_rejects_mismatched_inputs_and_an_unfrozen_compiler(self):
        for kind in ("message", "message-restart"):
            builds = record(kind)["builds"]
            for field in PAIRED_INPUTS:
                changed = copy.deepcopy(builds)
                changed["rust"][field] = "-std=c11 -Os" if field == "c_flags" else "b" * 64
                with self.subTest(kind=kind, field=field), self.assertRaises(AssertionError):
                    self.assert_paired_inputs(changed)
            changed = copy.deepcopy(builds)
            changed["rust"]["compiler"]["package_sha256"] = "0" * 64
            with self.subTest(kind=kind, field="package_sha256"), self.assertRaises(AssertionError):
                self.assert_paired_inputs(changed)

    def assert_message_matrix(self, item):
        expected = [(block, language, services) for block in range(3)
                    for language in (("c", "rust") if block % 2 == 0 else ("rust", "c"))
                    for services in (3, 20)]
        self.assertEqual([(row["block"], row["language"], row["services"]) for row in item["runs"]], expected)
        self.assertEqual(sum(row["received"] for row in item["runs"]), 12000)
        for row in item["runs"]:
            self.assertEqual((row["events"], row["received"]), (1000, 1000))
            memory = row["memory"]
            self.assertEqual(memory["delta"], memory["full"] - memory["before"])
            self.assertEqual(memory["stacks"], row["services"] * 4096)
            self.assertEqual(memory["payload"], row["services"] * 17 * 16)
            resident = item["builds"][row["language"]]["resident_ram_bytes"]
            self.assertEqual(row["full_capacity_ram_bytes"], resident + memory["full"])
            self.assertGreaterEqual(row["observed_peak_ram_bytes"], row["full_capacity_ram_bytes"])
        for block in range(3):
            for services in (3, 20):
                paired = {row["language"]: row for row in item["runs"]
                          if row["block"] == block and row["services"] == services}
                self.assertEqual(paired["c"]["memory"]["delta"], paired["rust"]["memory"]["delta"])
                self.assertEqual(paired["rust"]["full_capacity_ram_bytes"] - paired["c"]["full_capacity_ram_bytes"], 328)

    def test_message_matrix_delivery_order_and_ram_accounting(self):
        self.assert_message_matrix(record("message"))

    def test_no_psram_cohort_keeps_workload_and_frozen_compiler_separate(self):
        original, current = record("message"), record("message-no-psram")
        self.assertIsNone(psram_identity(original["builds"]))
        self.assertIs(psram_identity(current["builds"]), False)
        for language in ("c", "rust"):
            old, new = original["builds"][language], current["builds"][language]
            self.assertEqual(old["source_sha256"], new["source_sha256"])
            self.assertNotEqual(old["config_identity"], new["config_identity"])
        self.assertEqual(original["builds"]["rust"]["compiler"], current["builds"]["rust"]["compiler"])
        self.assert_message_matrix(current)
        self.assertLess(max(row["observed_peak_ram_bytes"] for row in current["runs"]),
                        current["ram_budget_bytes"])

    def assert_restart_matrix(self, item):
        self.assertEqual((item["calls"], item["delivered_events"]), (80, 8000))
        self.assertEqual((item["rounds"], item["blocks"], item["warmup_rounds"]), (10, 2, 2))
        self.assertTrue(item["restoration_verified"])
        self.assertTrue(item["same_boot_per_language_block"])
        self.assertFalse(item["same_process"])
        self.assertFalse(item["fault_injection"])
        self.assertEqual([(row["block"], row["language"]) for row in item["summaries"]],
                         [(block, language) for block in range(2) for language in ("c", "rust")])
        for row in item["summaries"]:
            self.assertEqual((row["calls"], row["delivered_events"], row["steady_samples"]), (20, 2000, 16))
            self.assert_flat_heap(row)

    def test_restart_evidence_is_bounded_and_reports_flat_post_warmup_heap(self):
        self.assert_restart_matrix(record("message-restart"))

    def test_no_psram_restart_reuses_the_message_images_and_has_flat_heap(self):
        message, restart = record("message-no-psram"), record("message-restart-no-psram")
        self.assertEqual(message["builds"], restart["builds"])
        self.assertIs(psram_identity(restart["builds"]), False)
        self.assert_restart_matrix(restart)
        for row in restart["summaries"]:
            self.assertEqual(row["steady_heap_min_bytes"], 7332 if row["language"] == "c" else 7372)

    def test_no_psram_sustained_delivery_and_recovery_reuse_normal_images(self):
        message, sustained = record("message-no-psram"), record("sustained-no-psram")
        self.assertEqual(message["builds"], sustained["builds"])
        self.assertIs(psram_identity(sustained["builds"]), False)
        self.assertEqual(sustained["profile"], "sustained-delivery-and-recovery")
        self.assertEqual((sustained["period_us"], sustained["recovery_period_us"]), (100, 2000))
        self.assertTrue(sustained["same_boot_recovery"])
        self.assertTrue(sustained["restoration_verified"])
        self.assertFalse(sustained["same_process"])
        self.assertFalse(sustained["overload_qualified"])
        self.assertFalse(sustained["retry_counts_available"])
        self.assertEqual([(row["block"], row["language"]) for row in sustained["runs"]],
                         [(0, "c"), (0, "rust"), (1, "rust"), (1, "c")])
        self.assertEqual(sum(row["received"] for row in sustained["runs"]), 80000)
        self.assertEqual(sum(row["recovery"]["received"] for row in sustained["runs"]), 400)
        for row in sustained["runs"]:
            self.assertEqual((row["services"], row["events"], row["received"]), (20, 20000, 20000))
            self.assertGreater(row["command_wall_seconds"], 10)
            self.assertLess(row["observed_peak_ram_bytes"], 250000)
            self.assertEqual(row["recovery"]["post_load_heap_change_bytes"], 0)
            self.assertEqual(row["recovery"]["post_load_allocation_change"], 0)
            self.assertEqual(row["recovery"]["events"], 100)
        for marker in ("/Users/", "/home/", "/private/", "backup_sha256", "transcript", "rustflags"):
            self.assertNotIn(marker, json.dumps(sustained))

    def test_no_psram_faults_use_the_same_kernel_but_separate_diagnostic_images(self):
        normal, faults = record("message-no-psram"), record("shutdown-faults-no-psram")
        self.assertIs(psram_identity(faults["builds"]), False)
        kernel_header_identity(faults["builds"])
        self.assertEqual((faults["calls"], faults["expected_failures"], faults["recoveries"],
                          faults["verified_events"]), (316, 192, 120, 12400))
        for flag in ("diagnostic_fault_injection", "same_coordinator", "restoration_verified",
                     "paired_kernel_headers_verified"):
            self.assertIs(faults[flag], True)
        for flag in ("performance_claim", "foreign_task_recovery_qualified", "interrupt_qualified"):
            self.assertIs(faults[flag], False)
        for language, diagnostic in faults["builds"].items():
            baseline = normal["builds"][language]
            for field in ("config_identity", "kernel_header_sha256"):
                self.assertEqual(diagnostic[field], baseline[field])
            self.assertEqual(diagnostic["kernel_inventory_sha256"], baseline["kernel_archive_inventory_sha256"])
            for source, digest in baseline["source_sha256"].items():
                self.assertEqual(diagnostic["source_sha256"][source], digest)
            self.assertNotEqual(diagnostic["artifacts"]["app.elf"], baseline["artifacts"]["app.elf"])
            self.assertEqual(diagnostic["compiler_package_sha256"],
                             COMPILER_PACKAGE_SHA256 if language == "rust" else None)
        self.assertEqual([(row["block"], row["language"]) for row in faults["summaries"]],
                         [(0, "c"), (0, "rust"), (1, "rust"), (1, "c")])
        for row in faults["summaries"]:
            self.assertEqual((row["calls"], row["failures"], row["recoveries"], row["status"]), (79, 48, 30, 0))
            self.assertEqual(row["heap_before"], row["heap_after"])
            self.assertEqual(row["verified_warmup_events"] + row["verified_recovery_events"], 3100)
            self.assertEqual(len(row["profiles"]), 3)
            for profile in row["profiles"]:
                self.assertEqual(tuple(profile[key] for key in ("descriptors", "handles", "names", "heap_growth")),
                                 (0, 0, 0, 0))
                self.assertLess(profile["max_call_ms"], 2000)
        for marker in ("/Users/", "/home/", "/private/", "backup_sha256", "transcript", "SQ_RESULT"):
            self.assertNotIn(marker, json.dumps(faults))

    def test_no_psram_pressure_uses_separate_images_and_real_traffic_full_counts(self):
        # Reuse the publication guard, rather than a second copy of its contract.
        with patch.object(sys, "path", sys.path.copy()):
            from pressure import STEPS, check_profile
        normal, pressure = record("message-no-psram"), record("pressure-no-psram")
        self.assertIs(psram_identity(pressure["builds"]), False)
        kernel_header_identity(pressure["builds"])
        self.assertEqual((pressure["calls"], pressure["expected_cancellations"], pressure["verified_events"]),
                         (40, 12, 26176))
        self.assertEqual((pressure["services"], pressure["queues"], pressure["event_bytes"],
                          pressure["queue_capacities"]), (20, 60, 16, [1, 8, 8]))
        for flag in ("diagnostic_pressure", "same_coordinator", "restoration_verified",
                     "paired_kernel_headers_verified", "producer_pacing_suppressed", "retry_sleep_preserved",
                     "post_join_heap_delaylist_drained",
                     "initial_capacity_probe_excluded_from_pressure_counts"):
            self.assertIs(pressure[flag], True)
        for flag in ("performance_claim", "ordinary_image_footprint_claim", "interrupt_qualified",
                     "arbitrary_overload_qualified"):
            self.assertIs(pressure[flag], False)
        source = "tests/service-qualification/pressure.c"
        expected = hashlib.sha256((RESULTS.parent / "pressure.c").read_bytes()).hexdigest()
        for language, diagnostic in pressure["builds"].items():
            baseline = normal["builds"][language]
            for field in ("config_identity", "kernel_header_sha256", "c_compiler_sha256", "c_flags", "thread_stack_bytes"):
                self.assertEqual(diagnostic[field], baseline[field])
            self.assertEqual(diagnostic["kernel_inventory_sha256"], baseline["kernel_archive_inventory_sha256"])
            self.assertEqual(set(diagnostic["source_sha256"]) - set(baseline["source_sha256"]), {source})
            self.assertEqual(diagnostic["source_sha256"][source], expected)
            for name, digest in baseline["source_sha256"].items():
                self.assertEqual(diagnostic["source_sha256"][name], digest)
            self.assertNotEqual(diagnostic["artifacts"]["app.elf"], baseline["artifacts"]["app.elf"])
            self.assertEqual(diagnostic["compiler_package_sha256"],
                             COMPILER_PACKAGE_SHA256 if language == "rust" else None)
        self.assertEqual([(row["block"], row["language"]) for row in pressure["summaries"]],
                         [(0, "c"), (0, "rust"), (1, "rust"), (1, "c")])
        for row in pressure["summaries"]:
            self.assertEqual(row["heap_before"], row["heap_after"])
            self.assertEqual([(p["phase"], p["index"]) for p in row["profiles"]], STEPS)
            for profile in row["profiles"]:
                check_profile(profile, row["heap_before"])
        for marker in ("/Users/", "/home/", "/private/", "backup_sha256", "transcript", "SQ_RESULT", "rustflags"):
            self.assertNotIn(marker, json.dumps(pressure))

    def test_flat_heap_guard_rejects_variation_despite_flat_flag_and_zero_net_change(self):
        for row in record("message-restart")["summaries"]:
            for field in ("steady_heap_min_bytes", "steady_heap_max_bytes",
                          "steady_allocations_min", "steady_allocations_max"):
                changed = copy.deepcopy(row)
                changed[field] += 1
                with self.subTest(block=row["block"], language=row["language"], field=field), \
                        self.assertRaises(AssertionError):
                    self.assert_flat_heap(changed)


if __name__ == "__main__":
    unittest.main()
