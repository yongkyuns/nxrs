"""Cargo firmware frontend and NuttX platform-profile checks."""
from __future__ import annotations

from pathlib import Path
import importlib.util
from itertools import product
import shlex
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ROOT / "platform/nuttx/platforms"
_SPEC = importlib.util.spec_from_file_location('platform_config', ROOT / 'tools/platform_config.py')
PLATFORM_CONFIG = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PLATFORM_CONFIG)

# An independent expected inventory, not discovery using the frontend under test.
# Keep exact equality so missing, duplicate and unexpected apps cannot pass.
FIRMWARE_APPS = {
    "home-cam": ("nxrs-home-cam", "home-cam", "home_cam"),
    "event-demo": ("nxrs-event-demo", "event-demo", "event_demo"),
    "dual-imu-demo": ("nxrs-dual-imu-demo", "dual-imu-demo", "dual_imu_demo"),
    "std-demo": ("nxrs-std-demo", "std-demo", "std_demo"),
    "ao-stress": ("nxrs-ao-stress", "ao-stress", "ao_stress"),
}
PLATFORM_NAMES = {
    "pico2-mock",
    "pico2-tls",
    "pico2w-wifi",
    "pico2w-ble",
    "mps2-an521-mock",
    "esp32s3-qemu-mock",
    "esp32s3-service-footprint",
    "esp32s3-wifi",
    "esp32s3-ble",
    "esp32s3-radio",
    "esp32s3-camera",
    "esp32s3-camera-stream",
    "esp32s3-camera-stream-tls",
    "esp32s3-camera-stream-mcuboot",
    "esp32s3-camera-stream-startup",
    "esp32s3-camera-stream-protected-build",
    "esp32s3-camera-stream-vww",
    "esp32s3-camera-stream-video",
    "esp32s3-camera-tls",
    "pico2w-radio",
    "m5stack-cores3-gnss",
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
    def test_provider_headers_are_staged_checked_and_hashed(self):
        script = (ROOT / 'tools/build-nuttx-std-app.sh').read_text()
        begin = script.index('  for header in "$(dirname "$source_path")"/*.h')
        end = script.index('  NXRS_TARGET_C_OBJECTS+=', begin)
        header_loop = script[begin:end]
        self.assertIn('HASH_INPUTS+=(${NXRS_TARGET_C_SOURCES[@]+"${NXRS_TARGET_C_SOURCES[@]}"})', script)
        self.assertIn('HASH_INPUTS+=("$header")', script)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'src/platform/nuttx/ffi/helper.c'
            source.parent.mkdir(parents=True)
            source.touch()
            (source.parent / 'local.h').write_text('/* local ABI */\n')
            app = root / 'app'
            app.mkdir()
            command = ['bash', '-euc', 'source_path=$1; APP=$2;\n' + header_loop,
                       'stage-headers', str(source), str(app)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((app / 'local.h').read_bytes(), (source.parent / 'local.h').read_bytes())
            (app / 'local.h').write_text('/* incompatible ABI */\n')
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Conflicting target helper header', result.stderr)

    def test_final_native_configuration_is_recorded_after_build_and_hashed(self):
        script = (ROOT / 'tools/build-nuttx-std-app.sh').read_text()
        final_build = script.index('make -C "$OUT/nuttx" -j"$NXRS_NUTTX_BUILD_JOBS"')
        final_config = script.index('cp "$OUT/nuttx/.config" "$OUT/resolved.config"')
        hashes = script.index('HASH_INPUTS=(')
        self.assertLess(final_build, final_config)
        self.assertLess(final_config, hashes)
        self.assertIn('"$OUT/resolved.config"', script[hashes:].splitlines()[0])

    def test_invalid_diagnostic_symbols_fail_before_build_inputs_are_touched(self):
        for option, symbol in product(
                ("--kconfig-enable", "--kconfig-disable"),
                ("", "lowercase", "FOO BAR", "FOO=1", "-FOO")):
            with self.subTest(option=option, symbol=symbol):
                result = subprocess.run(
                    ["bash", str(ROOT / 'tools/build-nuttx-std-app.sh'),
                     option, symbol], cwd=ROOT, text=True,
                    capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid Kconfig symbol:", result.stderr)

    def test_omitted_diagnostic_symbols_keep_normal_argument_validation(self):
        result = subprocess.run(
            ["bash", str(ROOT / 'tools/build-nuttx-std-app.sh')], cwd=ROOT,
            text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing required argument", result.stderr)
        self.assertNotIn("unbound", result.stderr)

    def test_valid_diagnostic_symbols_reach_normal_argument_validation(self):
        result = subprocess.run(
            ["bash", str(ROOT / 'tools/build-nuttx-std-app.sh'),
             "--kconfig-enable", "RP23XX_CYW43439_READ_OBSERVER",
             "--kconfig-enable", "NET_STATISTICS",
             "--kconfig-disable", "NSH_NETINIT",
             "--kconfig-disable", "NETUTILS_NETINIT"], cwd=ROOT,
            text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing required argument", result.stderr)
        self.assertNotIn("Invalid Kconfig symbol", result.stderr)

    def test_conflicting_diagnostic_symbols_fail_before_build_inputs_are_touched(self):
        result = subprocess.run(
            ["bash", str(ROOT / 'tools/build-nuttx-std-app.sh'),
             "--kconfig-enable", "NSH_NETINIT", "--kconfig-disable", "NSH_NETINIT"],
            cwd=ROOT, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Conflicting Kconfig request: NSH_NETINIT", result.stderr)

    def test_native_builder_compiles_the_manifest_it_validates(self):
        script = (ROOT / 'tools/build-nuttx-std-app.sh').read_text().replace('\\\n', ' ')
        command = next(line for line in script.splitlines() if line.startswith('"$CARGO_BIN" '))
        arguments = shlex.split(command)
        self.assertIn('--manifest-path', arguments)
        self.assertEqual(arguments[arguments.index('--manifest-path') + 1], '$NXRS_APP_MANIFEST')

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
                self.assertGreater(firmware.get("priority", 100), 0)
                self.assertGreater(firmware.get("stack-size", 32768), 0)

    def test_platforms_are_declarative_and_own_hardware_not_app_policy(self):
        profiles = sorted(PLATFORMS.glob("*.toml"))
        self.assertTrue(profiles)
        for platform in profiles:
            with self.subTest(platform=platform.name):
                data = PLATFORM_CONFIG.resolve(platform.stem)
                for key in ("board", "target", "crossdev", "machine", "image"):
                    self.assertIsInstance(data.get(key), str, f"{platform} missing {key}")
                    self.assertTrue(data[key])
                features = data.get("hal")
                self.assertIsInstance(features, dict)
                # Hardware qualification does not implicitly select sensor mocks.
                expected = {"m5stack-cores3-gnss": {"gnss": "nuttx", "imu": "nuttx"},
                            "esp32s3-service-footprint": {},
                            "pico2-tls": {"tls": "nuttx"},
                            "esp32s3-camera": {"camera": "nuttx"},
                            "esp32s3-camera-stream": {"camera": "nuttx", "wifi": "nuttx", "detector": "nuttx"},
                            "esp32s3-camera-stream-tls": {"camera": "nuttx", "wifi": "nuttx", "detector": "nuttx", "tls": "nuttx"},
                            "esp32s3-camera-stream-mcuboot": {"camera": "nuttx", "wifi": "nuttx", "detector": "nuttx", "tls": "nuttx", "boot": "nuttx"},
                            "esp32s3-camera-stream-startup": {"camera": "nuttx", "wifi": "nuttx", "detector": "nuttx", "tls": "nuttx", "boot": "nuttx", "watchdog": "nuttx", "config": "nuttx", "console": "nuttx", "execution": "nuttx"},
                            "esp32s3-camera-stream-protected-build": {"camera": "nuttx", "wifi": "nuttx", "detector": "nuttx", "tls": "nuttx", "boot": "none"},
                            "esp32s3-camera-stream-vww": {"camera": "nuttx", "wifi": "nuttx", "detector": "nuttx"},
                            "esp32s3-camera-stream-video": {"camera": "nuttx", "wifi": "nuttx", "detector": "none"},
                            "esp32s3-camera-tls": {"camera": "nuttx", "wifi": "nuttx", "detector": "none", "tls": "nuttx"},
                            "esp32s3-wifi": {"wifi": "nuttx"},
                            "esp32s3-ble": {"ble": "nuttx"},
                            "esp32s3-radio": {"wifi": "nuttx", "ble": "nuttx"},
                            "pico2w-radio": {"wifi": "nuttx", "ble": "nuttx"},
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

    def test_cores3_sensor_resources_and_invalid_bindings(self):
        data = PLATFORM_CONFIG.resolve("m5stack-cores3-gnss")
        self.assertEqual(PLATFORM_CONFIG.gnss_config(data),
                         {"device": "/dev/ttyS1", "baud": 38400})
        self.assertEqual(PLATFORM_CONFIG.imu_config(data),
                         {"driver": "bmi270", "device": "/dev/i2c0",
                          "address": 0x68, "frequency": 400000})
        for function, key, invalid in (
                (PLATFORM_CONFIG.gnss_config, "gnss", {"baud": True}),
                (PLATFORM_CONFIG.gnss_config, "gnss", {"device": "/dev/../ttyS1"}),
                (PLATFORM_CONFIG.imu_config, "imu", {"address": 0x70}),
                (PLATFORM_CONFIG.imu_config, "imu", {"frequency": 0})):
            with self.subTest(key=key, invalid=invalid):
                with self.assertRaises(ValueError):
                    function({**data, key: {**data[key], **invalid}})
        for function, key in ((PLATFORM_CONFIG.gnss_config, "gnss"),
                              (PLATFORM_CONFIG.imu_config, "imu")):
            with self.assertRaises(ValueError):
                function({**data, "hal": {**data["hal"], key: "mock"}})

    def test_cargo_frontend_help_is_successful(self):
        output = self.cargo_firmware("--help").stdout
        self.assertIn("cargo firmware --app <app> --platform <platform>", output)

    def test_radio_profiles_keep_both_transports_and_station_requirements(self):
        for board in ("esp32s3", "pico2w"):
            with self.subTest(board=board):
                config = tomllib.loads((PLATFORMS / f'{board}-radio.toml').read_text())['kconfig']
                self.assertFalse(set(config['enable']) & set(config['disable']))
                for feature in ('NET_IPv4', 'NET_UDP', 'NET_BROADCAST',
                                'WIRELESS_BLUETOOTH_HOST', 'NET_BLUETOOTH', 'NETUTILS_DHCPC'):
                    self.assertIn('CONFIG_' + feature + '=y', config['require'])
                for setting in ('BLUETOOTH_MAX_CONN=1', 'PREALLOC_MQ_MSGS=32'):
                    self.assertIn('CONFIG_' + setting, config['require'])
                self.assertNotIn('IEEE80211_BROADCOM_FRAME_TRACE', config['enable'])
        esp_profile = tomllib.loads((PLATFORMS / 'esp32s3-radio.toml').read_text())
        self.assertEqual(esp_profile['image'], 'nuttx.bin')
        self.assertFalse(esp_profile['requires-qemu'])
        esp = esp_profile['kconfig']
        self.assertIn('ESP32S3_QEMU_IMAGE', esp['disable'])
        self.assertNotIn('ESP32S3_QEMU_IMAGE', esp['enable'])
        self.assertIn('CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y', esp['require'])
        self.assertIn('CONFIG_ESPRESSIF_WIFI_BT_COEXIST=y', esp['require'])
        self.assertIn('BOARD_INITTHREAD_STACKSIZE=8192', esp['set'])
        self.assertIn('CONFIG_BOARD_INITTHREAD_STACKSIZE=8192', esp['require'])

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

    def test_camera_profile_selects_standard_capture_and_freenove_wiring(self):
        data = tomllib.loads((PLATFORMS / 'esp32s3-camera.toml').read_text())
        self.assertEqual(data['hal'], {'camera': 'nuttx'})
        self.assertEqual(data['image'], 'nuttx.bin')
        config = data['kconfig']
        for feature in ('ESP32S3_CAM', 'VIDEO_STREAM', 'VIDEO_OV3660',
                        'ESP32S3_I2C0', 'ESP32S3_DEVKIT_FREENOVE_CAMERA'):
            self.assertIn(feature, config['enable'])
            self.assertIn('CONFIG_' + feature + '=y', config['require'])
        self.assertIn('ESP32S3_CAMERA', config['disable'])
        for pin, value in {'XCLK': 15, 'PCLK': 13, 'VSYNC': 6, 'HREF': 7,
                           'D0': 11, 'D1': 9, 'D2': 8, 'D3': 10,
                           'D4': 12, 'D5': 18, 'D6': 17, 'D7': 16}.items():
            self.assertIn(f'ESP32S3_CAM_{pin}_PIN={value}', config['set'])
        self.assertIn('ESP32S3_I2C0_SDAPIN=4', config['set'])
        self.assertIn('ESP32S3_I2C0_SCLPIN=5', config['set'])
        self.assertIn('ESP32S3_CAM_XCLK_FREQUENCY=16000000', config['set'])
        # PSRAM must actually back malloc; DMA descriptors remain kernel/internal.
        for feature in ('ESP32S3_SPIRAM_USER_HEAP', 'MM_KERNEL_HEAP', 'SCHED_LPWORK'):
            self.assertIn('CONFIG_' + feature + '=y', config['require'])

    def test_stream_profile_preserves_camera_wiring_and_enables_only_station_radio(self):
        base = tomllib.loads((PLATFORMS / 'esp32s3-camera.toml').read_text())
        data = tomllib.loads((PLATFORMS / 'esp32s3-camera-stream.toml').read_text())
        self.assertEqual(data['extends'], 'esp32s3-camera')
        self.assertEqual(data['hal'], {'wifi': 'nuttx', 'detector': 'nuttx'})
        config = data['kconfig']
        for setting in base['kconfig']['set']:
            if setting.startswith(('ESP32S3_CAM_', 'ESP32S3_I2C0_')):
                self.assertIn(setting, config['set'])
        for feature in ('ESP32S3_CAM', 'VIDEO_OV3660', 'ESPRESSIF_WIFI',
                        'ESPRESSIF_WIFI_STATION', 'NET_TCP', 'NET_UDP',
                        'ESP32S3_SPIRAM_USER_HEAP', 'SCHED_CRITMONITOR',
                        'ARCH_PERF_EVENTS', 'PERF_OVERFLOW_CORRECTION', 'NET_TCP_WRITE_BUFFERS',
                        'ESP32S3_INSTRUCTION_CACHE_32KB', 'NET_TCP_SELECTIVE_ACK',
                        'ESP32S3_SPIRAM_SPEED_80M', 'ESP32S3_DATA_CACHE_64KB',
                        'ESP32S3_DATA_CACHE_LINE_64B', 'ESP32S3_SPIRAM_MEMTEST',
                        'NET_TCP_OUT_OF_ORDER'):
            self.assertIn(feature, config['enable'])
            self.assertIn('CONFIG_' + feature + '=y', config['require'])
        for feature in ('ESPRESSIF_BLE', 'NETINIT_DHCPC', 'ESP32S3_QEMU_IMAGE'):
            self.assertIn(feature, config['disable'])
        self.assertFalse(any('SSID' in value or 'PASSPHRASE' in value for value in config['set']))
        for setting in ('NET_SEND_BUFSIZE=16384', 'NET_MAX_SEND_BUFSIZE=16384',
                        'ESP32S3_CAM_XCLK_DRIVE_STRENGTH=0',
                        'NET_TCP_PREALLOC_CONNS=16', 'NET_TCP_ALLOC_CONNS=0',
                        'NET_TCP_NWRBCHAINS=16', 'NET_TCP_ALLOC_WRBCHAINS=0',
                        'NET_PREALLOC_DEVIF_CALLBACKS=64', 'NET_ALLOC_DEVIF_CALLBACKS=0',
                        'NET_TCP_OUT_OF_ORDER_BUFSIZE=4096'):
            self.assertIn(setting, config['set'])
            self.assertIn('CONFIG_' + setting, config['require'])
        for derived in ('ESP32S3_SPIRAM_SPEED=80', 'ESP32S3_DATA_CACHE_SIZE=0x10000',
                        'ESP32S3_DATA_CACHE_LINE_SIZE=64'):
            self.assertIn('CONFIG_' + derived, config['require'])

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
            config = PLATFORM_CONFIG.resolve(path.stem)["kconfig"]
            enabled = path.stem in ("pico2w-wifi", "pico2w-ble", "pico2w-radio")
            self.assertEqual("CDCACM_1200BPS_BOOTLOADER" in config["enable"], enabled)
            if enabled:
                for symbol in ("BOARD_USBDEV_BOOTLOADER", "CDCACM_1200BPS_BOOTLOADER"):
                    self.assertIn(f"CONFIG_{symbol}=y", config["require"])

    def test_radio_profiles_omit_retired_instrumentation_and_keep_bounded_logs(self):
        for platform in ("pico2w-radio", "esp32s3-radio"):
            config = tomllib.loads((PLATFORMS / f"{platform}.toml").read_text())["kconfig"]
            for symbol in ("NET_STATISTICS", "NETDEV_STATISTICS",
                           "IEEE80211_BROADCOM_FRAME_TRACE"):
                self.assertNotIn(symbol, config["enable"])
                self.assertNotIn(f"CONFIG_{symbol}=y", config["require"])
            self.assertFalse(any("DUMPPACKET" in symbol for symbol in config["enable"]))
            if platform == "pico2w-radio":
                self.assertIn("DEBUG_WIRELESS_WARN", config["enable"])
                self.assertIn("CONFIG_DEBUG_WIRELESS_WARN=y", config["require"])
                for symbol in ("RAMLOG", "RAMLOG_SYSLOG", "RAMLOG_NONBLOCKING"):
                    self.assertIn(symbol, config["enable"])
                    self.assertIn(f"CONFIG_{symbol}=y", config["require"])
                self.assertIn("CONFIG_RAMLOG_BUFSIZE=4096", config["require"])
                self.assertIn("SYSLOG_CDCACM", config["disable"])

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
