"""Host reset requests must be confirmed against the explicitly chosen chip."""
import importlib.util
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("pico_bootloader", ROOT / "tools/pico-bootloader.py")
RESET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESET)


class PicoBootloaderTests(unittest.TestCase):
    def test_already_in_bootloader_does_not_open_serial(self):
        with patch.object(RESET, "in_bootloader", return_value=True), \
             patch.dict("sys.modules", {"serial": None}):
            RESET.enter_bootloader("explicit-port", "picotool", "0123456789ABCDEF")

    def test_touch_uses_1200_baud_and_confirms_identity(self):
        serial = types.SimpleNamespace(Serial=MagicMock(), SerialException=OSError)
        with patch.dict("sys.modules", {"serial": serial}), \
             patch.object(RESET, "in_bootloader", side_effect=[False, True]) as check:
            RESET.enter_bootloader("explicit-port", "picotool", "0123456789ABCDEF")
        serial.Serial.assert_called_once_with("explicit-port", baudrate=1200,
                                             timeout=0.2, write_timeout=0.2)
        self.assertFalse(serial.Serial.return_value.__enter__.return_value.dtr)
        self.assertEqual(check.call_args_list[-1].args, ("picotool", "0123456789ABCDEF"))

    def test_port_disappearance_is_success_only_after_rom_confirmation(self):
        serial = types.SimpleNamespace(Serial=Mock(side_effect=OSError("disconnected")),
                                       SerialException=OSError)
        with patch.dict("sys.modules", {"serial": serial}), \
             patch.object(RESET, "in_bootloader", side_effect=[False, True]):
            RESET.enter_bootloader("explicit-port", "picotool", "0123456789ABCDEF")
        with patch.dict("sys.modules", {"serial": serial}), \
             patch.object(RESET, "in_bootloader", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "did not enter BOOTSEL"):
                RESET.enter_bootloader("explicit-port", "picotool", "0123456789ABCDEF", timeout=0)

    def test_picotool_queries_exact_chip_without_force_reset(self):
        result = subprocess.CompletedProcess([], 0, stdout="target chip: RP2350")
        with patch.object(RESET.subprocess, "run", return_value=result) as run:
            self.assertTrue(RESET.in_bootloader("picotool", "0123456789ABCDEF"))
        self.assertEqual(run.call_args.args[0],
                         ["picotool", "info", "--ser", "0123456789ABCDEF"])
        for result in (subprocess.CompletedProcess([], 1, stdout="RP2350"),
                       subprocess.CompletedProcess([], 0, stdout="target chip: RP2040")):
            with patch.object(RESET.subprocess, "run", return_value=result):
                self.assertFalse(RESET.in_bootloader("picotool", "0123456789ABCDEF"))


if __name__ == "__main__":
    unittest.main()
