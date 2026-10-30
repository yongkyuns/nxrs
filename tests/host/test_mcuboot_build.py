"""Product wiring stays consistent with the signed image and upstream pins."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from platform_config import resolve

SPEC = importlib.util.spec_from_file_location("nuttx_mcuboot", ROOT / "platform/nuttx/mcuboot.py")
BOOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOT)


class BootWiringTests(unittest.TestCase):
    def test_shared_geometry_public_root_and_reversible_configuration(self):
        profile = resolve("esp32s3-camera-stream-mcuboot")
        app = BOOT.application_config(profile)
        text = BOOT.bootloader_config(profile, "/private/tmp/test-root.pem")
        config = dict(line.split("=", 1) for line in text.splitlines())
        for application, boot in (("ESP32S3_OTA_PRIMARY_SLOT_OFFSET", "CONFIG_ESP_IMAGE0_PRIMARY_START_ADDRESS"),
                                  ("ESP32S3_OTA_SECONDARY_SLOT_OFFSET", "CONFIG_ESP_IMAGE0_SECONDARY_START_ADDRESS"),
                                  ("ESP32S3_OTA_SLOT_SIZE", "CONFIG_ESP_APPLICATION_SIZE"),
                                  ("ESP32S3_OTA_SCRATCH_OFFSET", "CONFIG_ESP_SCRATCH_OFFSET")):
            self.assertEqual(app[application], config[boot])
        self.assertEqual(config["CONFIG_ESP_SIGN_EC256"], "1")
        self.assertFalse(any(key.startswith("CONFIG_SECURE_") for key in config))
        self.assertIn(BOOT.MCUBOOT_REVISION, (ROOT / "tools/mcuboot-requirements.txt").read_text())

    def test_unsupported_target_geometry_and_config_injection_fail(self):
        profile = resolve("esp32s3-camera-stream-mcuboot")
        for field, value in (("slot-size", 0x300000), ("primary-offset", 0x10000),
                             ("write-align", 32)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                BOOT.application_config({**profile, "mcuboot": {**profile["mcuboot"], field: value}})
        with self.assertRaises(ValueError):
            BOOT.application_config({**profile, "target": "thumbv8m.main-nuttx-eabi"})
        for path in ("relative.pem", "/tmp/root\nCONFIG_SECURE_BOOT=1", "/tmp/root;injected"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                BOOT.bootloader_config(profile, path)


if __name__ == "__main__":
    unittest.main()
