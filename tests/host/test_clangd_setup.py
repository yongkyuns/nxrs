"""Editor configuration must quiet inactive targets without hiding active errors."""

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "setup_clangd", Path(__file__).resolve().parents[2] / "tools/setup-clangd.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class ClangdSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "hal/ble/src/platform/macos/ffi/scan.m"
        self.source.parent.mkdir(parents=True)
        self.source.touch()

    def generate(self, *args):
        with mock.patch.object(setup, "ROOT", self.root), \
             mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(sys, "argv", ["setup-clangd.py", *args]), \
             mock.patch.object(setup, "output", side_effect=["/tool/clang", "/SDK"]), \
             mock.patch("builtins.print"):
            setup.main()
        config = (self.root / ".clangd").read_text().split("\n", 1)[1]
        return [json.loads(fragment) for fragment in config.split("\n---\n")]

    def test_native_arc_and_sdk_are_configured(self):
        self.generate()
        entries = json.loads((self.root / "target/clangd/compile_commands.json").read_text())
        self.assertEqual(entries[0]["file"], str(self.source))
        self.assertEqual(entries[0]["arguments"][:3], ["/tool/clang", "-isysroot", "/SDK"])
        self.assertIn("-fobjc-arc", entries[0]["arguments"])

    def test_suppression_only_matches_inactive_sources(self):
        config = self.generate()
        condition = config[-1]["If"]

        def suppressed(path):
            return any(re.fullmatch(p, path) for p in condition["PathMatch"]) and not any(
                re.fullmatch(p, path) for p in condition["PathExclude"]
            )

        self.assertTrue(suppressed("hal/ble/src/platform/nuttx/ffi/nxrs_ble_scan.c"))
        self.assertTrue(suppressed("hal/ble/src/platform/linux/ffi/scan.c"))
        self.assertFalse(suppressed("hal/ble/src/platform/macos/ffi/scan.m"))
        self.assertFalse(suppressed("tests/service-qualification/runtime.c"))
        self.assertFalse(suppressed("hal/ble/src/platform/std/scan.h"))

    def test_imported_target_retains_flags_and_regains_diagnostics(self):
        source = self.root / "hal/ble/src/platform/nuttx/ffi/nxrs_ble_scan.c"
        database = self.root / "build/compile_commands.json"
        database.parent.mkdir()
        flags = ["arm-none-eabi-gcc", "-D__NuttX__", "-Iinclude", "-c", str(source)]
        database.write_text(json.dumps([{
            "directory": ".", "file": str(source), "arguments": flags,
        }]))
        config = self.generate("--compile-commands", str(database))
        self.assertIn(re.escape(str(source.relative_to(self.root))), config[-1]["If"]["PathExclude"])
        entries = json.loads((self.root / "target/clangd/compile_commands.json").read_text())
        self.assertEqual(entries[0]["arguments"], flags)
        self.assertEqual(entries[0]["directory"], str(database.parent))


if __name__ == "__main__":
    unittest.main()
