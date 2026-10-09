import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path

import build
import measure


class LayoutBuildTests(unittest.TestCase):
    def test_no_psram_preparation_changes_only_the_memory_controller_option(self):
        original = [("--enable", "CONFIG_DEV_GPIO"),
                    ("--enable", "CONFIG_ESP32S3_GPIO_IRQ"),
                    ("--enable", "CONFIG_EXAMPLES_NXRS_BENCH"),
                    ("--disable", "CONFIG_EXAMPLES_NXRS_STD_APP")]
        self.assertEqual(build.preparation_options(), original)
        self.assertEqual(build.preparation_options(True),
                         original + [("--disable", "CONFIG_ESP32S3_SPIRAM")])

    def test_default_staging_and_flags_remain_unchanged(self):
        self.assertEqual(build.source_inputs("c"), build.source_inputs("c", layout_pad_bytes=None))
        self.assertEqual(
            build.make_variables("compiler-", "c", []),
            build.make_variables("compiler-", "c", [], layout_pad_bytes=None),
        )
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "apps/examples/nxrs_bench"
            build.stage_native_app(Path(folder), "c")
            self.assertFalse((app / "layout_padding.c").exists())
            self.assertFalse((app / "sq_layout_padding.h").exists())

    def test_staging_generates_deterministic_header_and_precedes_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            sources, native = build.stage_native_app(Path(folder), "c", layout_pad_bytes=64)
            app = Path(folder) / "apps/examples/nxrs_bench"
            self.assertEqual((app / "sq_layout_padding.h").read_text(),
                             "#define SQ_LAYOUT_PADDING_BYTES 64\n")
            self.assertEqual(sources[0], build.HERE / "layout_padding.c")
            self.assertEqual(native[:2], ["layout_padding.c", "runtime.c"])
            self.assertEqual((app / "layout_padding.c").read_bytes(),
                             (build.HERE / "layout_padding.c").read_bytes())

    def test_layout_and_perfmon_link_controls_compose_with_native_flags(self):
        variables = build.make_variables(
            "compiler-", "rust", [], bundle=Path("/bundle"),
            layout_pad_bytes=2048, perfmon=True,
        )
        link = next(item for item in variables if item.startswith("EXTRALINKCMDS="))
        self.assertIn("--undefined=nxrs_sq_layout_padding", link)
        self.assertIn("--wrap=nxrs_sq_ready", link)
        self.assertIn("--wrap=nxrs_cq_thread_join", link)
        self.assertNotIn("--wrap=nxrs_sq_run", link)
        self.assertIn("NXRS_TARGET_C_FLAGS=-std=c11 -O2", variables)

    def test_plain_inputs_exclude_removed_investigation_helpers(self):
        names = {path.name for path in build.source_inputs("rust")}
        self.assertTrue({"trace.c", "entry_switch.c", "worker_switch.c"}.isdisjoint(names))

    def test_measure_rejects_old_instrumented_image_records_before_device_access(self):
        for field in ("diagnostic_trace", "diagnostic_worker_switch", "diagnostic_entry_switch"):
            records = {"c": {}, "rust": {field: True}}
            with patch.object(measure, "validate_pair", return_value=records):
                with self.subTest(field=field), self.assertRaisesRegex(
                        ValueError, "latency trace and same-image control images"):
                    measure.measure(SimpleNamespace(c=Path("unused"), rust=Path("unused")))

    def test_cli_rejects_out_of_range_and_unaligned_padding_without_building(self):
        for amount in ("-4", "2", "2049"):
            with self.subTest(amount=amount):
                result = subprocess.run(
                    [sys.executable, str(build.HERE / "build.py"), "link",
                     "--tree", "/unused", "--prefix", "xtensa-", "--out", "/unused-out",
                     "--language", "c", "--baseline", "/unused-config",
                     "--layout-pad-bytes", amount],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("must be between 0 and 2048", result.stderr)

    def test_cli_accepts_padding_boundaries_without_building(self):
        for amount in ("0", "2048"):
            with self.subTest(amount=amount):
                result = subprocess.run(
                    [sys.executable, str(build.HERE / "build.py"), "link",
                     "--tree", "/unused", "--prefix", "xtensa-", "--out", "/unused-out",
                     "--language", "c", "--baseline", "/unused-config",
                     "--layout-pad-bytes", amount],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                # Valid arguments advance to filesystem validation in final_link.
                self.assertNotEqual(result.returncode, 2)
                self.assertNotIn("must be between 0 and 2048", result.stderr)


if __name__ == "__main__":
    unittest.main()
