"""Exercise the real NuttX GATT adapter with deterministic native callbacks."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "hal/ble/ffi/nuttx"


class NativeGattTests(unittest.TestCase):
    def fixture(self, directory):
        for name in ("nuttx/config.h", "nuttx/clock.h", "nuttx/irq.h", "nuttx/wqueue.h",
                     "nuttx/wireless/bluetooth/bt_core.h", "nuttx/wireless/bluetooth/bt_gatt.h",
                     "nuttx/wireless/bluetooth/bt_hci.h", "wireless/bluetooth/bt_conn.h",
                     "wireless/bluetooth/bt_att.h"):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("/* Native definitions supplied by the witness. */\n")

    def command(self, directory):
        return [os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-I", str(directory), "-I", str(BRIDGE), "-I", str(BRIDGE.parent / "native"),
                str(ROOT / "tests/nuttx-std/bt-gatt-adapter.c")]

    def test_native_lifetime_discovery_read_and_recovery(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            self.fixture(directory)
            binary = directory / "gatt-adapter"
            subprocess.run([*self.command(directory), "-o", str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_parallel_lpwork_is_rejected_not_silently_unsafe(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            self.fixture(directory)
            result = subprocess.run([*self.command(directory), "-DCONFIG_SCHED_LPNTHREADS=2",
                                     "-fsyntax-only"], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("GATT requires serialized LPWORK", result.stderr)


if __name__ == "__main__":
    unittest.main()
