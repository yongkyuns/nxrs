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
                'platforms=["mock","linux","macos","windows","nuttx"]\n')
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
                                 {'wifi': name, 'ble': name})
                for capability in ('wifi', 'ble'):
                    self.assertIn('cargo:rustc-cfg=hal_enabled',
                                  PLATFORM.cargo_output(profile, capability, name))
                PLATFORM.validate_target(profile, name)
                other = 'macos' if name == 'linux' else 'linux'
                with self.assertRaisesRegex(ValueError, 'does not support target OS'):
                    PLATFORM.validate_target(profile, other)

    def test_named_variant_inherits_and_overrides_without_mutating_base(self):
        self.profile('variant', 'extends="mock"\n[hal]\nimu="none"\nled="mock"\n')
        result = PLATFORM.resolve('variant', self.root)
        self.assertEqual(result['hal'], {'imu': 'none', 'gnss': 'mock', 'led': 'mock'})
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(PLATFORM.resolve('mock', self.root)['hal']['imu'], 'mock')

    def test_retired_native_profile_and_binding_do_not_select_an_implicit_host_platform(self):
        with self.assertRaisesRegex(ValueError, 'unknown or ambiguous platform'):
            PLATFORM.resolve('native')
        self.profile('legacy', 'execution="std"\n[hal]\ncamera="native"\n')
        with self.assertRaisesRegex(ValueError, 'unsupported camera implementation'):
            PLATFORM.resolve('legacy', self.root)

    def test_os_specific_binding_is_checked_without_a_profile_os_constraint(self):
        self.profile('desktop', 'execution="std"\n[hal]\nwifi="macos"\n')
        profile = PLATFORM.resolve('desktop', self.root)
        PLATFORM.validate_target(profile, 'macos')
        with self.assertRaisesRegex(ValueError, 'wifi/macos does not support target OS linux'):
            PLATFORM.validate_target(profile, 'linux')

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
                        '[hal]\nimu="mock"\nimu="std"\n', 'hal-features=["nxrs-imu/mock"]\n'):
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

    def test_std_is_neither_a_platform_nor_a_hal_binding(self):
        with self.assertRaisesRegex(ValueError, 'unknown or ambiguous platform'):
            PLATFORM.resolve('std')
        self.profile('legacy-std', '[hal]\ncamera="std"\n')
        with self.assertRaisesRegex(ValueError, 'unsupported camera implementation'):
            PLATFORM.resolve('legacy-std', self.root)

    def test_enabled_cfg_has_no_platform_identity_and_unbound_has_no_cfg(self):
        for name in ('linux', 'macos', 'windows'):
            profile = PLATFORM.resolve(name)
            for capability in ('camera', 'storage', 'transport', 'detector'):
                self.assertEqual(profile['hal'][capability], name)
                cfgs = [line for line in PLATFORM.cargo_output(profile, capability, name)
                        if line.startswith('cargo:rustc-cfg=')]
                self.assertEqual(cfgs, ['cargo:rustc-cfg=hal_enabled'])
                for wrong in ('none', 'unknown', 'nuttx'):
                    with self.assertRaises(ValueError):
                        PLATFORM.validate_target(profile, wrong)

    def test_real_os_bindings_cannot_target_bare_metal(self):
        self.profile('device', '[hal]\ncamera="nuttx"\nstorage="nuttx"\n')
        profile = PLATFORM.resolve('device', self.root)
        with self.assertRaisesRegex(ValueError, 'does not support target OS none'):
            PLATFORM.validate_target(profile, 'none')

    def test_platform_os_constraint_is_inherited_and_checked_before_bindings(self):
        self.profile('macos', 'execution="std"\ntarget-os=["macos"]\n[hal]\nwifi="macos"\n')
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
        self.assertFalse(any(line.startswith('cargo:rustc-cfg=') for line in output))
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
        directory = self.root / 'hal/camera/src/platform/nuttx/ffi'
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

    def test_detector_model_dependencies_follow_product_and_transitive_service(self):
        service = self.root / 'service/detection/Cargo.toml'
        service.parent.mkdir(parents=True)
        service.write_text('[package]\nname="detection"\n[dependencies]\nvision={path="../../hal/detector"}\n')
        self.app.write_text('[package]\nname="demo"\n[dependencies]\nservice={path="../../service/detection"}\n')
        self.profile('camera', '[hal]\ndetector="linux"\n[detector]\nmodel="pico"\nstack-bytes=131072\nsample-ms=750\n')
        self.profile('vww', 'extends="camera"\n[detector]\nmodel="vww"\nstack-bytes=524288\nsample-ms=2000\n')
        self.profile('video', 'extends="camera"\ndetector={}\n[hal]\ndetector="none"\n')
        for name, feature in [('camera', 'pico-model'), ('vww', 'person-model')]:
            with self.subTest(platform=name):
                result = CHECK.validate(self.app, name, 'linux')
                self.assertEqual(result['model_features'], [f'nxrs-detector/{feature}'])
                self.assertEqual(len(result['profiles']), 1 if name == 'camera' else 2)
        result = CHECK.validate(self.app, 'video', 'linux')
        self.assertIsNone(result['detector'])
        self.assertEqual(result['model_features'], [])
        self.assertEqual(PLATFORM.detector_config(PLATFORM.resolve('camera', self.root))['model'], 'pico')

    def test_detector_dependencies_are_absent_without_a_consuming_product(self):
        self.profile('camera', '[hal]\ndetector="linux"\n[detector]\nmodel="pico"\nstack-bytes=131072\nsample-ms=750\n')
        original = self.app.read_text()
        for extra in ('', '[dev-dependencies]\nvision={path="../../hal/detector"}\n',
                      '[target.\'cfg(unix)\'.dev-dependencies]\nvision={path="../../hal/detector"}\n',
                      '[target.\'cfg(unix)\'.dependencies]\nvision={path="../../hal/detector",optional=true}\n'):
            with self.subTest(extra=extra):
                self.app.write_text(original + extra)
                self.assertEqual(CHECK.validate(self.app, 'camera', 'linux')['model_features'], [])
        self.app.write_text(original + '[target.\'cfg(unix)\'.dependencies]\nvision={path="../../hal/detector"}\n')
        self.assertEqual(CHECK.validate(self.app, 'camera', 'linux')['model_features'], ['nxrs-detector/pico-model'])

    def test_invalid_detector_configuration_is_rejected(self):
        for config in ('model="unknown"\nstack-bytes=1\nsample-ms=1',
                       'model=[]\nstack-bytes=1\nsample-ms=1',
                       'model="pico"\nstack-bytes=0\nsample-ms=750',
                       'model="pico"\nstack-bytes=true\nsample-ms=750',
                       'model="pico"\nstack-bytes=131072\nsample-ms=-1',
                       'model="pico"\nstack-bytes=131072\nsample-ms=4294967296',
                       'model="pico"\nsample-ms=750',
                       'model="pico"\nstack-bytes=131072\nsample-ms=750\nfeature="extra"'):
            self.profile('bad-model', '[hal]\ndetector="linux"\n[detector]\n' + config + '\n')
            with self.subTest(config=config), self.assertRaises(ValueError):
                PLATFORM.resolve('bad-model', self.root)
        self.profile('unbound', '[detector]\nmodel="pico"\nstack-bytes=131072\nsample-ms=750\n')
        with self.assertRaisesRegex(ValueError, 'requires a detector HAL'):
            PLATFORM.resolve('unbound', self.root)


if __name__ == '__main__':
    unittest.main()
