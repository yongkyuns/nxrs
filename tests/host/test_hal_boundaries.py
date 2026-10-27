"""Negative controls for shared Rust selection and native implementation drift."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('architecture', ROOT / 'tools/check-architecture.py')
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class HalBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def errors(self):
        return CHECK.inspect_hal_boundaries(self.root)

    def test_shared_behavior_rejects_attribute_expression_and_environment_selection(self):
        for module in ('api.rs', 'worker.rs', 'controller.rs', 'replay/mod.rs', 'model/pico.rs', 'shared/station.rs',
                       'gatt/client.rs', 'gatt_driver.rs', 'gatt_backend.rs'):
            for selection in ('#[cfg(target_os = "linux")] fn f() {}',
                              '#[cfg(unix)] fn f() {}',
                              'let x = cfg!(nxrs_hal_platform = "nuttx");',
                              '#[cfg(hal_enabled)] fn f() {}',
                              '#[cfg(hal_mock)] fn f() {}',
                              'let x = option_env!("NXRS_PLATFORM");'):
                with self.subTest(module=module, selection=selection):
                    path = self.write('hal/demo/src/' + module, selection)
                    self.assertTrue(self.errors())
                    path.unlink()

    def test_platform_wiring_and_ordinary_test_selection_are_allowed(self):
        self.write('hal/demo/src/lib.rs', '#[cfg(target_os = "linux")] mod selected;')
        self.write('hal/demo/src/worker.rs', '#[cfg(test)] mod tests; // no target_os selection\n')
        self.assertEqual(self.errors(), [])

    def test_internal_c_protocol_is_rejected_but_sdk_callbacks_and_test_shims_are_allowed(self):
        for symbol in ('host_wifi_open', 'host_ble_scan_get', 'nxrs_host_wifi_open', 'nxrs_host_ble_scan_get',
                       'nxrs_ble_gatt_submit', 'nxrs_ble_peer_lookup_rust'):
            with self.subTest(symbol=symbol):
                path = self.write('hal/demo/src/platform/backend.rs',
                                  f'extern "C" {{ fn {symbol}(); }}')
                self.assertTrue(any('typed Rust' in error for error in self.errors()))
                path.unlink()
        self.write('tests/host/radio_shims/backend.rs',
                   'pub extern "C" fn nxrs_ble_gatt_submit() {}')
        self.write('hal/demo/src/platform/backend.rs',
                   'pub extern "C" fn nxrs_ble_sdk_complete() {}')
        self.assertEqual(self.errors(), [])

    def test_unlisted_native_sources_and_stale_entries_fail(self):
        path = self.write('hal/demo/src/platform/nuttx/ffi/glue.c', 'int device(void) { return 0; }')
        self.assertTrue(self.errors())
        self.write('hal/ffi-boundaries.toml', '[[bridge]]\npath="hal/demo/src/platform/nuttx/ffi/glue.c"\nkind="abi"\nreason="Target ioctl ABI"\n')
        self.assertEqual(self.errors(), [])
        path.unlink()
        self.assertTrue(self.errors())

    def test_retired_provider_tree_and_ambiguous_native_selection_are_rejected(self):
        for directory in ('providers', 'platform/native'):
            path = self.write('hal/demo/src/' + directory + '/mod.rs', 'fn implementation() {}')
            self.assertTrue(self.errors())
            path.unlink()
            path.parent.rmdir()

    def test_std_cannot_return_as_a_platform_or_shared_provider_directory(self):
        path = self.write('platform/profiles/std.toml', '[hal]\n')
        self.assertTrue(self.errors())
        path.unlink()
        path = self.write('hal/demo/src/platform/std/mod.rs', 'fn f() {}')
        self.assertTrue(self.errors())

    def test_parallel_ffi_tree_is_rejected_even_when_inventoried(self):
        self.write('hal/demo/ffi/nuttx/glue.c', 'int device(void) { return 0; }')
        self.write('hal/ffi-boundaries.toml', '[[bridge]]\npath="hal/demo/ffi/nuttx/glue.c"\nkind="abi"\nreason="Target ioctl ABI"\n')
        self.assertTrue(any('beside its owning Rust provider' in error for error in self.errors()))

    def test_scalar_bridge_cannot_hide_ordinary_posix_or_runtime_behavior(self):
        self.write('hal/ffi-boundaries.toml', '[[bridge]]\npath="hal/demo/src/platform/nuttx/ffi/glue.c"\nkind="abi"\nreason="Target ioctl ABI"\n')
        for call in ('open', 'write', 'ftruncate', 'socket', 'pthread_create', 'calloc', 'g_thread_try_new', 'g_usleep', 'clock_gettime'):
            with self.subTest(call=call):
                self.write('hal/demo/src/platform/nuttx/ffi/glue.c', f'int wrapped(void) {{ return {call}(0); }}')
                self.assertTrue(self.errors())

    def test_migration_exceptions_are_rejected_even_with_matching_snapshots(self):
        source = 'void legacy(void) { retry(); }\n'
        path = self.write('hal/demo/src/platform/nuttx/ffi/legacy.c', source)
        digest = hashlib.sha256(source.encode()).hexdigest()
        self.write('hal/ffi-boundaries.toml', f'[[bridge]]\npath="hal/demo/src/platform/nuttx/ffi/legacy.c"\nkind="migration"\nreason="Move retry state into Rust"\nsha256="{digest}"\n')
        self.assertTrue(self.errors())
        path.write_text(source + 'void more_behavior(void) {}\n')
        self.assertTrue(self.errors())
        path.unlink()
        self.write('hal/ffi-boundaries.toml', '')
        self.assertEqual(self.errors(), [])


if __name__ == '__main__':
    unittest.main()
