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
                               ('driver', 'driver/demo'), ('platform', 'platform/firmware'),
                               ('test', 'tests/portable'), ('target', 'tests/nuttx')]:
            self.package(name, location)
        self.metadata = {'packages': list(self.packages.values()),
                         'workspace_members': list(self.packages),
                         'workspace_default_members': ['app', 'service', 'camera', 'test']}

    def package(self, name, location):
        directory = self.root / location
        (directory / 'src').mkdir(parents=True)
        (directory / 'Cargo.toml').write_text(f'[package]\nname="{name}"\n')
        marker = 'deny' if location in ('hal/camera', 'hal/storage', 'hal/wifi', 'hal/ble', 'hal/detector') else 'forbid'
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

    def test_valid_composition_and_contract_reuse(self):
        for source, target in [('app', 'service'), ('app', 'camera'), ('service', 'storage'),
                               ('storage', 'camera'),
                               ('test', 'app'), ('camera', 'driver')]:
            self.add(source, target)
        self.add('support', 'storage')
        self.add('support', 'camera')
        self.add('camera', 'app', kind='dev')
        self.assertEqual(self.violations(), [])

    def test_product_composition_is_downward_and_controllers_stay_safe(self):
        product = self.package('product', 'platform/nuttx/camera')
        self.metadata['packages'].append(product)
        self.metadata['workspace_members'].append('product')
        directory = Path(product['manifest_path']).parent
        (directory / 'src/lib.rs').write_text('#![deny(unsafe_code)]\n')
        (directory / 'src/main.rs').write_text('#![forbid(unsafe_code)]\nfn main() {}\n')
        for target in ('app', 'service', 'camera'):
            self.add('product', target)
        self.assertEqual(self.violations(), [])
        for source, target in [('product', 'test'), ('app', 'product'), ('service', 'product'),
                               ('camera', 'product'), ('platform', 'product')]:
            for extra in ({}, {'optional': True}, {'kind': 'build'}, {'target': 'cfg(unix)'}):
                with self.subTest(edge=(source, target), extra=extra):
                    before = copy.deepcopy(self.packages[source]['dependencies'])
                    self.add(source, target, **extra)
                    self.assertTrue(self.violations())
                    self.packages[source]['dependencies'] = before
        controller = directory / 'src/runtime.rs'
        controller.write_text('fn run() {}\n')
        self.assertTrue(self.violations())
        controller.write_text('#![forbid(unsafe_code)]\nfn run() {}\n')
        self.assertEqual(self.violations(), [])
        self.metadata['workspace_default_members'].append('product')
        self.assertTrue(self.violations())

    def test_shared_radio_delivery_contracts_are_only_reused_by_radio_hals(self):
        for name in ('wifi', 'ble'):
            self.package(name, f'hal/{name}')
            self.metadata['packages'].append(self.packages[name])
            self.metadata['workspace_members'].append(name)
            self.add(name, 'common')
        self.assertEqual(self.violations(), [])
        for name in ('camera', 'storage'):
            with self.subTest(package=name):
                self.add(name, 'common')
                self.assertTrue(self.violations())
                self.packages[name]['dependencies'].clear()

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

    def test_diagnostic_switches_are_consolidated_under_probe(self):
        path = self.root / 'app/demo/Cargo.toml'
        original = path.read_text()
        path.write_text(original + '[features]\nprobe=[]\n')
        self.assertEqual(self.violations(), [])
        path.write_text(original + '[features]\nstack-probe=[]\n')
        self.assertTrue(any('typed cases' in error for error in self.violations()))
        path.write_text(original + '[features]\nprobe=[]\ndefault=["probe"]\n')
        self.assertTrue(any('explicit opt-in' in error for error in self.violations()))

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
