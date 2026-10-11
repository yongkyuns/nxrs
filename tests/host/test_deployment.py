"""Negative controls for capability-local HAL provider validation."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "tools/check-deployment.py"
SPEC = importlib.util.spec_from_file_location("deployment", SOURCE)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "Cargo.toml").write_text('[workspace]\nresolver = "2"\n')
        (self.root / "app/demo").mkdir(parents=True)
        self.app_manifest = self.root / "app/demo/Cargo.toml"
        self.app_manifest.write_text(
            '[package]\nname = "demo-app"\nversion = "0.1.0"\n'
            '[dependencies]\nimu = { path = "../../hal/imu" }\n'
        )
        for capability in ("imu", "gnss"):
            facade = self.root / "hal" / capability
            provider = facade / "mock"
            api = facade / "api"
            provider.mkdir(parents=True)
            api.mkdir()
            package = f"demo-{capability}-mock"
            (provider / "Cargo.toml").write_text(
                f'[package]\nname = "{package}"\nversion = "0.1.0"\n'
                '[package.metadata.nxrs]\nmode = "test"\nplatforms = ["std"]\n'
            )
            (api / "Cargo.toml").write_text(
                f'[package]\nname = "demo-{capability}-api"\nversion = "0.1.0"\n'
            )
            (facade / "Cargo.toml").write_text(
                f'[package]\nname = "demo-{capability}"\nversion = "0.1.0"\n'
                '[package.metadata.nxrs]\nkind = "hal-capability"\n'
                'provider-features = ["mock"]\n'
                '[features]\ndefault = []\nmock = ["dep:provider"]\n'
                '[dependencies]\n'
                f'provider = {{ package = "{package}", path = "mock", optional = true }}\n'
                f'api = {{ package = "demo-{capability}-api", path = "api" }}\n'
            )

    def validate(self, features=None, platform="std"):
        return CHECK.validate(
            self.app_manifest,
            features or ["demo-imu/mock", "demo-gnss/mock"],
            platform,
        )

    def test_capability_local_mock_selection_is_valid(self):
        result = self.validate()
        self.assertEqual(
            {provider["capability"] for provider in result["providers"]},
            {"imu", "gnss"},
        )

    def test_application_may_access_facade_and_contract(self):
        self.app_manifest.write_text(
            '[package]\nname = "demo-app"\nversion = "0.1.0"\n'
            '[dependencies]\n'
            'imu = { path = "../../hal/imu" }\n'
            'imu-api = { path = "../../hal/imu/api" }\n'
        )
        self.validate()

    def test_application_cannot_select_concrete_provider(self):
        self.app_manifest.write_text(
            '[package]\nname = "demo-app"\nversion = "0.1.0"\n'
            '[dependencies]\nimu = { path = "../../hal/imu/mock" }\n'
        )
        with self.assertRaisesRegex(ValueError, "selects concrete HAL implementation"):
            self.validate()

    def test_capability_cannot_select_two_provider_features(self):
        with self.assertRaisesRegex(ValueError, "multiple provider selections for capability imu"):
            self.validate(["demo-imu/mock", "demo-imu/mock"])

    def test_unknown_provider_feature_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not a declared provider selection"):
            self.validate(["demo-imu/native"])

    def test_provider_must_support_execution_environment(self):
        with self.assertRaisesRegex(ValueError, "does not declare execution platform browser"):
            self.validate(["demo-imu/mock"], "browser")

    def test_real_wifi_provider_is_nuttx_only_not_generic_std(self):
        root = SOURCE.parents[1]
        app = root / 'tests/nuttx-std/wifi/Cargo.toml'
        result = CHECK.validate(app, ['nxrs-wifi/nuttx'], 'nuttx')
        self.assertEqual(result['providers'][0]['package'], 'nxrs-wifi-nuttx')
        for platform in ('std', 'macos', 'linux'):
            with self.subTest(platform=platform):
                with self.assertRaisesRegex(ValueError, 'does not declare execution platform'):
                    CHECK.validate(app, ['nxrs-wifi/nuttx'], platform)

    def test_existing_nuttx_sensor_mock_profiles_remain_valid(self):
        root = SOURCE.parents[1]
        CHECK.validate(root / 'app/event-demo/Cargo.toml',
                       ['nxrs-imu/mock', 'nxrs-gnss/mock'], 'nuttx')


    def test_nested_qualification_app_uses_enclosing_workspace(self):
        self.app_manifest = self.root / "tests/rtos-bench/rust/Cargo.toml"
        self.app_manifest.parent.mkdir(parents=True)
        self.app_manifest.write_text('[package]\nname = "nested-probe"\n')
        self.assertEqual(len(self.validate()["providers"]), 2)
        self.app_manifest.write_text(
            '[package]\nname = "nested-probe"\n[dependencies]\n'
            'imu = { path = "../../../hal/imu/mock" }\n'
        )
        with self.assertRaisesRegex(ValueError, "selects concrete HAL implementation"):
            self.validate()

    def test_missing_workspace_fails_explicitly(self):
        (self.root / "Cargo.toml").unlink()
        with self.assertRaisesRegex(ValueError, "no enclosing Cargo workspace"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
