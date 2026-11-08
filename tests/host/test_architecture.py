"""Production dependency and source-boundary checks; no hardware required."""
import copy
import hashlib
import json
import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'tools/check-architecture.py'
SPEC = importlib.util.spec_from_file_location('architecture', SOURCE)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.packages = {}
        for name, location in [('app', 'app/demo'), ('service', 'service/demo'),
                               ('camera', 'hal/camera'), ('storage', 'hal/storage'),
                               ('common', 'hal/common'), ('support', 'tests/nuttx/support'),
                               ('messaging', 'shared/messaging'),
                               ('driver', 'driver/demo'), ('platform', 'platform/build'),
                               ('bmi270', 'driver/bmi270'), ('imu', 'hal/imu'),
                               ('build-support', 'platform/build-support'),
                               ('camera-build', 'hal/camera/build-support'),
                               ('test', 'tests/portable'), ('target', 'tests/nuttx')]:
            self.package(name, location)
        self.metadata = {'packages': list(self.packages.values()),
                         'workspace_members': list(self.packages),
                         'workspace_default_members': ['app', 'service', 'camera', 'test']}

    def package(self, name, location):
        directory = self.root / location
        (directory / 'src').mkdir(parents=True)
        (directory / 'Cargo.toml').write_text(f'[package]\nname="{name}"\n')
        marker = 'deny' if location in ('hal/camera', 'hal/storage', 'hal/wifi', 'hal/ble', 'hal/detector', 'hal/imu') else 'forbid'
        (directory / 'src/lib.rs').write_text(f'#![no_std]\n#![{marker}(unsafe_code)]\n')
        if location.startswith('app/'):
            (directory / 'src/main.rs').write_text('#![forbid(unsafe_code)]\nfn main() {}\n')
        package = {'id': name, 'name': name, 'manifest_path': str(directory / 'Cargo.toml'), 'dependencies': []}
        self.packages[name] = package
        return package

    def add(self, source, destination, **kwargs):
        dep = {'name': destination, 'path': str(Path(self.packages[destination]['manifest_path']).parent), 'kind': None}
        self.packages[source]['dependencies'].append({**dep, **kwargs})

    def violations(self):
        return CHECK.inspect(self.metadata, self.root) + CHECK.inspect_sources(self.metadata, self.root)

    def test_hal_selector_exports_selected_items_without_implementation(self):
        directory = self.root / 'hal/camera/src/platform'
        directory.mkdir()
        selector = directory / 'mod.rs'
        for exports in ['pub(super) use selected::open;',
                        'pub use selected::{open, Camera};',
                        'pub(super) use selected::open;\n#[cfg(hal_provider = "mock")] pub use selected::{CameraAction, MockCamera, FORMAT};']:
            with self.subTest(exports=exports):
                selector.write_text('#[path="unbound.rs"] mod selected;\n' + exports + '\n')
                self.assertEqual(CHECK.inspect_hal_boundaries(self.root), [])
        valid = '#[path="unbound.rs"] mod selected;\npub use selected::open;\n'
        for code in [valid + 'pub fn open() {}', valid + 'pub use other::Camera;',
                     'mod selected { pub fn open() {} } pub use selected::*;',
                     'mod selected;', 'mod selected; pub use selected::{open, other::Camera};',
                     'mod selected; pub use selected::*;']:
            with self.subTest(code=code):
                selector.write_text(code)
                self.assertTrue(any('select source modules' in error
                                    for error in CHECK.inspect_hal_boundaries(self.root)))

    def test_hal_root_wildcard_exports_are_rejected(self):
        source = self.root / 'hal/camera/src/lib.rs'
        for export in ('pub use api::*;', 'pub use platform::*;'):
            with self.subTest(export=export):
                source.write_text('#![no_std]\n#![deny(unsafe_code)]\n' + export)
                self.assertTrue(any('must name public exports' in issue
                                    for issue in CHECK.inspect_hal_boundaries(self.root)))

    def test_camera_replay_feature_does_not_admit_provider_features(self):
        path = self.root / 'hal/camera/Cargo.toml'
        original = path.read_text()
        path.write_text(original + '[features]\nreplay=[]\n')
        self.assertEqual(self.violations(), [])
        path.write_text(original + '[features]\nreplay=[]\nnuttx=[]\n')
        self.assertTrue(self.violations())

    def test_valid_composition_and_contract_reuse(self):
        for source, target in [('app', 'service'), ('app', 'camera'), ('service', 'storage'),
                               ('storage', 'camera'),
                               ('test', 'app'), ('camera', 'driver')]:
            self.add(source, target)
        self.add('support', 'storage')
        self.add('support', 'camera')
        self.add('camera', 'app', kind='dev')
        self.assertEqual(self.violations(), [])

    def test_sensor_drivers_are_consumed_by_hals_not_apps_or_services(self):
        self.add('imu', 'bmi270')
        self.assertEqual(self.violations(), [])
        self.add('bmi270', 'imu')
        self.assertTrue(self.violations())
        self.packages['bmi270']['dependencies'].clear()
        for source in ('app', 'service'):
            self.add(source, 'bmi270')
            self.assertTrue(self.violations())
            self.packages[source]['dependencies'].clear()

        for source in ('app', 'service'):
            source_dir = Path(self.packages[source]['manifest_path']).parent
            (source_dir / 'src/entry.rs').write_text(
                '#[path="../../../driver/bmi270/src/lib.rs"] mod hidden;'
            )
            self.assertTrue(any('cross-package Rust source' in issue for issue in self.violations()))
            (source_dir / 'src/entry.rs').unlink()

    def test_hal_and_driver_build_helpers_are_only_host_build_dependencies(self):
        for source, destination in [('camera', 'build-support'), ('camera', 'camera-build'),
                                    ('driver', 'build-support')]:
            with self.subTest(source=source, destination=destination):
                self.add(source, destination, kind='build')
                self.assertEqual(self.violations(), [])
                self.packages[source]['dependencies'].clear()
                for extra in ({}, {'optional': True}, {'kind': 'dev'},
                              {'target': 'cfg(target_os = "linux")'}, {'rename': 'innocent'}):
                    self.add(source, destination, **extra)
                    self.assertTrue(self.violations())
                    self.packages[source]['dependencies'].clear()

    def test_build_controller_cannot_enter_hal_driver_or_support_graphs(self):
        for source in ('camera', 'driver', 'build-support', 'camera-build', 'app', 'service'):
            for kind in (None, 'dev', 'build'):
                with self.subTest(source=source, kind=kind):
                    self.add(source, 'platform', kind=kind)
                    self.assertTrue(self.violations())
                    self.packages[source]['dependencies'].clear()

    def test_owned_sdk_build_helpers_keep_their_direction_and_consumers(self):
        self.add('camera-build', 'build-support')
        self.add('platform', 'camera-build')
        self.add('platform', 'build-support')
        self.assertEqual(self.violations(), [])
        for source, destination in [('build-support', 'camera-build'),
                                    ('camera-build', 'camera'), ('camera-build', 'driver'),
                                    ('storage', 'camera-build'), ('driver', 'camera-build'),
                                    ('app', 'build-support'), ('service', 'camera-build')]:
            for kind in (None, 'build'):
                with self.subTest(source=source, destination=destination, kind=kind):
                    self.add(source, destination, kind=kind)
                    self.assertTrue(self.violations())
                    self.packages[source]['dependencies'].pop()

    def test_build_scripts_cannot_escape_their_package(self):
        manifest = self.root / 'hal/camera/Cargo.toml'
        original = manifest.read_text()
        manifest.write_text(original + 'build="build.rs"\n')
        self.assertEqual(self.violations(), [])
        manifest.write_text(original + 'build="../../platform/build-support/build.rs"\n')
        self.assertTrue(any('build scripts must belong' in error for error in self.violations()))

    def test_rust_sources_use_cargo_edges_instead_of_cross_package_includes(self):
        for name in ('camera', 'driver', 'build-support', 'platform'):
            path = Path(self.packages[name]['manifest_path']).parent / 'src/lib.rs'
            original = path.read_text()
            for code in ['#[path="../../../shared/messaging/src/lib.rs"] mod hidden;',
                         '#[cfg_attr(test, path="../../../shared/messaging/src/lib.rs")] mod hidden;',
                         'include!("../../../shared/messaging/src/lib.rs");',
                         'include!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../shared/messaging/src/lib.rs"));']:
                with self.subTest(package=name, code=code):
                    path.write_text(original + code)
                    self.assertTrue(any('cross-package Rust source' in error for error in self.violations()))
            path.write_text(original)
        path = self.root / 'hal/camera/src/lib.rs'
        path.write_text(path.read_text() + '#[path="../build-support/src/lib.rs"] mod nested;')
        self.assertTrue(any('cross-package Rust source' in error for error in self.violations()))

    def test_local_sources_and_generated_outputs_remain_ordinary_rust(self):
        path = self.root / 'hal/camera/src/lib.rs'
        path.write_text(path.read_text() + '\n'.join([
            '#[path="helper.rs"] mod helper;',
            'include!("helper.rs");',
            'include!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/helper.rs"));',
            'include!(concat!(env!("OUT_DIR"), "/generated.rs"));',
            '// #[path="../../../shared/messaging/src/lib.rs"] mod documentation;',
        ]))
        self.assertEqual(self.violations(), [])

    def test_cross_package_witnesses_require_an_effective_test_guard(self):
        path = self.root / 'hal/camera/src/lib.rs'
        original = path.read_text()
        witness = '#[path="../../../tests/host/witness.rs"] mod witness;'
        for guard in ('#[cfg(test)]', '#[cfg(all(test, feature="probe"))]',
                      '#[cfg(all(feature="probe", test))]',
                      '#[cfg(any(all(test, feature="one"), all(test, feature="two")))]'):
            with self.subTest(guard=guard):
                path.write_text(original + guard + witness)
                self.assertEqual(self.violations(), [])
        for guard in ('', '#[cfg(feature="probe")]', '#[cfg(any(test, feature="probe"))]',
                      '#[cfg(not(test))]'):
            with self.subTest(guard=guard):
                path.write_text(original + guard + witness)
                self.assertTrue(any('cross-package Rust source' in error for error in self.violations()))

    def test_portability_boundary_has_no_app_or_target_exceptions(self):
        for source, target in [('app', 'platform'), ('service', 'platform'), ('platform', 'app')]:
            for extra in ({}, {'kind': 'dev'}, {'kind': 'build'}, {'target': 'cfg(target_os = "nuttx")'}):
                self.add(source, target, **extra)
                self.assertTrue(self.violations())
                self.packages[source]['dependencies'].clear()
        directory = Path(self.packages['app']['manifest_path']).parent
        for code in ['#[cfg(target_os = "nuttx")] fn f() {}', 'fn f() { cfg!(unix); }',
                     'fn f() { let _ = std::env::consts::OS; }',
                     'fn f() { let _ = std::fs::File::open("/dev/ttyACM0"); }',
                     '#[path="../../platform/provider.rs"] mod hidden;']:
            with self.subTest(code=code):
                (directory / 'src/entry.rs').write_text(code)
                self.assertTrue(self.violations())
        (directory / 'src/entry.rs').unlink()
        native = directory / 'src/bridge.c'
        native.write_text('int call_sdk(void) { return 0; }')
        self.assertTrue(self.violations())
        native.unlink()
        manifest = directory / 'Cargo.toml'
        manifest.write_text('[package]\nname="app"\n[target.\'cfg(unix)\'.dependencies]\n')
        self.assertTrue(self.violations())
        manifest.write_text('[package]\nname="app"\n')
        platform = Path(self.packages['platform']['manifest_path']).parent
        (platform / 'src/main.rs').write_text('fn f(name: &str) { if name == "app" {} }')
        self.assertTrue(self.violations())
        (platform / 'src/main.rs').write_text('fn main() {}')
        script = platform / 'build.sh'
        script.write_text('exec build --app-manifest app/demo/Cargo.toml')
        self.assertTrue(self.violations())
        script.write_text('exec build --app-manifest "$selected_manifest"')
        self.assertEqual(self.violations(), [])

    def test_shared_radio_contracts_require_explicit_consumers(self):
        for name in ('wifi', 'ble'):
            self.package(name, f'hal/{name}')
            (self.root / 'hal' / name / 'src/lib.rs').write_text('#![no_std]\n#![deny(unsafe_code)]\n')
            self.metadata['packages'].append(self.packages[name])
            self.metadata['workspace_members'].append(name)
            self.add(name, 'common')
        self.assertEqual(self.violations(), [])
        for name in ('camera', 'storage'):
            with self.subTest(package=name):
                self.add(name, 'common')
                self.assertTrue(self.violations())
                self.packages[name]['dependencies'].clear()

    def test_messaging_is_portable_and_cannot_depend_on_upper_layers(self):
        for name in ('app', 'service', 'camera', 'driver'):
            self.add(name, 'messaging')
        self.assertEqual(self.violations(), [])
        for name in ('app', 'service', 'camera', 'driver', 'platform', 'common'):
            self.add('messaging', name)
            self.assertTrue(self.violations())
            self.packages['messaging']['dependencies'].clear()
        source = self.root / 'shared/messaging/src/lib.rs'
        source.write_text('#![no_std]\n#![forbid(unsafe_code)]\n#[cfg(unix)] fn f() {}\n')
        self.assertTrue(self.violations())

    def test_shared_manifests_cannot_bypass_inventory_or_feature_policy(self):
        manifest = self.root / 'shared/messaging/Cargo.toml'
        manifest.write_text('[package]\nname="messaging"\n[features]\nchannels=[]\n')
        self.assertEqual(self.violations(), [])
        manifest.write_text(manifest.read_text() + 'std=[]\n')
        self.assertTrue(any('provider features' in error for error in self.violations()))
        self.package('hidden', 'shared/hidden')
        self.assertTrue(any('package omitted' in error for error in self.violations()))

    def test_reverse_edges_and_optional_build_target_backdoors(self):
        for source, target in [('service', 'app'), ('camera', 'service'), ('common', 'camera'),
                               ('camera', 'support'), ('app', 'test'), ('driver', 'platform')]:
            for extra in ({}, {'optional': True}, {'kind': 'build'}, {'target': 'cfg(windows)'}, {'rename': 'innocent'}):
                with self.subTest(edge=(source, target), extra=extra):
                    self.add(source, target, **extra)
                    self.assertTrue(self.violations())
                    self.packages[source]['dependencies'].clear()

    def test_fixture_support_cannot_enter_production_even_when_target_scoped(self):
        for source, extra in [('app', {}), ('service', {}), ('camera', {}), ('storage', {'optional': True}), ('camera', {'target': 'cfg(target_os = "nuttx")'})]:
            self.add(source, 'support', **extra)
            self.assertTrue(self.violations())
            self.packages[source]['dependencies'].clear()

    def test_external_runtime_dependency_requires_portability_review(self):
        for extra in ({}, {'optional': True}, {'kind': 'build'}, {'target': 'cfg(unix)'}):
            self.packages['camera']['dependencies'] = [{'name': 'crossbeam-channel', 'kind': None, **extra}]
            self.assertTrue(self.violations())
        self.packages['camera']['dependencies'].clear()

    def test_nested_contract_provider_crates_are_rejected(self):
        for role in ('api', 'native', 'mock', 'nuttx', 'mystery'):
            package = self.package(role, 'hal/camera/' + role)
            data = copy.deepcopy(self.metadata)
            data['packages'].append(package)
            data['workspace_members'].append(role)
            self.assertTrue(CHECK.inspect(data, self.root))

    def test_outside_workspace_dependencies_and_hidden_packages_rejected(self):
        self.packages['app']['dependencies'].append({'name': 'outside', 'path': str(self.root / 'outside'), 'kind': None})
        self.assertTrue(self.violations())
        self.packages['app']['dependencies'].clear()
        self.package('hidden', 'app/hidden')
        self.assertTrue(self.violations())

    def test_private_experiment_does_not_become_production_dependency(self):
        directory = self.root / 'tests/comparison'
        directory.mkdir()
        (directory / 'Cargo.toml').write_text('[package]\nname="experiment"\npublish=false\n[workspace]\n')
        self.assertEqual(self.violations(), [])
        self.packages['camera']['dependencies'].append({'name': 'experiment', 'path': str(directory), 'kind': None})
        self.assertTrue(self.violations())

    def test_nested_build_output_is_not_source_inventory(self):
        directory = self.root / 'tests/portable/target/vendor'
        directory.mkdir(parents=True)
        (directory / 'Cargo.toml').write_text('[package]\nname="downloaded"\n')
        self.assertEqual(self.violations(), [])

    def test_shared_source_modules_must_forbid_unsafe(self):
        path = self.root / 'hal/camera/src/api.rs'
        path.write_text('pub trait Camera {}\n')
        self.assertTrue(self.violations())
        path.write_text('#![forbid(unsafe_code)]\npub trait Camera {}\n')
        self.assertEqual(self.violations(), [])
        path = self.root / 'hal/camera/Cargo.toml'
        path.write_text(path.read_text() + '[features]\nnuttx=[]\n')
        self.assertTrue(self.violations())

    def test_app_entry_and_portable_markers_are_required(self):
        (self.root / 'app/demo/src/main.rs').unlink()
        self.assertTrue(self.violations())

    def test_library_compositions_can_use_std_without_owning_the_firmware_entry(self):
        self.packages['app']['targets'] = [{'kind': ['lib']}]
        (self.root / 'app/demo/src/main.rs').unlink()
        path = self.root / 'app/demo/src/lib.rs'
        path.write_text('#![forbid(unsafe_code)]\nuse std::net::TcpListener;\n')
        self.assertEqual(self.violations(), [])
        path.unlink()
        self.assertTrue(self.violations())

    def test_services_can_use_std_without_unsafe(self):
        (self.root / 'service/demo/src/lib.rs').write_text('#![forbid(unsafe_code)]\nuse std::thread;\n')
        self.assertEqual(self.violations(), [])

    def test_hal_can_use_std_while_retaining_its_unsafe_boundary(self):
        path = self.root / 'hal/imu/src/lib.rs'
        path.write_text('#![deny(unsafe_code)]\nuse std::thread;\n')
        self.assertEqual(self.violations(), [])
        path.write_text('use std::thread;\n')
        self.assertTrue(any('must retain #![deny(unsafe_code)]' in error
                            for error in self.violations()))

    def test_diagnostic_switches_are_consolidated_under_probe(self):
        path = self.root / 'app/demo/Cargo.toml'
        original = path.read_text()
        path.write_text(original + '[features]\nprobe=[]\n')
        self.assertEqual(self.violations(), [])
        path.write_text(original + '[features]\nstack-probe=[]\n')
        self.assertTrue(any('use probe for diagnostics' in error for error in self.violations()))
        for feature, enabled in [('default', 'probe'), ('recording', 'probe'),
                                 ('recording', 'camera/probe'), ('recording', 'camera?/probe')]:
            with self.subTest(feature=feature, enabled=enabled):
                path.write_text(original + f'[features]\nprobe=[]\n{feature}=["{enabled}"]\n')
                self.assertTrue(any('explicit opt-in' in error for error in self.violations()))

    def test_optional_functionality_uses_normal_cargo_features(self):
        for package in ('app', 'service', 'camera', 'messaging'):
            path = Path(self.packages[package]['manifest_path'])
            original = path.read_text()
            with self.subTest(package=package):
                path.write_text(original + '[features]\ndefault=["recording"]\n'
                                'recording=["compression"]\ncompression=[]\n')
                self.assertEqual(self.violations(), [])
                for feature in ('nuttx', 'mock', 'std'):
                    path.write_text(original + f'[features]\n{feature}=[]\n')
                    self.assertTrue(any('provider features' in error for error in self.violations()))
                path.write_text(original)
        self.add('app', 'storage', optional=True)
        path = Path(self.packages['app']['manifest_path'])
        path.write_text(path.read_text() + '[dependencies]\n'
                        'storage={path="../../hal/storage",optional=true}\n'
                        '[features]\nrecording=["dep:storage", "storage?/compression"]\n')
        storage = Path(self.packages['storage']['manifest_path'])
        storage.write_text(storage.read_text() + '[features]\ncompression=[]\n')
        self.assertEqual(self.violations(), [])

    def test_services_consume_detector_contract_without_selecting_models(self):
        detector = self.package('detector', 'hal/detector')
        self.metadata['packages'].append(detector)
        self.metadata['workspace_members'].append('detector')
        for extra in ({'features': ['pico-model']},
                      {'features': ['probe']},
                      {'optional': True, 'features': ['person-model']},
                      {'target': 'cfg(unix)', 'features': ['pico-model']}):
            with self.subTest(extra=extra):
                self.add('service', 'detector', **extra)
                self.assertTrue(any('product wiring' in error for error in self.violations()))
                self.packages['service']['dependencies'].clear()
        self.add('service', 'detector')
        self.assertEqual(self.violations(), [])
        self.packages['service']['dependencies'].clear()
        self.add('service', 'detector', kind='dev', features=['person-model'])
        self.assertEqual(self.violations(), [])

    def test_service_model_feature_forwarding_is_rejected_including_renamed_dependencies(self):
        detector = self.package('detector', 'hal/detector')
        self.metadata['packages'].append(detector)
        self.metadata['workspace_members'].append('detector')
        self.add('service', 'detector', rename='vision')
        path = self.root / 'service/demo/Cargo.toml'
        original = path.read_text()
        for feature in ('vision/person-model', 'vision?/pico-model'):
            with self.subTest(feature=feature):
                path.write_text(original + f'[features]\nmodel=["{feature}"]\n')
                self.assertTrue(any('product wiring' in error for error in self.violations()))

    def test_application_model_selection_belongs_to_product_profile(self):
        detector = self.package('detector', 'hal/detector')
        self.metadata['packages'].append(detector)
        self.metadata['workspace_members'].append('detector')
        self.add('app', 'detector', features=['pico-model'])
        self.assertTrue(any('product wiring' in error for error in self.violations()))

    def test_target_only_packages_and_parallel_hierarchy_are_rejected(self):
        data = copy.deepcopy(self.metadata)
        data['workspace_default_members'].extend(['target', 'support'])
        self.assertTrue(CHECK.inspect(data, self.root))
        (self.root / 'crates').mkdir()
        self.assertTrue(self.violations())


class PublicNamespaceTests(unittest.TestCase):
    def test_hal_and_service_namespaces_are_short_and_distinct(self):
        root = SOURCE.parents[1]
        workspace = tomllib.loads((root / 'Cargo.toml').read_text())['workspace']
        names = set()
        for member in workspace['members']:
            path = Path(member)
            if path.parts[0] not in {'hal', 'service'}:
                continue
            if CHECK.layer(path) == 'build-support':
                continue
            data = tomllib.loads((root / member / 'Cargo.toml').read_text())
            name = data.get('lib', {}).get('name', data['package']['name'].replace('-', '_'))
            self.assertFalse(name.startswith('nxrs_'), name)
            self.assertNotIn(name, names)
            names.add(name)
            if path.parts[0] == 'hal' and len(path.parts) == 2:
                self.assertEqual(name, 'hal_common' if path.name == 'common' else path.name)


class DependencyCapsuleTests(unittest.TestCase):
    def test_missing_changed_and_extra_dependency_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'vendor/microflow/src/lib.rs'
            source.parent.mkdir(parents=True)
            source.write_text('reviewed source')
            self.assertTrue(CHECK.inspect_dependency_capsule(root))
            ledger = root / 'upstream/microflow/source.json'
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({'files': {'vendor/microflow/src/lib.rs': hashlib.sha256(source.read_bytes()).hexdigest()}}))
            self.assertEqual(CHECK.inspect_dependency_capsule(root), [])
            source.write_text('unreviewed change')
            self.assertTrue(CHECK.inspect_dependency_capsule(root))
            source.write_text('reviewed source')
            (source.parent / 'extra.rs').write_text('extra')
            self.assertTrue(CHECK.inspect_dependency_capsule(root))


if __name__ == '__main__':
    unittest.main()
