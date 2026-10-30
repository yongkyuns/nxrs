"""Product wiring stays consistent with the signed image and upstream pins."""
import importlib.util
import os
import re
from pathlib import Path
import sys
import subprocess
import tempfile
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
        self.assertEqual(app["BOOT_MCUBOOT"], "y")
        self.assertEqual(app["ESP32S3_SPIFLASH_AUTO_BRINGUP"], "n")
        self.assertEqual(app["MCUBOOT_VERSION"], app["ESP32S3_MCUBOOT_VERSION"])
        self.assertEqual(int(app["ESP32S3_STORAGE_MTD_OFFSET"], 0), 0x460000)
        self.assertEqual(config["CONFIG_ESP_SIGN_EC256"], "1")
        self.assertEqual(config["CONFIG_BOOTLOADER_WDT_ENABLE"], "1")
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

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE"), "set pinned SDK source for native FTL regression")
    def test_trailer_writes_never_erase_the_sector(self):
        sdk = Path(os.environ["NXRS_MCUBOOT_SOURCE"])
        relative = "boot/nuttx/src/flash_map_backend/flash_map_backend.c"
        original = (sdk / relative).read_text()
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"],
                                           cwd=ROOT, text=True).strip()
        ftl = subprocess.check_output(
            ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:drivers/mtd/ftl.c"], text=True)
        function = ftl[ftl.rindex("static ssize_t ftl_flush_direct("):]
        function = function[:function.index("\n}\n") + 3]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_text(original)
            subprocess.run(["patch", "-p1", "-i", str(ROOT / "upstream/mcuboot/patches/0001-nuttx-direct-trailer-writes.patch")],
                           cwd=directory, check=True, capture_output=True)
            (directory / "ftl.inc").write_text(function)
            for source, expected in ((original, 1), (path.read_text(), 0)):
                flags = re.search(r"fd = open\(dev->fa_cfg->fa_mtd_path, (.*?)\);", source).group(1)
                (directory / "open_flags.inc").write_text(f"#define SDK_OPEN_FLAGS ({flags})\n")
                executable = directory / "trailer"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                str(ROOT / "tests/host/mcuboot_trailer_nor.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, expected)

    def test_actual_nuttx_handoff_preserves_bits_and_unlocks_then_relocks(self):
        relative = "arch/xtensa/src/esp32s3/esp32s3_wdt.c"
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"],
                                           cwd=ROOT, text=True).strip()
        original = subprocess.check_output(
            ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{relative}"], text=True)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_text(original)
            subprocess.run(["patch", "-p1", "-i", str(ROOT / "upstream/nuttx/patches/0094-esp32s3-boot-watchdog-handoff.patch")],
                           cwd=directory, check=True, capture_output=True)
            for source, expected in ((original, 1), (path.read_text(), 0)):
                function = source[source.index("void esp32s3_wdt_early_deinit(void)"):]
                function = function[:function.index("\n}") + 2]
                (directory / "early.c").write_text(function)
                executable = directory / "handoff"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                str(ROOT / "tests/host/esp32s3_wdt_handoff.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, expected)


if __name__ == "__main__":
    unittest.main()
