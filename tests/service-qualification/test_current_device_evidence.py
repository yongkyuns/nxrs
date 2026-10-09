"""Keep the dated message/restart handoff internally consistent, not a speed gate."""
import copy
import json
from pathlib import Path
import unittest

from evidence import kernel_header_identity


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
        for item in (matrix, restart):
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

    def test_message_matrix_delivery_order_and_ram_accounting(self):
        item = record("message")
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

    def test_restart_evidence_is_bounded_and_reports_flat_post_warmup_heap(self):
        item = record("message-restart")
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
