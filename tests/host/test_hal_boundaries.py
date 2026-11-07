"""Repository-wide HAL organization and native-boundary regression controls."""
import hashlib
import importlib.util
from pathlib import Path
import re
import tempfile
import tomllib
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
                              '#[cfg(hal_provider = "simulation")] fn f() {}',
                              'let x = option_env!("NXRS_PLATFORM");'):
                with self.subTest(module=module, selection=selection):
                    path = self.write('hal/demo/src/' + module, selection)
                    self.assertTrue(self.errors())
                    path.unlink()

    def test_generated_imu_factory_is_fixed_and_maps_only_to_checked_in_constructors(self):
        self.write('hal/imu/src/platform/nuttx/mod.rs', '''
            include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));
            let device = match MODEL {
                SensorModel::Bmi270 => acquire::<Bmi270>(BINDING, config, sink),
                SensorModel::Lsm6dso => acquire::<Lsm6dso>(BINDING, config, sink),
            };
            Session::start(STACK_BYTES, cancel, sink, run)
        ''')
        self.write('hal/imu/build.rs', '''
            mod emit;
            if profile.binding("imu") == "nuttx" && os == "nuttx" {
                emit::write(&out.join("imu_config.rs"), binding)?;
            }
        ''')
        self.write('hal/imu/build/emit.rs', '''
            let model = match binding.model {
                nxrs_build_support::composition::ImuModel::Bmi270 => "SensorModel::Bmi270",
                nxrs_build_support::composition::ImuModel::Lsm6dso => "SensorModel::Lsm6dso",
            };
            let source = format!(
                "enum SensorModel {{ Bmi270, Lsm6dso }}\\n\\
                 const MODEL: SensorModel = {model};\\n\\
                 const BINDING: Binding<'static> = Binding {{ path: {path:?}, address: {address}, frequency: {frequency}, irq_path: {irq_path} }};\\n\\
                 const STACK_BYTES: usize = {stack_bytes};\\n",
                path = binding.path,
            );
        ''')
        self.write('hal/imu/src/sensors/bmi270.rs', 'pub struct Sensor;')
        self.write('hal/imu/src/sensors/lsm6dso.rs', 'pub struct Sensor;')
        self.assertEqual(CHECK.inspect_generated_imu_factory(self.root), [])

        build = self.root / 'hal/imu/build.rs'
        build.write_text(build.read_text().replace('"imu_config.rs"', 'profile_text'))
        self.assertTrue(any('fixed generated factory' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))
        build.write_text(build.read_text().replace('profile_text', '"imu_config.rs"'))
        nuttx = self.root / 'hal/imu/src/platform/nuttx/mod.rs'
        nuttx.write_text(nuttx.read_text().replace('SensorModel::Bmi270 =>', 'SensorModel::Unknown =>'))
        self.assertTrue(any('checked-in typed constructor' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))

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

    def test_shared_tls_sdk_is_still_limited_to_native_abi_glue(self):
        name = 'hal/tls/src/sdk/ffi/sdk.c'
        self.write('hal/ffi-boundaries.toml', f'[[bridge]]\npath="{name}"\nkind="abi"\nreason="PSA header constants/layout"\n')
        source = self.write(name, 'unsigned context_size(void) { return sizeof(void *); }')
        self.assertEqual(self.errors(), [])
        source.write_text('void *context(void) { return calloc(1, 32); }')
        self.assertTrue(any('ordinary I/O/runtime' in error for error in self.errors()))

    def test_driver_and_sdk_build_packages_retain_native_inventory_and_behavior_checks(self):
        for name in ('driver/linux/gio/src/platform/linux/ffi/gio.c',
                     'hal/detector/build-support/src/ffi/compat.hpp'):
            with self.subTest(path=name):
                source = self.write(name, 'unsigned sdk_constant(void) { return 1; }')
                self.assertTrue(any('documented ABI boundary' in error for error in self.errors()))
                self.write('hal/ffi-boundaries.toml', f'[[bridge]]\npath="{name}"\nkind="abi"\nreason="SDK header translation"\n')
                self.assertEqual(self.errors(), [])
                source.write_text('void *hidden_worker(void) { return pthread_create(0); }')
                self.assertTrue(any('ordinary I/O/runtime' in error for error in self.errors()))
                source.unlink()
                self.assertTrue(any('stale native boundary' in error for error in self.errors()))
                self.write('hal/ffi-boundaries.toml', '')

    def test_driver_and_sdk_glue_cannot_move_outside_the_owning_source_module(self):
        for name in ('driver/linux/gio/ffi/gio.c', 'hal/detector/build-support/compat.hpp'):
            with self.subTest(path=name):
                source = self.write(name, 'unsigned sdk_constant(void) { return 1; }')
                self.write('hal/ffi-boundaries.toml', f'[[bridge]]\npath="{name}"\nkind="abi"\nreason="SDK header translation"\n')
                self.assertTrue(any('beside its owning Rust provider' in error for error in self.errors()))
                source.unlink()
                self.write('hal/ffi-boundaries.toml', '')

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


# Structural checks complement compilation and the architecture audit. They do
# not infer runtime capabilities or replace Cargo dependency resolution.
def code(text):
    return re.sub(r'//[^\n]*|/\*.*?\*/', '', text, flags=re.S)


def exports(text):
    return re.findall(r'\bpub(?:\([^)]*\))?\s+use\s+([^;]+);', code(text))


def wiring_errors(root, selector):
    errors = []
    root = code(root)
    selector = code(selector)
    for export in exports(root):
        if re.search(r'\b(?:platform|selected)::', export) and '*' in export:
            errors.append('crate root must name its backend exports')
    if re.search(r'\bpub(?:\([^)]*\))?\s+mod\s+platform\b', root):
        errors.append('platform namespace must remain private')
    if re.search(r'#\s*\[\s*path\s*=\s*"platform/', root):
        errors.append('injected provider sources belong in test_support.rs')
    selected = exports(selector)
    named = r'selected::(?:[A-Za-z_]\w*|\{\s*[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*\s*,?\s*\})'
    if not selected or any(not re.fullmatch(named, item.strip()) for item in selected):
        errors.append('selector must export named selected items only')
    if not re.search(r'\bmod\s+selected\s*;', selector):
        errors.append('selector must select a source module')
    if re.search(r'\b(?:fn|impl|struct|enum|trait)\b|\bmod\s+\w+\s*\{', selector):
        errors.append('selector must not implement behavior')
    return errors


def capabilities(root):
    workspace = tomllib.loads((root / 'Cargo.toml').read_text())['workspace']
    for member in workspace['members']:
        directory = root / member
        manifest = tomllib.loads((directory / 'Cargo.toml').read_text())
        metadata = manifest.get('package', {}).get('metadata', {}).get('nxrs', {})
        if metadata.get('kind') == 'hal-capability':
            yield directory


class WiringNegativeControls(unittest.TestCase):
    ROOT = 'mod api; mod platform; pub use api::*; pub use platform::open;'
    SELECTOR = '#[path = "unbound.rs"] mod selected; pub use selected::open;'

    def test_explicit_api_and_named_selected_exports_are_allowed(self):
        self.assertEqual(wiring_errors(self.ROOT, self.SELECTOR), [])
        self.assertEqual(wiring_errors(self.ROOT, self.SELECTOR + '\n#[cfg(hal_provider = "mock")] pub use selected::{Mock, FORMAT};'), [])

    def test_backend_globs_in_roots_are_rejected(self):
        for export in ('pub use platform::*;', 'pub use self::platform::{open, *};'):
            with self.subTest(export=export):
                self.assertIn('crate root must name its backend exports', wiring_errors(self.ROOT + export, self.SELECTOR))

    def test_selector_globs_and_unrelated_exports_are_rejected(self):
        for export in ('pub use selected::*;', 'pub use other::open;', 'pub use selected::{open, other::Type};'):
            with self.subTest(export=export):
                self.assertIn('selector must export named selected items only', wiring_errors(self.ROOT, 'mod selected;' + export))

    def test_selector_implementations_are_rejected(self):
        for body in ('fn open() {}', 'struct Placeholder;', 'impl Camera for Placeholder {}', 'mod fallback {}'):
            with self.subTest(body=body):
                self.assertIn('selector must not implement behavior', wiring_errors(self.ROOT, self.SELECTOR + body))

    def test_missing_selection_and_exports_are_rejected(self):
        self.assertTrue(wiring_errors(self.ROOT, ''))
        self.assertTrue(wiring_errors(self.ROOT, 'mod selected;'))
        self.assertTrue(wiring_errors(self.ROOT, 'pub use selected::open;'))

    def test_public_backend_namespace_and_root_injection_are_rejected(self):
        self.assertIn('platform namespace must remain private', wiring_errors('pub mod platform;', self.SELECTOR))
        self.assertIn('injected provider sources belong in test_support.rs', wiring_errors('#[path = "platform/nuttx/mod.rs"] mod witness;', self.SELECTOR))

    def test_comments_do_not_trigger_rules(self):
        self.assertEqual(wiring_errors(self.ROOT + '// pub use platform::*;\n', self.SELECTOR + '/* fn open() {} */'), [])

    def test_inventory_follows_workspace_metadata_not_a_named_hal_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Cargo.toml').write_text('[workspace]\nmembers=["hal/new-device","shared/data"]\n')
            for name, metadata in [('hal/new-device', '[package.metadata.nxrs]\nkind="hal-capability"\n'), ('shared/data', '')]:
                path = root / name
                path.mkdir(parents=True)
                (path / 'Cargo.toml').write_text('[package]\nname="fixture"\n' + metadata)
            self.assertEqual(list(capabilities(root)), [root / 'hal/new-device'])


class RepositoryHalOrganization(unittest.TestCase):
    def test_every_hal_has_explicit_api_and_selection_boundaries(self):
        directories = list(capabilities(ROOT))
        self.assertTrue(directories, 'HAL inventory is empty')
        for directory in directories:
            with self.subTest(hal=directory.name):
                root = directory / 'src/lib.rs'
                selector = directory / 'src/platform/mod.rs'
                self.assertTrue(root.is_file())
                self.assertTrue(selector.is_file())
                self.assertEqual(wiring_errors(root.read_text(), selector.read_text()), [])
                support = directory / 'src/test_support.rs'
                if support.exists():
                    self.assertRegex(code(root.read_text()), r'#\[cfg\(test\)\]\s*mod test_support;')
        print(f'HAL organization: {len(directories)} capabilities checked')


if __name__ == '__main__':
    unittest.main()
