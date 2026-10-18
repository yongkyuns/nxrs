"""SDK-backed fake-object tests of the actual bridges, never host RF tests."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(sys.platform == 'darwin', 'requires the macOS Objective-C SDK')
class MacosRadioNativeTests(unittest.TestCase):
    def exercise(self, source, frameworks):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'radio-native'
            compiled = subprocess.run([
                'xcrun', '--sdk', 'macosx', 'clang', '-Wall', '-Wextra', '-Werror',
                '-fobjc-arc', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                str(ROOT / 'tests/host' / source), '-o', str(binary),
                '-framework', 'Foundation',
                *(value for framework in frameworks for value in ('-framework', framework)),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_wifi_ownership_and_failed_connection_rollback(self):
        self.exercise('macos_wifi_native.m', ['CoreWLAN', 'CoreLocation'])

    def test_ble_scan_generation_filtering_bounds_and_quiescent_release(self):
        self.exercise('macos_ble_scan.m', ['CoreBluetooth'])

    def test_ble_gatt_callbacks_ordering_value_bounds_and_cancel_barriers(self):
        self.exercise('macos_ble_gatt.m', ['CoreBluetooth'])


if __name__ == '__main__':
    unittest.main()
