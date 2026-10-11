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
    "pico2w-wifi",
    "pico2w-ble",
    "mps2-an521-mock",
    "esp32s3-qemu-mock",
    "esp32s3-service-footprint",
    "esp32s3-wifi",
    "esp32s3-ble",
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
    def test_workflows_do_not_invoke_retired_provider_selection_cli(self):
        for workflow in (ROOT / '.github/workflows').glob('*.yml'):
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text()
                self.assertNotIn('--execution-platform', text)
                self.assertNotIn('--hal-feature', text)

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
                features = data.get("hal")
                self.assertIsInstance(features, dict)
                # Hardware qualification does not implicitly select sensor mocks.
                expected = {"esp32s3-service-footprint": {},
                            "esp32s3-wifi": {"wifi": "nuttx"},
                            "esp32s3-ble": {"ble": "nuttx"},
                            "pico2w-wifi": {"wifi": "nuttx"},
                            "pico2w-ble": {"ble": "nuttx"}}
                self.assertEqual(features, expected.get(platform.stem,
                                 {"imu": "mock", "gnss": "mock"}))
                self.assertNotIn("hal-features", data)
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

    def test_wifi_scan_profile_has_driver_resources_but_no_boot_credentials(self):
        data = tomllib.loads((PLATFORMS / 'esp32s3-wifi.toml').read_text())
        config = data['kconfig']
        for feature in ('SCHED_LPWORK', 'PTHREAD_MUTEX_TYPES', 'SYSLOG_CONSOLE',
                        'DRIVERS_WIRELESS', 'DRIVERS_IEEE80211',
                        'NETDEV_WIRELESS_IOCTL', 'NETDEV_WIRELESS_HANDLER',
                        'NETDEV_LATEINIT', 'TIMER', 'PTHREAD_MUTEX_TYPES',
                        'SYSLOG_CONSOLE'):
            self.assertIn(feature, config['enable'])
        for feature in ('NETDEV_IOCTL', 'NETDEV_WIRELESS_IOCTL',
                        'NETDEV_WIRELESS_HANDLER', 'DRIVERS_IEEE80211',
                        'NETDEV_LATEINIT', 'TIMER'):
            self.assertIn('CONFIG_' + feature + '=y', config['require'])
        for setting in ('TLS_TASK_NELEM=1', 'SCHED_LPWORKSTACKSIZE=4096',
                        'ESPRESSIF_WIFI_SCAN_RESULT_SIZE=4096',
                        'IOB_NBUFFERS=100', 'IOB_BUFSIZE=400', 'NET_ETH_PKTSIZE=1514'):
            self.assertIn(setting, config['set'])
            self.assertIn('CONFIG_' + setting, config['require'])
        for feature in ('WIRELESS_WAPI', 'NETINIT_DHCPC', 'ESPRESSIF_BLE'):
            self.assertIn(feature, config['disable'])
        self.assertIn('NETUTILS_DHCPC', config['enable'])
        self.assertIn('NETDB_DNSCLIENT', config['enable'])
        self.assertIn('CONFIG_NET_BINDTODEVICE=y', config['require'])
        self.assertIn('SERIAL_TERMIOS', config['enable'])
        self.assertIn('CONFIG_SERIAL_TERMIOS=y', config['require'])
        for setting in ('ARP_SEND_DELAYMSEC=200', 'ARP_SEND_MAXTRIES=5'):
            self.assertIn(setting, config['set'])
            self.assertIn('CONFIG_' + setting, config['require'])
        for feature in ('DEBUG_WIRELESS_INFO', 'DEBUG_NET_INFO'):
            self.assertIn(feature, config['disable'])
        self.assertFalse(any('SSID' in value or 'PASSPHRASE' in value
                             for value in config['set']))

    def test_cargo_frontend_lists_apps_and_platforms(self):
        apps = self.cargo_firmware("--list-apps").stdout.splitlines()
        self.assertEqual(apps, sorted(FIRMWARE_APPS))

        platforms = self.cargo_firmware("--list-platforms").stdout.splitlines()
        self.assertEqual(platforms, sorted(PLATFORM_NAMES))

    def test_pico_radio_profile_excludes_gpio25_led_and_boot_credentials(self):
        data = tomllib.loads((PLATFORMS / "pico2w-wifi.toml").read_text())
        self.assertEqual(data["radio-firmware"], "cyw43439")
        self.assertFalse(data["requires-qemu"])
        config = data["kconfig"]
        for disabled in ("ARCH_LEDS", "USERLED", "NETINIT_DHCPC", "WIRELESS_WAPI",
                         "DEBUG_WIRELESS_INFO", "DEBUG_NET_INFO"):
            self.assertIn(disabled, config["disable"])
        self.assertIn("RP23XX_INFINEON_CYW43439", config["enable"])
        self.assertIn("NETDB_DNSCLIENT", config["enable"])
        self.assertIn("CONFIG_NETDB_DNSCLIENT=y", config["require"])
        for feature in ("SYSLOG_CDCACM", "DEBUG_WIRELESS_ERROR"):
            self.assertIn(feature, config["enable"])
            self.assertIn("CONFIG_" + feature + "=y", config["require"])
        # DEV_CONSOLE activates physical UART init on this pinned ARM port.
        self.assertIn("DEV_CONSOLE", config["disable"])
        self.assertIn("SYSLOG_CONSOLE", config["disable"])
        self.assertFalse(any("SSID" in setting or "PASSPHRASE" in setting or
                             "FIRMWARE_BIN_PATH" in setting for setting in config["set"]))

    def test_usb_bootloader_trigger_is_limited_to_pico_radio_development(self):
        for path in PLATFORMS.glob("*.toml"):
            config = tomllib.loads(path.read_text())["kconfig"]
            enabled = path.stem in ("pico2w-wifi", "pico2w-ble")
            self.assertEqual("CDCACM_1200BPS_BOOTLOADER" in config["enable"], enabled)
            if enabled:
                for symbol in ("BOARD_USBDEV_BOOTLOADER", "CDCACM_1200BPS_BOOTLOADER"):
                    self.assertIn(f"CONFIG_{symbol}=y", config["require"])

    def test_station_profiles_accept_the_broadcast_dhcp_replies_they_request(self):
        for platform in ("esp32s3-wifi", "pico2w-wifi"):
            with self.subTest(platform=platform):
                config = tomllib.loads((PLATFORMS / f"{platform}.toml").read_text())["kconfig"]
                self.assertIn("NETUTILS_DHCPC_BOOTP_FLAGS=0x8000", config["set"])
                self.assertIn("NET_BROADCAST", config["enable"])
                self.assertIn("CONFIG_NET_BROADCAST=y", config["require"])
                for setting in ("ARP_SEND_DELAYMSEC=200", "ARP_SEND_MAXTRIES=5"):
                    self.assertIn(setting, config["set"])
                    self.assertIn("CONFIG_" + setting, config["require"])

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
