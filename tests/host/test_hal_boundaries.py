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
                              '#[cfg(platform = "simulation")] fn f() {}',
                              '#[cfg_attr(platform = "mock", allow(dead_code))] fn f() {}',
                              'let x = cfg!(platform = "simulation");',
                              'let x = option_env!("CARGO_CFG_PLATFORM");',
                              'let x = option_env!("NXRS_PLATFORM");'):
                with self.subTest(module=module, selection=selection):
                    path = self.write('hal/demo/src/' + module, selection)
                    self.assertTrue(self.errors())
                    path.unlink()

    def test_platform_identifier_is_not_a_selection_flag(self):
        self.write('hal/demo/src/api.rs', '#[cfg(test)] fn platform() {}')
        self.assertEqual(self.errors(), [])

    def write_declarative_imu(self):
        self.write('platform/devices/imu/src/nuttx/mod.rs', '''
            include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));
            mod implementations {
                type Bmi270 = nxrs_bmi270::Bmi270<Bus, Delay>;
                type Lsm6dso = nxrs_lsm6dso::Lsm6dso<Bus>;
            }
            macro_rules! declare_device {
                static $name: Instance<$sensor, $resources> = Instance::new(
                    |trace| binding.open(trace), |bus, settings| {
                    <$sensor>::from_interface(bus.try_into()?, Delay, settings)
                });
            }
        ''')
        self.write('hal/imu/src/platform/nuttx/instance.rs', """
            device::Device::open(config, self.irq_path, trace, &self.state, open, initialize);
            state: DeviceState,
            storage: Mutex<Option<device::Device<S>>>,
            Worker::start_guarded("imu-acquire", self.stack_bytes, cancel, sink, retained, Event::Failed, run)
        """)
        self.write('platform/devices/imu/build.rs', '''
            fn main() {
                nxrs_build_support::configure_hal().unwrap_or_else(|error| panic!("{error}"));
            }
        ''')
        self.write('platform/devices/imu/Cargo.toml', '''
            [package]
            name = "nxrs-imu"
            [package.metadata.nxrs]
            bindings = ["bindings.toml"]
        ''')
        self.write('platform/devices/imu/bindings.toml', '''
            version = 2
            [[binding]]
            compatible = "bosch,bmi270"
            bus = "i2c"
            implementation = "Bmi270"
        ''')

    def test_sensor_generation_uses_declarative_package_inputs(self):
        self.write_declarative_imu()
        self.assertEqual(CHECK.inspect_generated_imu_factory(self.root), [])
        build = self.root / 'platform/devices/imu/build.rs'
        generic = build.read_text()
        build.write_text(generic.replace('configure_hal()', 'configure_hal_with(emit)'))
        self.assertTrue(any('generic shared-preparation' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))
        build.write_text(generic)
        emitter = self.write('platform/devices/imu/build/emit.rs', 'fn emit() {}')
        self.assertTrue(any('host generation belongs' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))
        emitter.unlink()
        self.assertEqual(CHECK.inspect_generated_imu_factory(self.root), [])

    def test_generated_sensor_include_matches_its_resource_contract(self):
        self.write_declarative_imu()
        provider = self.root / 'platform/devices/imu/src/nuttx/mod.rs'
        provider.write_text(provider.read_text().replace('imu_config.rs', 'other.rs'))
        self.assertTrue(any('declared generated output' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))
        provider.write_text(provider.read_text().replace('other.rs', 'imu_config.rs'))
        manifest = self.root / 'platform/devices/imu/Cargo.toml'
        manifest.write_text(manifest.read_text().replace('bindings.toml', '../outside.toml'))
        self.assertTrue(any('package-relative' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))

    def test_generated_imu_keeps_the_current_checked_in_constructor_boundary(self):
        self.write_declarative_imu()
        nuttx = self.root / 'platform/devices/imu/src/nuttx/mod.rs'
        nuttx.write_text(nuttx.read_text().replace('type Bmi270 =', 'type Unknown ='))
        self.assertTrue(any('checked-in typed constructor' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))

    def test_static_devices_cannot_regress_to_global_or_descriptor_only_ownership(self):
        self.write_declarative_imu()
        instance = self.root / 'hal/imu/src/platform/nuttx/instance.rs'
        original = instance.read_text()
        for removed in ['storage: Mutex<Option<device::Device<S>>>', 'state: DeviceState',
                        'Worker::start_guarded', 'device::Device::open']:
            instance.write_text(original.replace(removed, 'missing'))
            self.assertTrue(any('guarded worker retirement' in error for error in CHECK.inspect_generated_imu_factory(self.root)))
        instance.write_text(original)
        self.write('hal/imu/src/platform/nuttx/device.rs', 'static OWNED: AtomicU8 = AtomicU8::new(0);')
        self.assertTrue(any('global device ownership' in error for error in CHECK.inspect_generated_imu_factory(self.root)))

    def test_hal_cannot_reintroduce_concrete_sensor_wiring(self):
        self.write_declarative_imu()
        self.write('hal/imu/src/lib.rs', 'use nxrs_bmi270::Bmi270;')
        self.assertTrue(any('concrete sensor wiring' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))
        self.write('hal/imu/src/lib.rs', '')
        self.write('hal/imu/Cargo.toml', '[package.metadata.nxrs]\nbindings = ["bindings.toml"]\n')
        self.assertTrue(any('concrete bindings belong' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))

    def test_sensor_bindings_can_belong_to_a_declared_driver(self):
        self.write_declarative_imu()
        catalog = self.root / 'platform/devices/imu/bindings.toml'
        self.write('driver/bmi270/bindings.toml', catalog.read_text())
        catalog.unlink()
        driver = self.write('driver/bmi270/Cargo.toml', '''
            [package]
            name = "nxrs-bmi270"
            [package.metadata.nxrs]
            bindings = ["bindings.toml"]
        ''')
        manifest = self.write('platform/devices/imu/Cargo.toml', '''
            [package]
            name = "nxrs-imu"
            [package.metadata.nxrs]
            bindings = [{ dependency = "sensor" }]
            [target.'cfg(target_os = "nuttx")'.dependencies]
            sensor = { package = "nxrs-bmi270", path = "../../../driver/bmi270" }
        ''')
        self.assertEqual(CHECK.inspect_generated_imu_factory(self.root), [])
        original = manifest.read_text()
        manifest.write_text(original.replace('.dependencies]', '.dev-dependencies]'))
        self.assertTrue(any('normal Cargo path dependency' in error
                            for error in CHECK.inspect_generated_imu_factory(self.root)))
        manifest.write_text(original)
        driver.write_text(driver.read_text().replace('bindings.toml', '../outside.toml'))
        self.assertTrue(any('package-relative' in error
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
    modules = set(re.findall(r'\bmod\s+(\w+)\s*;', selector))
    aliases = {f'{name} as selected' for name in modules if name != 'selected'}
    aliases.update(f'crate::{name} as selected' for name in re.findall(r'\bmod\s+(\w+)\s*;', root))
    selected_aliases = re.findall(r'\buse\s+((?:crate::)?\w+\s+as\s+selected)\s*;', selector)
    named = r'selected::(?:[A-Za-z_]\w*|\{\s*[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*\s*,?\s*\})'
    if not selected or any(not (re.fullmatch(named, item.strip()) or item.strip() == 'selected as construction' or item.strip() in aliases) for item in selected):
        errors.append('selector must export named selected items only')
    if (any(alias not in aliases for alias in selected_aliases)
            or not ('selected' in modules or any(alias in aliases for alias in selected_aliases))):
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
        self.assertEqual(wiring_errors('mod platform; pub use platform::construction;',
                                       'pub mod selected; pub use selected as construction;'), [])
        self.assertEqual(wiring_errors(self.ROOT,
                                       'mod unbound; pub use unbound as selected; pub use selected::open;'), [])
        self.assertEqual(wiring_errors(self.ROOT,
                                       'cfg_if::cfg_if! { if #[cfg(platform = "nuttx")] '
                                       '{ mod nuttx; use nuttx as selected; } else '
                                       '{ mod unbound; use unbound as selected; } } pub use selected::open;'), [])
        self.assertEqual(wiring_errors(self.ROOT + 'mod model;',
                                       'use crate::model as selected; pub use selected::open;'), [])
        self.assertEqual(wiring_errors(self.ROOT, self.SELECTOR + '\n#[cfg(platform = "mock")] pub use selected::{Mock, FORMAT};'), [])

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
        self.assertTrue(wiring_errors(self.ROOT, 'use crate::missing as selected; pub use selected::open;'))
        self.assertTrue(wiring_errors(self.ROOT, 'mod unbound; use unbound as selected; use missing as selected; pub use selected::open;'))

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
