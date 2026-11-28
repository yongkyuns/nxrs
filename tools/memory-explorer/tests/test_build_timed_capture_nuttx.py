"""Reject source/config mismatches before modifying disposable native trees."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build_timed_capture_nuttx import run, validate_apps_directory


class NativeAppsAdmission(unittest.TestCase):
    def test_wrong_existing_apps_path_changes_nothing(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            nuttx, apps, wrong = [root / name for name in ("nuttx", "apps", "wrong")]
            for directory in (nuttx, apps, wrong):
                directory.mkdir()
                (directory / "Makefile").write_text("# retained source\n")
            config = nuttx / ".config"
            initial = 'CONFIG_ARCH_BOARD_MPS2_AN521=y\nCONFIG_APPS_DIR="../wrong"\n'
            config.write_text(initial)
            args = SimpleNamespace(nuttx=nuttx, apps=apps, output=root / "evidence")
            with patch("build_timed_capture_nuttx.source_identity") as identities, \
                 patch("build_timed_capture_nuttx.subprocess.run") as commands:
                with self.assertRaisesRegex(RuntimeError, "differs from --apps"):
                    run(args)
            identities.assert_not_called()
            commands.assert_not_called()
            self.assertEqual(config.read_text(), initial)
            self.assertFalse(args.output.exists())
            self.assertEqual(sorted(path.name for path in apps.iterdir()), ["Makefile"])
            self.assertEqual((apps / "Makefile").read_text(), "# retained source\n")

    def test_accepts_literal_relative_absolute_and_default_apps_paths(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            nuttx, apps = root / "nuttx", root / "apps"
            nuttx.mkdir()
            apps.mkdir()
            (apps / "Makefile").write_text("# native apps\n")
            for setting in ('CONFIG_APPS_DIR="../apps"', f'CONFIG_APPS_DIR="{apps.resolve().as_posix()}"', ""):
                with self.subTest(setting=setting):
                    self.assertEqual(validate_apps_directory(nuttx, apps, setting), apps.resolve())

    def test_ambiguous_or_interpolated_apps_paths_fail_without_evaluation(self):
        with TemporaryDirectory() as temporary:
            nuttx, apps = Path(temporary) / "nuttx", Path(temporary) / "apps"
            for setting in ('CONFIG_APPS_DIR="$(unexpected)"',
                            'CONFIG_APPS_DIR="../apps"\nCONFIG_APPS_DIR="../elsewhere"',
                            'CONFIG_APPS_DIR="../apps with spaces"'):
                with self.subTest(setting=setting), self.assertRaises(RuntimeError):
                    validate_apps_directory(nuttx, apps, setting)


if __name__ == "__main__":
    unittest.main()
