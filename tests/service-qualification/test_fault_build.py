import contextlib
import copy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import build
with patch.object(sys, "path", sys.path.copy()):
    from device_faults import public_report


FAULT_SOURCES = ("lifecycle_faults.c", "lifecycle_faults.h", "lifecycle_device.c")


class FaultBuildTests(unittest.TestCase):
    def test_regular_source_inputs_exclude_fault_fixture(self):
        for language in ("c", "rust"):
            names = {path.name for path in build.source_inputs(language)}
            self.assertTrue(set(FAULT_SOURCES).isdisjoint(names))

    def test_fault_mode_stages_shared_hooks_and_fixture_for_both_native_targets(self):
        for language in ("c", "rust"):
            with self.subTest(language=language):
                sources = build.source_inputs(language, faults=True)
                self.assertTrue(set(FAULT_SOURCES).issubset({path.name for path in sources}))
                with tempfile.TemporaryDirectory() as folder:
                    staged, native = build.stage_native_app(Path(folder), language, faults=True)
                    self.assertTrue(set(FAULT_SOURCES).issubset({path.name for path in staged}))
                    self.assertTrue(set(("lifecycle_faults.c", "lifecycle_device.c")).issubset(native))
                    app = Path(folder) / "apps/examples" / ("nxrs_std_app" if language == "rust" else "nxrs_bench")
                    for filename in FAULT_SOURCES:
                        self.assertTrue((app / filename).is_file())

    def test_gnu_fault_wrappers_are_complete_and_fault_only(self):
        normal = build.make_variables("xtensa-elf-", "c", [])
        fault = build.make_variables("xtensa-elf-", "c", [], faults=True)
        self.assertFalse(any("__wrap_" in item or "EXTRALINKCMDS=" in item for item in normal))
        link = next(item for item in fault if item.startswith("EXTRALINKCMDS="))
        self.assertEqual(link.split("=", 1)[1].split(), ["--wrap=" + name for name in build.FAULT_WRAPS])
        self.assertEqual(len(build.FAULT_WRAPS), len(set(build.FAULT_WRAPS)))

    def test_cli_rejects_fault_mode_combined_with_other_diagnostics_before_link(self):
        conflicts = (("--trace",), ("--perfmon",), ("--hot-iram",),
                     ("--layout-pad-bytes", "0"))
        for conflict in conflicts:
            argv = ["build.py", "link", "--tree", "/unused", "--prefix", "xtensa-elf-",
                    "--out", "/unused-out", "--language", "c", "--baseline", "/unused-config",
                    "--faults", *conflict]
            stderr = io.StringIO()
            with self.subTest(conflict=conflict), patch.object(sys, "argv", argv), \
                    contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                build.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("--faults requires", stderr.getvalue())

    def test_public_report_keeps_diagnostics_private_and_hashes_as_opaque_values(self):
        digest = "a" * 64
        report = {
            "blocks": 1,
            "failure": None,
            "restoration_error": None,
            "restoration_verified": True,
            "fault_injection": True,
            "runs": [dict(block=0, language=language, repetitions=3, calls=79, failures=48, recoveries=30,
                           status=0, heap_before=42, heap_after=42, verified_warmup_events=100,
                           verified_recovery_events=3000,
                           profiles=[dict(scenario=scenario, repetitions=3,
                                          calls=24 if scenario == "fail-close" else 27,
                                          failures=12 if scenario == "fail-close" else 18,
                                          recoveries=12 if scenario == "fail-close" else 9,
                                          descriptors=0, handles=0, names=0, heap_growth=0,
                                          verified_recovery_events=1200 if scenario == "fail-close" else 900,
                                          max_call_ms=12)
                                     for scenario in ("fail-stop", "fail-join", "fail-close")])
                     for language in ("c", "rust")],
            "builds": {language: {"diagnostic_faults": True,
                                  "source_sha256": {"tests/service-qualification/runtime.c": digest},
                                  "artifacts": {"image.bin": digest}, "config_identity": digest,
                                  "kernel_header_sha256": digest,
                                  "kernel_archives": {"libc.a": digest}, "compiler_input": None}
                       for language in ("c", "rust")},
            "harness_sha256": digest,
        }
        public = public_report(report)
        self.assertTrue(public["paired_kernel_headers_verified"])
        for build_row in public["builds"].values():
            self.assertEqual(build_row["kernel_header_sha256"], digest)
        rendered = repr(public)
        for private_marker in ("/Users/", "/home/", "/tmp/", "SQ_RESULT", "SQ_MEMORY", "compiler_input"):
            self.assertNotIn(private_marker, rendered)
        for build_row in public["builds"].values():
            for value in build_row["source_sha256"].values():
                self.assertEqual(len(value), 64)
        for mutation in (
            lambda item: item.update(restoration_error="restore failed"),
            lambda item: item["builds"]["rust"].update(kernel_header_sha256="b" * 64),
            lambda item: item["builds"]["c"].update(kernel_header_sha256="bad"),
        ):
            bad = copy.deepcopy(report)
            mutation(bad)
            with self.assertRaises(ValueError):
                public_report(bad)


if __name__ == "__main__":
    unittest.main()
