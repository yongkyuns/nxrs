"""Active build contracts; no SDK or device access."""
import contextlib
import io
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path

import build
with patch.object(sys, "path", sys.path.copy()):
    import measure


class BuildTests(unittest.TestCase):
    def test_no_psram_preparation_changes_only_the_memory_controller_option(self):
        original = [("--enable", "CONFIG_DEV_GPIO"),
                    ("--enable", "CONFIG_ESP32S3_GPIO_IRQ"),
                    ("--enable", "CONFIG_EXAMPLES_NXRS_BENCH"),
                    ("--disable", "CONFIG_EXAMPLES_NXRS_STD_APP")]
        self.assertEqual(build.preparation_options(), original)
        self.assertEqual(build.preparation_options(True),
                         original + [("--disable", "CONFIG_ESP32S3_SPIRAM")])

    def test_default_staging_and_flags_remain_unchanged(self):
        self.assertEqual(build.make_variables("compiler-", "c", []), [
            "CROSSDEV=compiler-", "ESPTOOL_BINDIR=.", "NXRS_APP_COMMAND=sq_c",
            "NXRS_APP_PRIORITY=100", "NXRS_APP_STACKSIZE=8192",
            "NXRS_TARGET_C_SOURCE=", "NXRS_TARGET_C_FLAGS=-std=c11 -O2",
        ])
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "apps/examples/nxrs_bench"
            build.stage_native_app(Path(folder), "c")
            self.assertFalse((app / "layout_padding.c").exists())
            self.assertFalse((app / "perfmon.c").exists())
            self.assertFalse((app / "sq_layout_padding.h").exists())

    def test_plain_inputs_exclude_removed_investigation_helpers(self):
        names = {path.name for path in build.source_inputs("rust")}
        self.assertTrue({"trace.c", "entry_switch.c", "worker_switch.c",
                         "layout_padding.c", "perfmon.c"}.isdisjoint(names))

    def test_measure_rejects_old_instrumented_image_records_before_device_access(self):
        for field, value in (("diagnostic_trace", True), ("diagnostic_worker_switch", True),
                             ("diagnostic_entry_switch", True), ("diagnostic_perfmon", True),
                             ("diagnostic_hot_iram", True), ("diagnostic_layout_padding_bytes", 0)):
            records = {"c": {}, "rust": {field: value}}
            with patch.object(measure, "validate_pair", return_value=records), \
                    patch.object(measure.subprocess, "run") as run, \
                    patch.object(measure, "open_serial") as serial:
                with self.subTest(field=field), self.assertRaisesRegex(
                        ValueError, "retired diagnostic images"):
                    measure.measure(SimpleNamespace(c=Path("unused"), rust=Path("unused")))
            run.assert_not_called()
            serial.assert_not_called()

    def test_cli_rejects_retired_and_conflicting_diagnostics_before_linking(self):
        for option, expected in ((["--perfmon"], "unrecognized arguments"),
                                 (["--hot-iram"], "unrecognized arguments"),
                                 (["--layout-pad-bytes", "0"], "unrecognized arguments"),
                                 (["--faults", "--pressure"], "require separate diagnostic images"),
                                 (["--pressure", "--faults"], "require separate diagnostic images")):
            argv = ["build.py", "link", "--tree", "/unused", "--prefix", "xtensa-",
                    "--out", "/unused-out", "--language", "c", "--baseline", "/unused-config",
                    *option]
            stderr = io.StringIO()
            with self.subTest(option=option), patch.object(sys, "argv", argv), \
                    patch.object(build, "final_link") as link, \
                    contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                build.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn(expected, stderr.getvalue())
            link.assert_not_called()


if __name__ == "__main__":
    unittest.main()
