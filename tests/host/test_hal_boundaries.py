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
        for module in ('api.rs', 'worker.rs', 'controller.rs', 'providers/udp.rs', 'gatt/client.rs'):
            for selection in ('#[cfg(target_os = "linux")] fn f() {}',
                              '#[cfg(unix)] fn f() {}',
                              'let x = cfg!(nxrs_provider = "nuttx");',
                              'let x = option_env!("NXRS_PLATFORM");'):
                with self.subTest(module=module, selection=selection):
                    path = self.write('hal/demo/src/' + module, selection)
                    self.assertTrue(self.errors())
                    path.unlink()

    def test_platform_wiring_and_ordinary_test_selection_are_allowed(self):
        self.write('hal/demo/src/lib.rs', '#[cfg(target_os = "linux")] mod selected;')
        self.write('hal/demo/src/worker.rs', '#[cfg(test)] mod tests; // no target_os selection\n')
        self.assertEqual(self.errors(), [])

    def test_unlisted_native_sources_and_stale_entries_fail(self):
        path = self.write('hal/demo/ffi/nuttx/glue.c', 'int device(void) { return 0; }')
        self.assertTrue(self.errors())
        self.write('hal/ffi-boundaries.toml', '[[bridge]]\npath="hal/demo/ffi/nuttx/glue.c"\nkind="abi"\nreason="Target ioctl ABI"\n')
        self.assertEqual(self.errors(), [])
        path.unlink()
        self.assertTrue(self.errors())

    def test_scalar_bridge_cannot_hide_ordinary_posix_or_runtime_behavior(self):
        self.write('hal/ffi-boundaries.toml', '[[bridge]]\npath="hal/demo/ffi/glue.c"\nkind="abi"\nreason="Target ioctl ABI"\n')
        for call in ('open', 'write', 'ftruncate', 'socket', 'pthread_create', 'calloc'):
            with self.subTest(call=call):
                self.write('hal/demo/ffi/glue.c', f'int wrapped(void) {{ return {call}(0); }}')
                self.assertTrue(self.errors())

    def test_existing_behavior_is_frozen_until_its_exception_is_removed(self):
        source = 'void legacy(void) { retry(); }\n'
        path = self.write('hal/demo/ffi/legacy.c', source)
        digest = hashlib.sha256(source.encode()).hexdigest()
        self.write('hal/ffi-boundaries.toml', f'[[bridge]]\npath="hal/demo/ffi/legacy.c"\nkind="migration"\nreason="Move retry state into Rust"\nsha256="{digest}"\n')
        self.assertEqual(self.errors(), [])
        path.write_text(source + 'void more_behavior(void) {}\n')
        self.assertTrue(self.errors())
        path.unlink()
        self.write('hal/ffi-boundaries.toml', '')
        self.assertEqual(self.errors(), [])


if __name__ == '__main__':
    unittest.main()
