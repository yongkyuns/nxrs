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

    def test_protected_build_matches_encryption_alignment_and_blocks_runtime_updates(self):
        profile = resolve("esp32s3-camera-stream-protected-build")
        app = BOOT.application_config(profile)
        text = BOOT.bootloader_config(profile, "/tmp/public-root.pem")
        self.assertEqual(profile["mcuboot"]["write-align"], 32)
        self.assertEqual(app["ESP32S3_OTA_PARTITION_ENCRYPT"], "y")
        self.assertNotIn("BOOT_MCUBOOT", app)
        for key in ("SECURE_BOOT_V2_ENABLED", "SECURE_FLASH_ENC_ENABLED",
                    "SECURE_FLASH_REQUIRE_ALREADY_ENABLED", "SECURE_FLASH_ENCRYPTION_MODE_RELEASE"):
            self.assertIn(f"CONFIG_{key}=1\n", text)
        for overrides in ({"hardware-protection": "yes"},
                          {"mcuboot": {**profile["mcuboot"], "write-align": 4}},
                          {"hal": {**profile["hal"], "boot": "nuttx"}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                BOOT.application_config({**profile, **overrides})

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
            erase = path.read_text()
            erase = erase[erase.index("int flash_area_erase("):]
            (directory / "erase.inc").write_text(erase[:erase.index("\n}\n") + 3])
            executable = directory / "erase"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                            str(ROOT / "tests/host/mcuboot_native_erase.c"), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True, timeout=5)
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


@unittest.skipUnless(importlib.util.find_spec("espsecure"), "install esptool==4.8.1 for ROM signature tests")
class RomSigningTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("imgtool"), "install tools/mcuboot-requirements.txt")
    def test_factory_ciphertext_initialization_and_refusals(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from imgtool.keys import load
        from mcuboot_image import sign
        import hashlib
        import json
        modules = []
        for name in ("build-mcuboot", "prepare-protected-factory"):
            spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
        builder, factory = modules
        profile = resolve("esp32s3-camera-stream-protected-build")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key, root = directory / "rom.key", directory / "rom.pem"
            material = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            key.write_bytes(material.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
            root.write_bytes(material.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            app_key, app_root = directory / "app.key", directory / "app.pem"
            subprocess.run(["imgtool", "keygen", "-k", str(app_key), "-t", "ecdsa-p256"], check=True)
            app_root.write_bytes(load(str(app_key)).get_public_pem())
            source, boot, image = directory / "source.bin", directory / "bootloader.signed.bin", directory / "factory.bin"
            source.write_bytes(b"test firmware" * 128)
            builder.sign_rom(source, boot, key, root)
            sign(source, image, profile["mcuboot"], app_key, app_root, "1.2.3", 7, confirm=True)
            metadata = {"mcuboot": BOOT.MCUBOOT_REVISION, "esp-hal-3rdparty": BOOT.HAL_REVISION,
                        "platform": profile["name"], "layout": profile["mcuboot"],
                        "bootloader_file": boot.name, "hardware_secure_boot": True, "flash_encryption": True,
                        "rom_root_pem_sha256": hashlib.sha256(root.read_bytes()).hexdigest(),
                        "public_root_sha256": hashlib.sha256(app_root.read_bytes()).hexdigest(),
                        "bootloader_sha256": hashlib.sha256(boot.read_bytes()).hexdigest()}
            (directory / "bootloader.json").write_text(json.dumps(metadata))
            flash_key = directory / "flash.key"
            flash_key.write_bytes(os.urandom(32))
            output = directory / "bundle"
            factory.prepare(profile, directory, image, root, app_root, flash_key, output)
            manifest = json.loads((output / "factory.json").read_text())
            self.assertFalse(manifest["hardware_qualified"])
            self.assertFalse(manifest["runtime_updates"])
            self.assertEqual([region["offset"] for region in manifest["regions"]],
                             [0, 0x20000, 0x220000, 0x420000])
            self.assertEqual({path.name for path in output.iterdir()},
                             {"factory.json", "boot.enc.bin", "primary.enc.bin", "secondary.enc.bin", "scratch.enc.bin"})
            for region in manifest["regions"]:
                encrypted, decrypted = output / region["file"], directory / "decoded.bin"
                subprocess.run([sys.executable, "-m", "espsecure", "decrypt_flash_data", "--aes_xts",
                                "--keyfile", str(flash_key), "--address", hex(region["offset"]),
                                "--output", str(decrypted), str(encrypted)], check=True)
                expected = boot.read_bytes() if region["offset"] == 0 else image.read_bytes() if (
                    region["offset"] == profile["mcuboot"]["primary-offset"]) else b"\xff" * region["size"]
                self.assertEqual(decrypted.read_bytes(), expected)
                self.assertEqual(hashlib.sha256(encrypted.read_bytes()).hexdigest(), region["sha256"])
            for name, changed in (("existing", {}), ("private-root", {"rom_root": key}),
                                  ("private-app-root", {"app_root": app_key}),
                                  ("wrong-profile", {"profile": resolve("esp32s3-camera-stream-mcuboot")})):
                target = output if name == "existing" else directory / name
                arguments = dict(profile=profile, boot_build=directory, application=image, rom_root=root,
                                 app_root=app_root, flash_key=flash_key, output=target)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    factory.prepare(**{**arguments, **changed})
                self.assertEqual(target.exists(), name == "existing")
            good = image.read_bytes()
            data = bytearray(good)
            data[64] ^= 1
            image.write_bytes(data)
            with self.assertRaises(subprocess.CalledProcessError):
                factory.prepare(profile, directory, image, root, app_root, flash_key, directory / "corrupt")
            self.assertFalse((directory / "corrupt").exists())
            data = bytearray(good)
            data[-64] = 0xff  # Remove image_ok without changing the authenticated payload.
            image.write_bytes(data)
            with self.assertRaises(ValueError):
                factory.prepare(profile, directory, image, root, app_root, flash_key, directory / "unconfirmed")
            self.assertFalse((directory / "unconfirmed").exists())
            self.assertFalse(list(directory.glob(".factory-*")))

    def test_independent_root_corruption_size_and_atomic_publication(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        spec = importlib.util.spec_from_file_location("boot_builder", ROOT / "tools/build-mcuboot.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            keys = [rsa.generate_private_key(public_exponent=65537, key_size=3072) for _ in range(2)]
            key, root, wrong = (directory / name for name in ("key.pem", "root.pem", "wrong.pem"))
            private = keys[0].private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption())
            key.write_bytes(private)
            for path, material in ((root, keys[0]), (wrong, keys[1])):
                path.write_bytes(material.public_key().public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            source, output = directory / "unsigned.bin", directory / "signed.bin"
            source.write_bytes(b"payload" * 512)
            builder.sign_rom(source, output, key, root)
            good = output.read_bytes()
            for expected, size in ((wrong, 4096), (root, 0xf000)):
                source.write_bytes(b"x" * size)
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    builder.sign_rom(source, output, key, expected)
                self.assertEqual(output.read_bytes(), good)
            for protected in (source, key, root):
                with self.assertRaises(ValueError):
                    builder.sign_rom(source, protected, key, root)
            with self.assertRaises(ValueError):
                builder.sign_rom(source, output, key, key)
            corrupt = bytearray(good)
            corrupt[64] ^= 1
            output.write_bytes(corrupt)
            result = subprocess.run([sys.executable, "-m", "espsecure", "verify_signature", "--version", "2",
                                     "--keyfile", str(root), str(output)], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(key.read_bytes(), private)
            self.assertFalse(list(directory.glob(".rom-sign-*")))


if __name__ == "__main__":
    unittest.main()
