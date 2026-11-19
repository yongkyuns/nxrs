"""Check cancellation and retry in the exact pinned ESP32-S3 I²S source."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
I2S = "arch/xtensa/src/common/espressif/esp_i2s.c"
DMA = "arch/xtensa/src/esp32s3/esp32s3_dma"
PATCHES = (
    "0126-espressif-i2s-rx-enqueue-ownership.patch",
    "0127-esp32s3-i2s-rx-interrupt-route.patch",
    "0128-esp32s3-i2s-rx-stop-ownership.patch",
)


def extract(source, declaration):
    match = re.search(rf"(?m)^{declaration}[^;]*?\n\{{", source)
    if match is None:
        raise ValueError(f"missing native declaration {declaration}")
    return source[match.start():source.index("\n}", match.end()) + 2]


class EspI2sStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="nxrs-i2s-stop-")
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True).strip()

        def upstream(path):
            return subprocess.check_output(
                ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{path}"], text=True,
            )

        for name in (I2S, DMA + ".c", DMA + ".h"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(upstream(name))
        (root / "queue.h").write_text(upstream("include/nuttx/queue.h"))
        registers = upstream("arch/xtensa/src/esp32s3/hardware/esp32s3_dma.h")
        (root / "dma_registers.h").write_text(registers.replace(
            '#include "esp32s3_soc.h"', '#define DR_REG_GDMA_BASE 0\n#define BIT(n) (1u << (n))',
        ))

        def apply_patch(name):
            subprocess.run(
                ["git", "apply", "--whitespace=error", str(ROOT / "upstream" / "nuttx" / "patches" / name)],
                cwd=root, env={**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)},
                check=True, capture_output=True,
            )

        for patch in PATCHES[:-1]:
            apply_patch(patch)
        cls.binaries = {}
        for label in ("original", "patched"):
            flags = []
            definitions = []
            if label == "patched":
                apply_patch(PATCHES[-1])
                flags.append("-DRETRYABLE_RX_STOP")
                source = (root / I2S).read_text()
                (root / "rx_state.inc").write_text(extract(source, "enum i2s_rx_state_e") + ";\n")
                definitions.append(extract((root / (DMA + ".c")).read_text(), r"bool esp32s3_dma_is_idle\("))
                definitions.append(extract(source, r"static int i2s_stop_rx\("))
            source = (root / I2S).read_text()
            for name in ("i2s_rxdma_setup", "i2s_receive", "i2s_rx_worker", "i2s_ioctl"):
                definitions.append(extract(source, rf"static (?:int|void) {name}\("))
            (root / "rx_stop.inc").write_text("\n\n".join(definitions))
            executable = root / label
            build = subprocess.run(
                ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                 "-Wno-unused-variable", "-Wno-unused-function", *flags, "-I", str(root),
                 str(ROOT / "tests" / "host" / "esp_i2s_rx_stop.c"), "-o", str(executable)],
                capture_output=True, text=True,
            )
            if build.returncode:
                raise AssertionError(build.stderr)
            cls.binaries[label] = executable

    def run_case(self, case, original_ok=False):
        if case != 6:
            original = subprocess.run([self.binaries["original"], str(case)], capture_output=True, timeout=5)
            if original_ok:
                self.assertEqual(original.returncode, 0, original.stderr)
            else:
                self.assertNotEqual(original.returncode, 0, "the previous driver must reproduce the defect")
                self.assertIn(b"Assertion", original.stderr)
        patched = subprocess.run([self.binaries["patched"], str(case)], capture_output=True, timeout=5)
        self.assertEqual(patched.returncode, 0, patched.stderr)

    def test_stop_keeps_all_buffers_until_dma_parks_then_allows_reopen(self):
        self.run_case(0)

    def test_stop_retries_while_completion_or_submission_in_progress(self):
        self.run_case(1)
        self.run_case(2)

    def test_cancellation_callback_cannot_restart_or_reenter_drain(self):
        self.run_case(3)

    def test_normal_completion_and_try_enqueue(self):
        self.run_case(4, original_ok=True)
        self.run_case(5)

    def test_park_read_checks_the_requested_channel_direction(self):
        self.run_case(6)


if __name__ == "__main__":
    unittest.main()
