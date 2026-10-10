"""Guard the native HAL sources with the platform compiler defines."""
from pathlib import Path
import re
import unittest

HERE = Path(__file__).resolve().parent


class HalPlatformGuardTests(unittest.TestCase):
    def test_c_hal_native_branches_use_actual_target_macros(self):
        nuttx = (HERE / "hal_nuttx.c").read_text()
        zephyr = (HERE / "hal_zephyr.c").read_text()
        self.assertRegex(nuttx, r"(?m)^#if defined\(__NuttX__\)$")
        self.assertRegex(zephyr, r"(?m)^#if defined\(__ZEPHYR__\)$")
        self.assertNotIn("CONFIG_ZEPHYR", zephyr)


if __name__ == "__main__":
    unittest.main()
