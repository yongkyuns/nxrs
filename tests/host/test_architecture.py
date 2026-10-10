"""Negative controls for the dependency gate; no compiler or network needed."""
import copy
import importlib.util
from pathlib import Path
import tempfile
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
        locations = {'app': 'app/camera', 'service': 'service', 'hal': 'hal/facade',
                     'api': 'hal/example/api', 'native': 'hal/example/native',
                     'nuttx': 'hal/example/nuttx', 'mock': 'hal/example/mock',
                     'driver': 'driver/sensor', 'platform': 'platform/native',
                     'test': 'tests/portable', 'target': 'tests/nuttx'}
        self.packages = {}
        for name, location in locations.items():
            directory = self.root / location
            directory.mkdir(parents=True)
            (directory / 'Cargo.toml').write_text('[package]\nname = "' + name + '"\n')
            (directory / 'src').mkdir()
            (directory / 'src/lib.rs').write_text('#![no_std]\n#![forbid(unsafe_code)]\n')
            self.packages[name] = {'id': name, 'name': name, 'manifest_path': str(directory / 'Cargo.toml'), 'dependencies': []}
        (self.root / 'app/camera/src/main.rs').write_text('#![forbid(unsafe_code)]\nfn main() {}\n')
        self.metadata = {'packages': list(self.packages.values()), 'workspace_members': list(self.packages),
                         'workspace_default_members': ['app', 'service', 'api', 'native', 'test']}

    def add(self, source, destination, **kwargs):
        dep = {'name': destination, 'path': str(Path(self.packages[destination]['manifest_path']).parent), 'kind': None}
        dep.update(kwargs)
        self.packages[source]['dependencies'].append(dep)

    def violations(self):
        return CHECK.inspect(self.metadata, self.root) + CHECK.inspect_sources(self.metadata, self.root)

    def test_valid_composition_and_dev_only_reverse_edges(self):
        for a, b in [('app', 'service'), ('service', 'api'), ('service', 'hal'),
                     ('hal', 'api'), ('hal', 'mock'), ('native', 'api'), ('nuttx', 'api'),
                     ('mock', 'api'), ('driver', 'api'), ('platform', 'native'),
                     ('test', 'app'), ('test', 'mock'), ('native', 'driver')]:
            self.add(a, b)
        self.add('native', 'app', kind='dev')
        self.assertEqual(self.violations(), [])

    def test_normal_optional_target_and_build_backdoors_rejected(self):
        for options in [{}, {'optional': True}, {'target': 'cfg(target_os = "none")'},
                        {'kind': 'build'}, {'rename': 'innocent_alias'}]:
            with self.subTest(options=options):
                self.packages['app']['dependencies'] = []
                self.add('app', 'nuttx', **options)
                self.assertTrue(self.violations())

    def test_reverse_and_test_leaks_rejected(self):
        for a, b in [('service', 'app'), ('api', 'native'), ('native', 'service'),
                     ('driver', 'platform'), ('platform', 'app'),
                     ('hal', 'service'), ('app', 'test'), ('service', 'mock')]:
            with self.subTest(edge=(a, b)):
                self.add(a, b)
                self.assertTrue(self.violations())
                self.packages[a]['dependencies'].clear()

    def test_unclassified_local_dependency_rejected(self):
        self.packages['api']['dependencies'].append({'name': 'outside', 'path': str(self.root / 'outside'), 'kind': None})
        self.assertTrue(self.violations())

    def test_unqualified_external_portable_dependency_rejected(self):
        self.packages['service']['dependencies'].append({'name': 'libc', 'source': 'registry+example', 'kind': None})
        self.assertTrue(self.violations())

    def test_new_package_cannot_hide_outside_workspace(self):
        extra = self.root / 'app/extra'; extra.mkdir()
        (extra / 'Cargo.toml').write_text('[package]\nname="extra"\n')
        self.assertTrue(self.violations())

    def test_private_standalone_test_workspace_is_not_a_production_member(self):
        extra = self.root / 'tests/comparison'
        extra.mkdir()
        (extra / 'Cargo.toml').write_text('[package]\nname="experiment"\npublish=false\n[workspace]\n')
        self.assertEqual(self.violations(), [])
        self.packages['app']['dependencies'].append(
            {'name': 'experiment', 'path': str(extra), 'kind': None})
        self.assertTrue(self.violations())

    def test_standalone_workspace_cannot_hide_a_production_or_published_package(self):
        for directory, publish in [('app/extra', 'false'), ('tests/published', 'true')]:
            extra = self.root / directory
            extra.mkdir()
            manifest = extra / 'Cargo.toml'
            manifest.write_text(f'[package]\nname="extra"\npublish={publish}\n[workspace]\n')
            self.assertTrue(self.violations())
            manifest.unlink()

    def test_nested_build_output_is_not_source_inventory(self):
        output = self.root / 'tests/portable/target/vendor'
        output.mkdir(parents=True)
        (output / 'Cargo.toml').write_text('[package]\nname="downloaded"\n')
        self.assertEqual(self.violations(), [])

    def test_parallel_hierarchy_rejected(self):
        (self.root / 'crates').mkdir()
        self.assertTrue(self.violations())

    def test_portable_markers_required(self):
        (self.root / 'service/src/lib.rs').write_text('pub fn run() {}\n')
        self.assertTrue(self.violations())
        (self.root / 'service/src/lib.rs').write_text('#![forbid(unsafe_code)]\n')
        (self.root / 'hal/facade/src/lib.rs').write_text('#![forbid(unsafe_code)]\n')
        self.assertTrue(self.violations())

    def test_service_may_use_std_when_it_remains_safe(self):
        (self.root / 'service/src/lib.rs').write_text(
            '#![forbid(unsafe_code)]\nuse std::thread;\npub fn run() { let _ = thread::current(); }\n'
        )
        self.assertEqual(self.violations(), [])

    def test_target_only_packages_not_default_members(self):
        for name in ['nuttx', 'target']:
            with self.subTest(name=name):
                data = copy.deepcopy(self.metadata)
                data['workspace_default_members'].append(name)
                self.assertTrue(CHECK.inspect(data, self.root))

    def test_nxrs_app_has_no_provider_selection_exception(self):
        self.packages['app']['name'] = 'nxrs-applications'
        self.add('app', 'native', rename='configured-example', optional=True)
        self.assertTrue(self.violations())

    def test_service_provider_alias_is_also_rejected(self):
        self.add('service', 'native', rename='configured-example', optional=True)
        self.assertTrue(self.violations())

    def test_app_requires_main_but_not_a_library(self):
        (self.root / 'app/camera/src/lib.rs').unlink()
        self.assertEqual(self.violations(), [])
        (self.root / 'app/camera/src/main.rs').unlink()
        self.assertTrue(self.violations())

    def test_app_may_use_portable_hal_abstractions(self):
        self.add('app', 'api')
        self.add('app', 'hal')
        self.assertEqual(self.violations(), [])

    def test_app_cannot_depend_on_another_app(self):
        self.add('app', 'app')
        self.assertTrue(self.violations())

    def domain_package(self, name, location):
        directory = self.root / location
        (directory / 'src').mkdir(parents=True)
        (directory / 'Cargo.toml').write_text('[package]\nname="' + name + '"\n')
        (directory / 'src/lib.rs').write_text('#![no_std]\n#![forbid(unsafe_code)]\n')
        package = {'id': name, 'name': name, 'manifest_path': str(directory / 'Cargo.toml'), 'dependencies': []}
        self.packages[name] = package
        self.metadata['packages'].append(package)
        self.metadata['workspace_members'].append(name)

    def camera_packages(self):
        for name, path in [('common', 'hal/common'), ('camera-api', 'hal/camera/api'),
                           ('camera-native', 'hal/camera/native'), ('camera-nuttx', 'hal/camera/nuttx'),
                           ('camera-mock', 'hal/camera/mock'), ('support', 'hal/support/nuttx')]:
            self.domain_package(name, path)
        for source, target in [('camera-api', 'common'), ('support', 'common'),
                               ('camera-native', 'camera-api'), ('camera-mock', 'camera-api'),
                               ('camera-nuttx', 'camera-api'), ('camera-nuttx', 'support')]:
            self.add(source, target)

    def test_domain_provider_packages_remain_independent_of_facade(self):
        self.camera_packages()
        self.assertEqual(self.violations(), [])

    def test_domain_and_facade_backdoors_rejected(self):
        self.camera_packages()
        for a, b in [('common', 'camera-api'), ('support', 'camera-api'),
                     ('camera-native', 'support'), ('camera-native', 'native'),
                     ('camera-native', 'camera-nuttx'), ('support', 'camera-native'),
                     ('native', 'camera-nuttx'), ('camera-mock', 'mock'), ('service', 'camera-native')]:
            for extra in ({}, {'kind': 'build'}, {'optional': True}, {'target': 'cfg(windows)'}):
                with self.subTest(edge=(a, b), kind=extra):
                    self.add(a, b, **extra)
                    self.assertTrue(self.violations())
                    self.packages[a]['dependencies'].pop()

    def test_nxrs_app_cannot_select_camera_provider_directly(self):
        self.camera_packages()
        self.packages['app']['name'] = 'nxrs-applications'
        self.add('app', 'camera-native', rename='configured-camera', optional=True)
        self.assertTrue(self.violations())

    def test_nxrs_app_cannot_select_camera_mock_directly(self):
        self.camera_packages()
        self.packages['app']['name'] = 'nxrs-applications'
        self.add('app', 'camera-mock', rename='configured-camera', optional=True)
        self.assertTrue(self.violations())

    def test_nuttx_provider_and_support_not_default_members(self):
        self.camera_packages()
        for name in ['camera-nuttx', 'support']:
            data = copy.deepcopy(self.metadata)
            data['workspace_default_members'].append(name)
            self.assertTrue(CHECK.inspect(data, self.root))

    def test_unknown_domain_role_is_rejected(self):
        self.domain_package('wrong', 'hal/camera/mystery')
        self.assertTrue(self.violations())

    def storage_packages(self):
        self.camera_packages()
        for role in ('api', 'native', 'nuttx', 'mock'):
            self.domain_package('storage-' + role, 'hal/storage/' + role)
        self.add('storage-api', 'camera-api')
        for role in ('native', 'nuttx', 'mock'):
            self.add('storage-' + role, 'storage-api')
        self.add('storage-nuttx', 'support')

    def test_storage_contract_can_reuse_camera_data_without_a_provider(self):
        self.storage_packages()
        self.assertEqual(self.violations(), [])
        for a, b in [('storage-api', 'camera-native'), ('storage-native', 'native'),
                     ('storage-native', 'camera-native'), ('storage-mock', 'mock'),
                     ('service', 'storage-native'), ('native', 'storage-nuttx')]:
            for options in ({}, {'optional': True}, {'kind': 'build'}, {'target': 'cfg(windows)'}):
                with self.subTest(edge=(a, b), options=options):
                    self.add(a, b, **options)
                    self.assertTrue(self.violations())
                    self.packages[a]['dependencies'].pop()

    def test_nxrs_app_cannot_select_storage_provider_directly(self):
        self.storage_packages()
        self.packages['app']['name'] = 'nxrs-applications'
        self.add('app', 'storage-native', rename='configured-storage', optional=True)
        self.assertTrue(self.violations())

    def test_package_cannot_hide_under_crates(self):
        self.packages['app']['manifest_path'] = str(self.root / 'crates/app/Cargo.toml')
        self.assertTrue(self.violations())


if __name__ == '__main__':
    unittest.main()
