"""Run the pinned ESP32-S3 RX IRQ functions against an injected DMA source."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
I2S = "arch/xtensa/src/common/espressif/esp_i2s.c"
DMA = "arch/xtensa/src/esp32s3/hardware/esp32s3_dma.h"
PATCH = "0127-esp32s3-i2s-rx-interrupt-route.patch"


class EspI2sIrqTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="nxrs-i2s-irq-")
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True).strip()
        source, registers = [
            subprocess.check_output(
                ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{path}"], text=True,
            )
            for path in (I2S, DMA)
        ]
        (root / I2S).parent.mkdir(parents=True)
        (root / I2S).write_text(source)
        (root / "dma_registers.h").write_text(registers.replace(
            '#include "esp32s3_soc.h"', '#define DR_REG_GDMA_BASE 0\n#define BIT(n) (1u << (n))',
        ))

        cls.binaries = {}
        for label in ("original", "patched"):
            if label == "patched":
                subprocess.run(
                    ["git", "apply", "--whitespace=error", str(ROOT / "upstream/nuttx/patches" / PATCH)],
                    cwd=root, env={**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)},
                    check=True, capture_output=True,
                )
            source = (root / I2S).read_text()
            functions = []
            for name in ("i2s_rx_channel_start", "i2s_rx_channel_stop", "i2s_interrupt", "i2s_dma_setup"):
                start = re.search(rf"(?m)^static (?:int|void) {name}\([^;]*?\)\n\{{", source)
                if start is None:
                    raise ValueError(f"missing native function {name}")
                functions.append(source[start.start():source.index("\n}\n", start.end()) + 3])
            (root / "i2s_rx_irq.inc").write_text("\n".join(functions))
            executable = root / label
            build = subprocess.run(
                ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                 "-Wno-unused-function", "-Wno-unused-variable",
                 "-I", str(root), str(ROOT / "tests/host/esp_i2s_rx_irq.c"),
                 "-o", str(executable)],
                capture_output=True, text=True,
            )
            if build.returncode:
                raise AssertionError(build.stderr)
            cls.binaries[label] = executable

    def check_cases(self, cases, original_ok):
        for case in cases:
            with self.subTest(case=case):
                original = subprocess.run([self.binaries["original"], str(case)], capture_output=True, timeout=5)
                if original_ok:
                    self.assertEqual(original.returncode, 0, original.stderr)
                else:
                    self.assertNotEqual(original.returncode, 0, "the original must reproduce the defect")
                    self.assertIn(b"Assertion", original.stderr)
                patched = subprocess.run([self.binaries["patched"], str(case)], capture_output=True, timeout=5)
                self.assertEqual(patched.returncode, 0, patched.stderr)

    def test_channel_operations_leave_the_cpu_route_enabled(self):
        self.check_cases((0, 1, 6), original_ok=False)

    def test_interrupt_uses_only_its_source_and_reads_rx_under_the_lock(self):
        self.check_cases((2, 3), original_ok=False)

    def test_rx_end_of_transfer_and_masked_interrupt(self):
        self.check_cases((4, 5), original_ok=True)


if __name__ == "__main__":
    unittest.main()
