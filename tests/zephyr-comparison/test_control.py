"""Protect the exact workload and bounded OS-only adaptation."""
import unittest
from pathlib import Path
from generate_control import adapt, replace_once, nuttx_without_wake

SOURCE = Path(__file__).resolve().parents[1] / "service-footprint/channel_scale_mq.c"


class ControlTests(unittest.TestCase):
    def test_algorithm_functions_unchanged(self):
        source = SOURCE.read_text()
        generated = adapt(source)
        for first, last in (("static uint32_t checksum(", "static unsigned lanes("),
                            ("static int ready_index(", "static int compare_u32(")):
            section = source[source.index(first):source.index(last)]
            self.assertIn(section, generated)

    def test_no_dynamic_scenario_or_posix_imports(self):
        generated = adapt(SOURCE.read_text())
        self.assertNotIn("calloc(", generated)
        self.assertNotIn("free(scenario)", generated)
        self.assertIn("static struct scenario scenario_storage;", generated)
        for name in ("pthread.h", "mqueue.h", "poll.h"):
            self.assertNotIn(f"#include <{name}>", generated)

    def test_source_drift_fails_closed(self):
        with self.assertRaises(ValueError):
            adapt(SOURCE.read_text().replace("free(scenario);", "dispose(scenario);"))
        with self.assertRaises(ValueError):
            replace_once("xx", "x", "y")

    def test_nuttx_control_only_omits_auxiliary_probe(self):
        source = SOURCE.read_text()
        generated = nuttx_without_wake(source)
        boundary = '#ifndef NXRS_CQ_EMBED_CONTROL\n  if (run_wake() != 0)'
        self.assertEqual(source[:source.index(boundary)], generated[:source.index(boundary)])
        self.assertNotIn('  if (run_wake() != 0)', generated)
        self.assertIn('return run_thread_baseline() == 0 ? 0 : 1;', generated)


if __name__ == "__main__":
    unittest.main()
