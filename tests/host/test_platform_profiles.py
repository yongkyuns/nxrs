"""Cargo firmware frontend and NuttX platform-profile checks."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ROOT / "platform/nuttx/platforms"

# An independent expected inventory, not discovery using the frontend under test.
# Keep exact equality so missing, duplicate and unexpected apps cannot pass.
FIRMWARE_APPS = {
    "event-demo": ("nxrs-event-demo", "event-demo", "event_demo"),
    "dual-imu-demo": ("nxrs-dual-imu-demo", "dual-imu-demo", "dual_imu_demo"),
    "std-demo": ("nxrs-std-demo", "std-demo", "std_demo"),
    "ao-stress": ("nxrs-ao-stress", "ao-stress", "ao_stress"),
}
PLATFORM_NAMES = {
    "pico2-mock",
    "mps2-an521-mock",
    "esp32s3-qemu-mock",
    "esp32s3-service-footprint",
}

APP_OWNED = (
    "NXRS_DEPLOYMENT",
    "NXRS_OUT_REL",
    "NXRS_APP_MANIFEST",
    "NXRS_APP_PACKAGE",
    "NXRS_APP_BIN",
    "NXRS_APP_COMMAND",
    "NXRS_APP_PRIORITY",
    "NXRS_APP_STACKSIZE",
)


class FirmwareFrontendTests(unittest.TestCase):
    def cargo_firmware(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["cargo", "+1.90.0", "firmware", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_firmware_apps_keep_build_metadata_in_cargo(self):
        for app, (package, binary, command) in FIRMWARE_APPS.items():
            with self.subTest(app=app):
                manifest = ROOT / "app" / app / "Cargo.toml"
                data = tomllib.loads(manifest.read_text())
                self.assertEqual(data["package"]["name"], package)
                firmware = data["package"]["metadata"]["nxrs"]["firmware"]
                self.assertEqual(firmware["bin"], binary)
                self.assertEqual(firmware["command"], command)
                self.assertGreater(firmware["priority"], 0)
                self.assertGreater(firmware["stack-size"], 0)

    def test_platforms_are_declarative_and_own_hardware_not_app_policy(self):
        profiles = sorted(PLATFORMS.glob("*.toml"))
        self.assertTrue(profiles)
        for platform in profiles:
            with self.subTest(platform=platform.name):
                data = tomllib.loads(platform.read_text())
                for key in ("board", "target", "crossdev", "machine", "image"):
                    self.assertIsInstance(data.get(key), str, f"{platform} missing {key}")
                    self.assertTrue(data[key])
                features = data.get("hal-features")
                self.assertIsInstance(features, list)
                self.assertTrue(all(isinstance(value, str) and "/" in value for value in features))
                if not features:
                    self.assertFalse(data.get("requires-qemu", True), f"{platform} needs HAL selections")
                kconfig = data.get("kconfig")
                self.assertIsInstance(kconfig, dict)
                for key in ("enable", "disable", "set", "require"):
                    self.assertIsInstance(kconfig.get(key), list)
                    self.assertTrue(all(isinstance(value, str) for value in kconfig[key]))
                self.assertIsInstance(kconfig.get("forbid-regex"), str)
                text = platform.read_text()
                for name in APP_OWNED:
                    self.assertNotIn(name, text, f"{platform} owns app policy {name}")

    def test_cargo_frontend_help_is_successful(self):
        output = self.cargo_firmware("--help").stdout
        self.assertIn("cargo firmware --app <app> --platform <platform>", output)

    def test_cargo_frontend_lists_apps_and_platforms(self):
        apps = self.cargo_firmware("--list-apps").stdout.splitlines()
        self.assertEqual(apps, sorted(FIRMWARE_APPS))

        platforms = self.cargo_firmware("--list-platforms").stdout.splitlines()
        self.assertEqual(platforms, sorted(PLATFORM_NAMES))

    def test_cargo_frontend_resolves_app_plus_platform_to_backend_arguments(self):
        for app, (package, binary, command) in FIRMWARE_APPS.items():
            for platform in sorted(PLATFORM_NAMES):
                with self.subTest(app=app, platform=platform):
                    output = self.cargo_firmware(
                        "--app",
                        app,
                        "--platform",
                        platform,
                        "--dry-run",
                    ).stdout
                    self.assertIn("build-nuttx-std-app.sh", output)
                    self.assertIn(f'--app-package" "{package}', output)
                    self.assertIn(f'--bin" "{binary}', output)
                    self.assertIn(f'--command" "{command}', output)
                    self.assertIn(f'--platform" "{platform}', output)
                    self.assertIn(f"target/firmware/{app}/{platform}", output)

    def test_invalid_app_or_platform_is_rejected_before_backend(self):
        for args in [
            ("--app", "../event-demo", "--platform", "pico2-mock"),
            ("--app", "event-demo", "--platform", "../pico2"),
            ("--app", "nxrs", "--platform", "pico2-mock"),
        ]:
            with self.subTest(args=args):
                result = subprocess.run(
                    ["cargo", "+1.90.0", "firmware", *args, "--dry-run"],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
