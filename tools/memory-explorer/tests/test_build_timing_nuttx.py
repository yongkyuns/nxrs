"""Admission checks for reproducible native timing-witness builds."""

from pathlib import Path
import json
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build_timing_nuttx import pinned_revisions, run, source_identity, validate_configuration


class BuildEvidenceChecks(unittest.TestCase):
    def test_release_and_gitlink_pins_must_agree(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            expected = {"nuttx": "1" * 40, "apps": "2" * 40}
            for component, revision in (("nuttx", expected["nuttx"]), ("nuttx-apps", expected["apps"])):
                subprocess.run(["git", "-C", str(root), "update-index", "--add", "--cacheinfo",
                                "160000," + revision + ",external/" + component], check=True)
            manifest = root / "upstream/nuttx/release.json"
            manifest.parent.mkdir(parents=True)
            release = {"schema": 1, "version": "1.2.3", "revision": expected["nuttx"]}
            manifest.write_text(json.dumps(release))
            self.assertEqual(pinned_revisions(root), expected)
            release["revision"] = "3" * 40
            manifest.write_text(json.dumps(release))
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                pinned_revisions(root)

    def test_wrong_revision_is_rejected_before_source_inspection(self):
        with patch("build_timing_nuttx.subprocess.check_output", return_value="wrong\n") as command:
            with self.assertRaisesRegex(RuntimeError, "Expected pinned source"):
                source_identity(Path("source"), "expected")
            self.assertEqual(command.call_count, 1)

    def test_validates_enabled_disabled_and_numeric_settings(self):
        settings = {"TRACE": "y", "SMP": "n", "HIDDEN": "n", "CAPACITY": "32768", "MASK": "0x0"}
        text = "CONFIG_TRACE=y\n# CONFIG_SMP is not set\nCONFIG_CAPACITY=0x8000\nCONFIG_MASK=0\n"
        checks = validate_configuration(text, settings)
        self.assertEqual(set(checks), set(settings))
        self.assertEqual(checks["CAPACITY"]["effective"], "0x8000")
        self.assertFalse(checks["HIDDEN"]["entry_present"])

    def test_rejects_reenabled_feature_or_wrong_numeric_value(self):
        for text, settings in (("CONFIG_SMP=y", {"SMP": "n"}),
                               ("CONFIG_TRACE=m", {"TRACE": "y"}),
                               ("CONFIG_CAPACITY=4096", {"CAPACITY": "32768"}),
                               ("CONFIG_MASK=0x1", {"MASK": "0x0"})):
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, "did not resolve"):
                validate_configuration(text, settings)

    def test_tracked_dirty_sources_are_rejected_before_untracked_discovery(self):
        with patch("build_timing_nuttx.subprocess.check_output",
                   side_effect=["abc\n", "drivers/note/note_driver.c\n"]) as command:
            with self.assertRaisesRegex(RuntimeError, "Tracked source modifications"):
                source_identity(Path("source"), "abc")
            self.assertEqual(command.call_count, 2)

    def test_untracked_fixture_is_explicitly_recorded(self):
        with patch("build_timing_nuttx.subprocess.check_output",
                   side_effect=["abc\n", "", "examples/memory_timing/timing_main.c\n"]):
            result = source_identity(Path("source"), "abc")
        self.assertTrue(result["tracked_sources_clean"])
        self.assertEqual(result["untracked_paths_before_fixture_install"],
                         ["examples/memory_timing/timing_main.c"])

    def test_fresh_configuration_supplies_apps_relative_to_nuttx(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            nuttx = root / "source" / "nuttx"
            apps = root / "external" / "apps"
            nuttx.mkdir(parents=True)
            apps.mkdir(parents=True)
            commands = []

            def configure(command, *, cwd, **unused):
                commands.append(command)
                self.assertEqual(cwd, nuttx)
                if len(commands) == 1:
                    self.assertEqual(command[1], "tools/configure.sh")
                    production = Path(__file__).resolve().parents[3] / "shared/acquisition/src/platform/nuttx/ffi/trace.c"
                    self.assertEqual((apps / "examples/memory_timing/acquisition_trace.c").read_bytes(), production.read_bytes())
                    supplied = Path(command[command.index("-a") + 1])
                    self.assertFalse(supplied.is_absolute())
                    self.assertEqual((cwd / supplied).resolve(), apps)
                    (nuttx / ".config").write_text("CONFIG_ARCH_BOARD_MPS2_AN521=y\n")
                else:
                    self.assertEqual(command, ["make", "olddefconfig"])
                    raise RuntimeError("fresh configure reached olddefconfig")

            args = SimpleNamespace(nuttx=nuttx, apps=apps, output=root / "evidence",
                                   qemu_timer_clock=True)
            with patch("build_timing_nuttx.pinned_revisions", return_value={"nuttx": "1" * 40, "apps": "2" * 40}), \
                 patch("build_timing_nuttx.source_identity", return_value={}), \
                 patch("build_timing_nuttx.subprocess.run", side_effect=configure):
                with self.assertRaisesRegex(RuntimeError, "fresh configure reached olddefconfig"):
                    run(args)
            self.assertEqual(len(commands), 2)


if __name__ == "__main__":
    unittest.main()
