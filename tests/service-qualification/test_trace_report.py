import unittest

from test_publish import report
from trace_report import public_trace


def traced_report():
    item = report()
    item["diagnostic_trace"] = True
    for build in item["builds"].values():
        build["diagnostic_trace"] = True
    run = item["runs"][0]
    run.update(instrumentation=True, path_us=dict(transit=1, worker_excluding_led=1, led=1, end=3), mean_us=3)
    run["trace_rows"] = [dict(id=i, events=10, transit_cycles=2400, worker_cycles=2400,
                              led_cycles=1200 if i == 1 else 0, end_cycles=(i + 1) * 4800, errors=0)
                         for i in range(3)]
    return item


class TraceReportTests(unittest.TestCase):
    def test_export_contains_no_footprint_or_private_transcript(self):
        result = public_trace(traced_report())
        self.assertTrue(result["diagnostic_only"])
        self.assertFalse(result["footprint_claim"])
        self.assertNotIn("binary_bytes", result["builds"]["c"])
        self.assertNotIn("memory", result["runs"][0])
        self.assertNotIn("backup_sha256", result)
        self.assertNotIn("rustflags", result["builds"]["rust"])
        self.assertNotIn("transcript", result["runs"][0])
        self.assertTrue(result["paired_kernel_headers_verified"])
        for build in result["builds"].values():
            self.assertEqual(build["kernel_header_sha256"], "a" * 64)

    def test_unrestored_uninstrumented_and_broken_paths_are_rejected(self):
        for field, value in (("restoration_verified", False), ("restoration_error", "restore failed"),
                             ("diagnostic_trace", False),
                             ("failure", "error"), ("source", "gpio")):
            item = traced_report()
            item[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                public_trace(item)
        for language, header in (("c", "invalid"), ("rust", "b" * 64)):
            item = traced_report()
            item["builds"][language]["kernel_header_sha256"] = header
            with self.subTest(language=language), self.assertRaises(ValueError):
                public_trace(item)
        item = traced_report()
        item["runs"][0]["trace_rows"][1]["end_cycles"] += 1
        with self.assertRaises(ValueError):
            public_trace(item)

    def test_same_image_controls_require_verified_selection(self):
        for control in ("diagnostic_worker_switch", "diagnostic_entry_switch"):
            item = traced_report()
            item["builds"]["c"][control] = True
            with self.subTest(control=control), self.assertRaises(ValueError):
                public_trace(item)


if __name__ == "__main__":
    unittest.main()
