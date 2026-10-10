from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import build
with patch.object(sys, "path", sys.path.copy()):
    import measure
from test_publish import report as publish_report_fixture
from publish import public_report


class PressureBuildTests(unittest.TestCase):
    def test_pressure_fixture_stages_for_both_native_targets(self):
        for language in ("c", "rust"):
            with self.subTest(language=language):
                normal = build.source_inputs(language)
                self.assertNotIn("pressure.c", {path.name for path in normal})

                sources = build.source_inputs(language, pressure=True)
                self.assertIn("pressure.c", {path.name for path in sources})
                with tempfile.TemporaryDirectory() as folder:
                    staged, native = build.stage_native_app(Path(folder), language, pressure=True)
                    self.assertIn("pressure.c", {path.name for path in staged})
                    self.assertIn("pressure.c", native)
                    app = Path(folder) / "apps/examples" / (
                        "nxrs_std_app" if language == "rust" else "nxrs_bench")
                    self.assertEqual((app / "pressure.c").read_bytes(),
                                     (build.HERE / "pressure.c").read_bytes())

    def test_pressure_wrappers_are_unique_exact_and_diagnostic_only(self):
        self.assertEqual(len(build.PRESSURE_WRAPS), len(set(build.PRESSURE_WRAPS)))
        normal = build.make_variables("xtensa-elf-", "c", [])
        pressure = build.make_variables("xtensa-elf-", "c", [], pressure=True)
        self.assertFalse(any("pressure.c" in item or "--wrap=" in item or
                             "EXTRALINKCMDS=" in item for item in normal))
        link = next(item for item in pressure if item.startswith("EXTRALINKCMDS="))
        self.assertEqual(link.split("=", 1)[1].split(),
                         ["--wrap=" + name for name in build.PRESSURE_WRAPS])

    def test_measure_rejects_pressure_images_before_board_access(self):
        records = publish_report_fixture()["builds"]
        for record in records.values():
            record["diagnostic_pressure"] = True
        args = SimpleNamespace(c=Path("unused"), rust=Path("unused"), source="messages",
                               recovery_events=0)
        with patch.object(measure, "validate_pair", return_value=records), \
                patch.object(measure.subprocess, "run") as run, \
                patch.object(measure, "open_serial") as serial, \
                patch.object(measure, "load") as load, self.assertRaises(ValueError):
            measure.measure(args)
        run.assert_not_called()
        serial.assert_not_called()
        load.assert_not_called()

    def test_public_report_rejects_pressure_diagnostic(self):
        report = publish_report_fixture()
        report["builds"]["rust"]["diagnostic_pressure"] = True
        with self.assertRaises(ValueError):
            public_report([report])


if __name__ == "__main__":
    unittest.main()
