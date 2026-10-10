import copy
import json
import unittest

from publish import public_report


def report():
    """Minimal paired evidence with deliberately private raw-only fields."""
    build = dict(
        binary_bytes=200000, accounting=dict(loadbearing_flash_bytes=170000, resident_ram_bytes=66000),
        config_identity="config", kernel_archives={"libkernel.a": "archive"},
        kernel_header_sha256="a" * 64,
        c_flags="-std=c11 -O2", c_compiler_sha256="cc", thread_stack=4096,
        artifacts={"image.bin": "binary"}, source_sha256={"tests/service-qualification/runtime.c": "source"},
        compiler_input=None, private_path="/private/sdk",
    )
    proof = dict(
        patch_ledger=dict(upstream_revision="llvm", rust_revision="rust",
                          patches=[dict(name="fix.patch", sha256="patch", path="/private/fix.patch")]),
        compiler_driver_sha256={"rustc": "driver"}, target_sha256="target",
        std_inventory_sha256="std", std_features=["optimize_for_size"],
        input_sha256="input", compiler_package_sha256="package", rustflags="/private/link.py",
    )
    rust = copy.deepcopy(build)
    rust["compiler_input"] = proof
    run = dict(
        language="c", block=0, result=dict(services=3, queues=9, events=10, received=10, errors=0,
            source="messages", mean_cycles=240, max_cycles=480, misses_1ms=0),
        memory=dict(before=100, full=1000, delta=900, metadata=240, payload=816, stacks=12288),
        done=dict(status=0), full_capacity_ram_bytes=67000,
        nsh_before=dict(used=100, maxused=200), nsh_after=dict(used=100, maxused=1200),
        transcript="private serial content",
    )
    return dict(failure=None, restoration_error=None, restoration_verified=True, source="messages", period_us=2000,
                builds=dict(c=build, rust=rust), runs=[run], backup_sha256="private backup hash")


class PublishTests(unittest.TestCase):
    def test_psram_state_is_retained_without_relabeling_legacy_records(self):
        original = public_report([report()])
        self.assertNotIn("psram_enabled", original["builds"]["c"])
        for state in (False, True):
            item = report()
            for build in item["builds"].values():
                build["psram_enabled"] = state
            result = public_report([item])
            self.assertTrue(all(build["psram_enabled"] is state
                                for build in result["builds"].values()))
        item["builds"]["rust"]["psram_enabled"] = False
        with self.assertRaisesRegex(ValueError, "PSRAM"):
            public_report([item])

    def test_compact_report_retains_evidence_and_excludes_private_fields(self):
        result = public_report([report()])
        self.assertEqual(result["runs"][0]["observed_peak_ram_bytes"], 67200)
        self.assertEqual(result["builds"]["c"]["thread_stack_bytes"], 4096)
        self.assertEqual(result["builds"]["rust"]["compiler"]["patches"],
                         [dict(name="fix.patch", sha256="patch")])
        self.assertFalse(result["interrupt_qualified"])
        self.assertEqual(result["fault_path_review"], "open")
        self.assertTrue(result["paired_kernel_headers_verified"])
        self.assertEqual(result["builds"]["c"]["kernel_header_sha256"], "a" * 64)
        self.assertEqual(result["builds"]["rust"]["kernel_header_sha256"], "a" * 64)
        encoded = json.dumps(result)
        for private in ("/private", "backup_sha256", "transcript", "rustflags"):
            self.assertNotIn(private, encoded)

    def test_empty_failed_and_unrestored_reports_are_rejected(self):
        with self.assertRaises(ValueError):
            public_report([])
        for field, value in (("failure", "TimeoutError"), ("restoration_verified", False),
                             ("restoration_error", "restore failed"),
                             ("runs", []), ("source", "gpio"), ("period_us", 1000)):
            item = report()
            item[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                public_report([item])
        item = report()
        item["builds"]["c"]["diagnostic_trace"] = True
        with self.assertRaises(ValueError):
            public_report([item])
        for language, header in (("c", "b" * 64), ("rust", None)):
            item = report()
            item["builds"][language]["kernel_header_sha256"] = header
            with self.subTest(language=language), self.assertRaises(ValueError):
                public_report([item])
        for field, value in (("diagnostic_perfmon", True),
                             ("diagnostic_faults", True),
                             ("diagnostic_layout_padding_bytes", 0),
                             ("diagnostic_hot_iram", {"selectors": ["runtime"]})):
            item = report()
            item["builds"]["rust"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                public_report([item])

    def test_paired_and_cross_report_build_changes_are_rejected(self):
        for field in ("config_identity", "kernel_archives", "c_flags", "c_compiler_sha256", "thread_stack"):
            item = report()
            item["builds"]["rust"][field] = "changed"
            with self.subTest(field=field), self.assertRaises(ValueError):
                public_report([item])
        item = report()
        item["builds"]["c"]["binary_bytes"] += 1
        with self.assertRaises(ValueError):
            public_report([report(), item])

    def test_private_source_paths_and_failed_rows_are_rejected(self):
        for name in ("/private/sdk/runtime.c", "../runtime.c"):
            item = report()
            item["builds"]["c"]["source_sha256"] = {name: "source"}
            with self.subTest(name=name), self.assertRaises(ValueError):
                public_report([item])
        for field, value in (("errors", 1), ("received", 9), ("source", "gpio")):
            item = report()
            item["runs"][0]["result"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                public_report([item])


if __name__ == "__main__":
    unittest.main()
