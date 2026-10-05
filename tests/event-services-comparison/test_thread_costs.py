"""Keep historical wrapper costs distinct from a measured growth curve."""
import json
from pathlib import Path
import re
import unittest


class ThreadCostEvidenceTests(unittest.TestCase):
    def test_recorded_deltas_and_estimates_reconcile(self):
        evidence = json.loads((Path(__file__).parent / "results" /
                               "thread-wrapper-costs-2026-10-03.json").read_text())
        self.assertTrue(evidence["not_remeasured_in_scheduling_matrix"])
        led = evidence["silent_led"]
        self.assertEqual(led["std"]["linked_flash_bytes"] - led["native"]["linked_flash_bytes"],
                         led["std_minus_native_linked_flash_bytes"])
        scale = evidence["twenty_thread_fixture"]
        for field, delta in (("linked_flash_bytes", "linked_flash_bytes"),
                             ("fixed_dram_bytes", "fixed_dram_bytes"),
                             ("idle_thread_peak_heap_bytes", "idle_peak_heap_bytes"),
                             ("idle_thread_live_heap_median_bytes", "idle_live_heap_median_bytes")):
            self.assertEqual(scale["std"][field] - scale["native"][field],
                             scale["std_minus_native_" + delta])
        estimate = evidence["planning_estimate"]
        self.assertFalse(estimate["measured_growth_curve"])
        average = scale["std_minus_native_idle_peak_heap_bytes"] / scale["threads"]
        self.assertEqual(average, estimate["average_extra_peak_heap_bytes_per_thread"])
        for count, name in ((10, "ten"), (15, "fifteen")):
            self.assertAlmostEqual(average * count, estimate[name + "_threads_extra_peak_heap_bytes"])
        for sample in (led["std"], led["native"], scale["std"], scale["native"]):
            self.assertRegex(sample["elf_sha256"], re.compile(r"^[0-9a-f]{64}$"))


if __name__ == "__main__":
    unittest.main()
