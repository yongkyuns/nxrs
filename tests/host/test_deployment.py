"""Platform selection, inheritance, target and required-capability controls."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "tools/check-deployment.py"
SPEC = importlib.util.spec_from_file_location("deployment", SOURCE)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
import platform_config as PLATFORM


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Cargo.toml").write_text('[workspace]\nresolver="2"\n')
        self.app = self.root / "app/demo/Cargo.toml"
        self.app.parent.mkdir(parents=True)
        self.app.write_text('[package]\nname="demo"\n[dependencies]\nimu={path="../../hal/imu"}\n')
        for capability in PLATFORM.CAPABILITIES:
            directory = self.root / "hal" / capability
            directory.mkdir(parents=True)
            (directory / "Cargo.toml").write_text(
                f'[package]\nname="nxrs-{capability}"\n[package.metadata.nxrs]\n'
                'providers=["mock","native","nuttx"]\n')
        self.directory = self.root / "platform/profiles"
        self.directory.mkdir(parents=True)
        self.profile('mock', 'execution="any"\n[hal]\nimu="mock"\ngnss="mock"\n')

    def profile(self, name, content):
        path = self.directory / (name + '.toml')
        path.write_text(content)
        return path

    def test_one_platform_selects_complete_bindings(self):
        result = CHECK.validate(self.app, 'mock', 'linux')
        self.assertEqual({p['capability'] for p in result['providers']}, {'imu', 'gnss'})
        self.assertEqual(result['platform'], 'mock')
        self.assertEqual(len(result['profiles'][0]['sha256']), 64)

    def test_desktop_radio_profiles_are_explicit_and_os_specific(self):
        for name in ('linux', 'macos'):
            with self.subTest(profile=name):
                profile = PLATFORM.resolve(name)
                self.assertEqual({key: profile['hal'][key] for key in ('wifi', 'ble')},
                                 {'wifi': 'native', 'ble': 'native'})
                PLATFORM.validate_target(profile, name)
                other = 'macos' if name == 'linux' else 'linux'
                with self.assertRaisesRegex(ValueError, 'does not support target OS'):
                    PLATFORM.validate_target(profile, other)
        native = PLATFORM.resolve('native')
        self.assertFalse(set(native['hal']) & {'wifi', 'ble'})

    def test_named_variant_inherits_and_overrides_without_mutating_base(self):
        self.profile('variant', 'extends="mock"\n[hal]\nimu="none"\nled="mock"\n')
        result = PLATFORM.resolve('variant', self.root)
        self.assertEqual(result['hal'], {'imu': 'none', 'gnss': 'mock', 'led': 'mock'})
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(PLATFORM.resolve('mock', self.root)['hal']['imu'], 'mock')

    def test_cycles_unknown_names_and_path_traversal_are_rejected(self):
        self.profile('a', 'extends="b"\n')
        self.profile('b', 'extends="a"\n')
        for name in ('a', 'missing', '../mock', '', '/tmp/wifi'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                PLATFORM.resolve(name, self.root)

    def test_ambiguous_name_is_rejected(self):
        other = self.root / 'platform/nuttx/platforms'
        other.mkdir(parents=True)
        (other / 'mock.toml').write_text('[hal]\n')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            PLATFORM.resolve('mock', self.root)

    def test_invalid_duplicate_and_retired_bindings_are_rejected(self):
        for content in ('[hal]\nunknown="mock"\n', '[hal]\nimu="other"\n',
                        '[hal]\nimu="mock"\nimu="native"\n', 'hal-features=["nxrs-imu/mock"]\n'):
            self.profile('bad', content)
            with self.assertRaises(ValueError):
                PLATFORM.resolve('bad', self.root)

    def test_target_validation_checks_whole_platform_not_only_current_crate(self):
        self.profile('radio', 'execution="nuttx"\n[hal]\nwifi="nuttx"\n')
        platform = PLATFORM.resolve('radio', self.root)
        PLATFORM.validate_target(platform, 'nuttx')
        for os in ('linux', 'macos', 'windows', 'none'):
            with self.subTest(os=os), self.assertRaisesRegex(ValueError, 'does not support'):
                PLATFORM.validate_target(platform, os)

    def test_native_platform_rejects_wasm_and_bare_metal(self):
        self.profile('native', 'execution="std"\n[hal]\ncamera="native"\n')
        platform = PLATFORM.resolve('native', self.root)
        for os in ('linux', 'macos', 'windows'):
            PLATFORM.validate_target(platform, os)
        for os in ('unknown', 'none', 'nuttx'):
            with self.assertRaises(ValueError):
                PLATFORM.validate_target(platform, os)

    def test_platform_os_constraint_is_inherited_and_checked_before_bindings(self):
        self.profile('macos', 'execution="std"\ntarget-os=["macos"]\n[hal]\nwifi="native"\n')
        self.profile('macos-product', 'extends="macos"\n')
        profile = PLATFORM.resolve('macos-product', self.root)
        PLATFORM.validate_target(profile, 'macos')
        for target in ('linux', 'windows', 'nuttx', 'none'):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'does not support target OS'):
                PLATFORM.validate_target(profile, target)

    def test_invalid_platform_os_constraints_fail_explicitly(self):
        for value in ('[]', '"macos"', '[1]', '["mystery"]'):
            self.profile('bad-os', f'execution="std"\ntarget-os={value}\n')
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'target-os must be'):
                PLATFORM.validate_target(PLATFORM.resolve('bad-os', self.root), 'macos')

    def test_missing_binding_stays_none_not_mock(self):
        platform = PLATFORM.resolve('mock', self.root)
        output = PLATFORM.cargo_output(platform, 'wifi', 'linux', self.root)
        self.assertIn('cargo:rustc-cfg=nxrs_provider="none"', output)
        self.assertTrue(any(line.endswith('mock.toml') for line in output if 'rerun-if-changed' in line))

    def test_missing_required_capability_fails_before_build(self):
        self.app.write_text(self.app.read_text() + '[package.metadata.nxrs]\nrequired-capabilities=["wifi"]\n')
        with self.assertRaisesRegex(ValueError, 'missing required capability wifi'):
            CHECK.validate(self.app, 'mock', 'linux')

    def test_app_cannot_select_provider_path_or_feature(self):
        for declaration in ('imu={path="../../hal/imu/mock"}',
                            'imu={path="../../hal/imu",features=["mock"]}'):
            self.app.write_text('[package]\nname="demo"\n[dependencies]\n' + declaration + '\n')
            with self.assertRaisesRegex(ValueError, 'selects'):
                CHECK.validate(self.app, 'mock', 'linux')

    def test_nested_app_uses_enclosing_workspace(self):
        app = self.root / 'tests/probe/rust/Cargo.toml'
        app.parent.mkdir(parents=True)
        app.write_text('[package]\nname="probe"\n')
        CHECK.validate(app, 'mock', 'linux')

    def test_missing_workspace_is_explicit(self):
        (self.root / 'Cargo.toml').unlink()
        with self.assertRaisesRegex(ValueError, 'no enclosing Cargo workspace'):
            CHECK.validate(self.app, 'mock', 'linux')

    def test_native_helpers_include_only_selected_capability_abi(self):
        directory = self.root / 'hal/camera/ffi/nuttx'
        directory.mkdir(parents=True)
        for filename in ('v4l2.c', 'close.c'):
            (directory / filename).touch()
        legacy = self.root / 'tests/nuttx/c'
        legacy.mkdir(parents=True)
        (legacy / 'storage.c').touch()
        (legacy / 'camera.c').touch()
        self.profile('fixture', 'execution="nuttx"\n[hal]\ncamera="nuttx"\nstorage="nuttx"\n')
        helpers = PLATFORM.native_helpers(PLATFORM.resolve('fixture', self.root), self.root)
        self.assertEqual({path.name for path in helpers}, {'v4l2.c', 'close.c'})
        self.assertEqual(len(helpers), 2)
        self.assertEqual(PLATFORM.native_helpers(PLATFORM.resolve('mock', self.root), self.root), [])

    def test_cargo_tracks_every_manifest_used_to_validate_platform(self):
        lines = PLATFORM.cargo_output(PLATFORM.resolve('mock', self.root), 'wifi', 'linux', self.root)
        for capability in ('wifi', 'imu', 'gnss'):
            self.assertIn(f'cargo:rerun-if-changed={self.root / "hal" / capability / "Cargo.toml"}', lines)


if __name__ == '__main__':
    unittest.main()
