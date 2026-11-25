"""Check the exact pinned NuttX receive-submission code before and after the patch."""

import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "arch/xtensa/src/common/espressif/esp_i2s.c"
DMA = "arch/xtensa/src/esp32s3/esp32s3_dma"
PATCH = "0126-espressif-i2s-rx-enqueue-ownership.patch"


def function(source, name):
    match = re.search(rf"(?m)^(?:static int|uint32_t) {name}\([^;]*?\)\n\{{", source)
    if match is None:
        raise ValueError(f"missing native function {name}")
    return source[match.start():source.index("\n}\n", match.end()) + 3]


class EspI2sReceiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="nxrs-i2s-rx-")
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        series = runpy.run_path(str(ROOT / "tools" / "apply-nuttx-patches.py"))["SERIES"]["nuttx"]
        assert series.count(PATCH) == 1
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True).strip()
        for name in (SOURCE, DMA + ".c", DMA + ".h"):
            actual = subprocess.check_output(
                ["git", "-C", str(ROOT / "external" / "nuttx"), "show", f"{revision}:{name}"], text=True,
            )
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(actual)
        (root / "nuttx").mkdir()
        (root / "nuttx" / "config.h").touch()

        cls.binaries = {}
        for label in ("original", "patched"):
            if label == "patched":
                subprocess.run(
                    ["git", "apply", "--whitespace=error", str(ROOT / "upstream" / "nuttx" / "patches" / PATCH)],
                    cwd=root, env={**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)},
                    check=True, capture_output=True,
                )
            source_code = (root / SOURCE).read_text()
            dma_code = (root / (DMA + ".c")).read_text()
            (root / "i2s_rx.inc").write_text(
                function(dma_code, "esp32s3_dma_setup") + function(source_code, "i2s_rxdma_setup")
                + function(source_code, "i2s_receive"),
            )
            executable = root / label
            built = subprocess.run(
                ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter", "-Wno-unused-variable",
                 "-Wno-sign-compare", "-I", str(root), "-I", str((root / DMA).parent),
                 str(ROOT / "tests" / "host" / "esp_i2s_rx_enqueue.c"), "-o", str(executable)],
                capture_output=True, text=True,
            )
            if built.returncode:
                raise AssertionError(built.stderr)
            cls.binaries[label] = executable

    def witness(self, *cases, original_ok=False):
        for case in cases:
            with self.subTest(case=case):
                original = subprocess.run([self.binaries["original"], str(case)], capture_output=True, timeout=5)
                patched = subprocess.run([self.binaries["patched"], str(case)], capture_output=True, timeout=5)
                if original_ok:
                    self.assertEqual(original.returncode, 0, original.stderr)
                else:
                    self.assertNotEqual(original.returncode, 0, "original driver must reproduce the defect")
                    self.assertIn(b"Assertion", original.stderr)
                self.assertEqual(patched.returncode, 0, patched.stderr)

    def test_rejects_empty_or_incomplete_frames(self):
        self.witness(0, 1)

    def test_failed_lock_and_incomplete_descriptors_rollback(self):
        self.witness(2, 3, 4)

    def test_valid_queue_owns_the_aligned_receive_buffer(self):
        self.witness(5, original_ok=True)

    def test_dma_terminates_a_full_or_exhausted_descriptor_table(self):
        self.witness(6)
        self.witness(7, 8, original_ok=True)


if __name__ == "__main__":
    unittest.main()
