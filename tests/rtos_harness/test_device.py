"""Hardware-free tests for neutral serial and complete restore helpers."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch
import sys
_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(_TESTS / "service-footprint"))

from rtos_harness import device
import evidence
import serial_io


class OpenSerialTests(unittest.TestCase):
    def test_retries_open_and_configures_the_first_success(self):
        with patch.object(device.os, "open", side_effect=[OSError("busy"), OSError("busy"), 17]) as open_, \
                patch.object(device.time, "monotonic", return_value=10), \
                patch.object(device.time, "sleep") as sleep, \
                patch.object(device, "set_serial") as configure:
            self.assertEqual(device.open_serial("PORT"), 17)

        self.assertEqual(open_.call_args_list, [
            call("PORT", device.os.O_RDWR | device.os.O_NOCTTY | device.os.O_NONBLOCK),
        ] * 3)
        self.assertEqual(sleep.call_args_list, [call(0.25), call(0.25)])
        configure.assert_called_once_with(17)

    def test_open_timeout_propagates_without_sleeping_forever(self):
        with patch.object(device.os, "open", side_effect=OSError("missing port")), \
                patch.object(device.time, "monotonic", side_effect=[0, 40]), \
                patch.object(device.time, "sleep") as sleep:
            with self.assertRaisesRegex(OSError, "missing port"):
                device.open_serial("PORT")
        sleep.assert_not_called()

    def test_configuration_failure_closes_the_open_descriptor(self):
        with patch.object(device.os, "open", return_value=17), \
                patch.object(device, "set_serial", side_effect=OSError("not a tty")), \
                patch.object(device.os, "close") as close:
            with self.assertRaisesRegex(OSError, "not a tty"):
                device.open_serial("PORT")
        close.assert_called_once_with(17)


class SerialConfigurationTests(unittest.TestCase):
    def test_existing_modules_reexport_the_neutral_helpers(self):
        self.assertIs(serial_io.set_serial, device.set_serial)
        self.assertIs(evidence.marker_rows, device.marker_rows)

    def test_set_serial_applies_the_existing_raw_115200_configuration(self):
        attrs = list(range(6))
        with patch.object(device.termios, "tcgetattr", return_value=attrs) as get, \
                patch.object(device.termios, "tcsetattr") as set_:
            device.set_serial(17)

        get.assert_called_once_with(17)
        self.assertEqual(attrs, [0, 0, device.termios.CS8 | device.termios.CREAD |
                                 device.termios.CLOCAL, 0, device.termios.B115200,
                                 device.termios.B115200])
        set_.assert_called_once_with(17, device.termios.TCSANOW, attrs)


class RestoreTests(unittest.TestCase):
    def test_restore_writes_with_preserved_header_then_verifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            results = [SimpleNamespace(stdout="write out", stderr="write err",
                                       check_returncode=lambda: None),
                       SimpleNamespace(stdout="verify out", stderr="verify err",
                                       check_returncode=lambda: None)]
            with patch.object(device.subprocess, "run", side_effect=results) as run:
                device.restore("FLASHER", "PORT", Path("backup.bin"), out)

            base = ["FLASHER", "--chip", "esp32s3", "--port", "PORT", "--baud", "460800"]
            self.assertEqual(run.call_args_list, [
                call(base + ["write_flash", "--flash_mode", "keep", "--flash_freq", "keep",
                             "--flash_size", "keep", "0x0", "backup.bin"],
                     capture_output=True, text=True, timeout=600),
                call(base + ["verify_flash", "0x0", "backup.bin"],
                     capture_output=True, text=True, timeout=600),
            ])
            self.assertEqual((out / "restore.log").read_text(), "write outwrite err")
            self.assertEqual((out / "restore-verify.log").read_text(), "verify outverify err")

    def test_restore_stops_if_write_fails(self):
        failed = SimpleNamespace(stdout="partial", stderr="failure",
                                 check_returncode=unittest.mock.Mock(
                                     side_effect=RuntimeError("restore write failed")))
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(device.subprocess, "run", return_value=failed) as run:
            with self.assertRaisesRegex(RuntimeError, "restore write failed"):
                device.restore("FLASHER", "PORT", Path("backup.bin"), Path(temporary))
            run.assert_called_once()
            self.assertEqual((Path(temporary) / "restore.log").read_text(), "partialfailure")
            self.assertFalse((Path(temporary) / "restore-verify.log").exists())


if __name__ == "__main__":
    unittest.main()
