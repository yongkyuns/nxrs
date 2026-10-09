"""Fail-closed checks for both pinned NuttX upstream patch series."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/apply-nuttx-patches.py"
SERIES = {
    "nuttx": ("libs/libc/tls/Kconfig", ROOT / "upstream/nuttx/patches"),
    "nuttx-apps": ("wireless/bluetooth/nimble/Makefile.nimble",
                   ROOT / "upstream/nuttx-apps/patches"),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PatchSeriesTests(unittest.TestCase):
    def setUp(self):
        # Archive-like copies nested inside nxrs must not inherit its Git root.
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, component):
        marker, patch_dir = SERIES[component]
        upstream = ROOT / "external" / component
        source = self.base / component
        source.mkdir()
        patches = sorted(patch_dir.glob("*.patch"))
        self.assertTrue(patches)
        names = {marker}
        for patch in patches:
            names.update(line.split("\t", 2)[2] for line in subprocess.check_output(
                ["git", "apply", "--numstat", str(patch)], text=True,
            ).splitlines())
        original = {}
        for name in names:
            if not (upstream / name).is_file():
                continue  # New files must be created by the series.
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(upstream / name, destination)
            original[name] = digest(destination)
        return source, upstream, patches, original

    def apply(self, component, source):
        return subprocess.run(
            ["python3", str(TOOL), "--component", component,
             "--source", str(source), "--revision", "test-revision",
             "--record", str(self.base / f"{component}-patches.json")],
            text=True, capture_output=True,
        )

    def test_both_series_apply_to_copies_and_record_provenance(self):
        for component in SERIES:
            with self.subTest(component=component):
                source, upstream, patches, original = self.fixture(component)
                result = self.apply(component, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                ledger = json.loads((self.base / f"{component}-patches.json").read_text())
                self.assertEqual(ledger["component"], component)
                self.assertEqual(ledger["upstream_revision"], "test-revision")
                self.assertEqual(len(ledger["patches"]), len(patches))
                latest = {}
                for entry, patch in zip(ledger["patches"], patches):
                    self.assertEqual(entry["name"], patch.name)
                    self.assertEqual(entry["sha256"], digest(patch))
                    for name, hashes in entry["files"].items():
                        self.assertEqual(hashes["before"], latest.get(name, original.get(name)))
                        latest[name] = hashes["after"]
                for name, after in latest.items():
                    self.assertEqual(after, digest(source / name))
                for name, before in original.items():
                    self.assertEqual(digest(upstream / name), before)
                if component == "nuttx":
                    self.assertTrue((source / "arch/xtensa/src/esp32s3/esp32s3_camera.c").is_file())
                    self.assertIn("config TLS_GLOBAL_KEYS", (source / "libs/libc/tls/Kconfig").read_text())
                    self.assertIn("return -EINVAL;", (source / "arch/xtensa/src/esp32s3/esp32s3_spiram.c").read_text())
                    self.assertTrue((source / "boards/xtensa/esp32s3/esp32s3-devkit/src/esp32s3_freenove_userled.c").is_file())

    def test_rejects_reapplication_without_mutation(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        witness = source / "arch/xtensa/src/esp32s3/esp32s3_ble.c"
        before = digest(witness)
        result = self.apply("nuttx", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already applied", result.stderr)
        self.assertEqual(digest(witness), before)

    def test_rejects_incompatible_source_without_mutation(self):
        source, _, _, _ = self.fixture("nuttx")
        witness = source / "arch/xtensa/src/esp32s3/esp32s3_ble.c"
        witness.write_text(witness.read_text().replace(
            "#include <nuttx/wqueue.h>", "#include <nuttx/different.h>"))
        before = digest(witness)
        result = self.apply("nuttx", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible", result.stderr)
        self.assertEqual(digest(witness), before)
        self.assertFalse((self.base / "nuttx-patches.json").exists())


if __name__ == "__main__":
    unittest.main()
