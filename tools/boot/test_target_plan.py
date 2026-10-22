"""Actual public-policy, payload, frontend and offline SDK-command controls."""
import copy
import contextlib
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from intelhex import IntelHex
from imgtool.image import Image
import sign
import target_plan as target
import sdk_cli
import build_esp32s3 as builder
import qualify_target

ROOT = target.ROOT
PROFILE = "esp32s3-camera-mcuboot"


class TargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="nxrs-target-controls-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.data = target.plan(PROFILE)
        cls.authority = cls.root / "authority"
        cls.authority.mkdir(mode=0o700)
        cls.pin = sign.initialize(cls.authority, cls.data["settings"], os.urandom(48).hex().encode("ascii"))
        cls.policy = cls.authority / "policy.json"
        cls.public = cls.root / "application.pem"
        cls.public_hash = sign.export_public(cls.policy.read_bytes(), cls.pin, PROFILE,
            cls.data["settings"]["configuration_sha256"], cls.public)
        key = serialization.load_pem_public_key(cls.public.read_bytes())
        cls.der_hash = hashlib.sha256(key.public_bytes(serialization.Encoding.DER,
            serialization.PublicFormat.PKCS1)).hexdigest()

    def test_qualification_failure_location_does_not_disclose_exception_data(self):
        output = io.StringIO()
        private = "private-authority-path-and-secret-must-not-leave-stage"
        with patch.object(sys, "argv", ["qualify_target.py", "--work", "unused"]), \
                patch.object(qualify_target, "qualify", side_effect=ValueError(private)), \
                contextlib.redirect_stderr(output):
            self.assertEqual(qualify_target.main(), 1)
        text = output.getvalue()
        self.assertRegex(text, r"^Target qualification refused at qualify_target[.]py:[0-9]+\n")
        self.assertIn("private stage logs retained", text)
        self.assertNotIn(private, text)
        self.assertNotIn(str(ROOT), text)

    def setUp(self):
        self.test_directory = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.test_directory.cleanup)
        self.work = Path(self.test_directory.name)
        self.config = self.work / "resolved.config"
        self.config.write_text("\n".join(self.data["configuration"]["kconfig"]["require"]) + "\n")
        self.elf = self.work / "nuttx"
        self.elf.write_bytes(b"\x7fELF-public-build-fixture")
        self.hex = self.work / "nuttx.hex"
        self.make_hex()
        self.output = self.work / "output"
        self.output.mkdir(mode=0o700)

    def make_hex(self, values=None):
        image = IntelHex()
        image.fromdict({0x42000020: 0xE9, 0x42000021: 0x03, 0x42000025: 0x7E} if values is None else values)
        image.write_hex_file(str(self.hex))

    def record(self):
        return target.record_payload(PROFILE, self.policy, self.pin, self.config,
                                     self.elf, self.hex, self.output)

    def test_verified_public_policy_and_compiled_sector_budget(self):
        data, raw, public = target.checked_policy(PROFILE, self.policy, self.pin)
        self.assertEqual(raw, self.policy.read_bytes())
        self.assertEqual(public.key_size, 3072)
        self.assertEqual(data["settings"]["layout"]["max_sectors"], 512)
        for name in ("rom_authenticated", "protected_device_counter", "physical_qualified", "deployment_permitted"):
            self.assertIs(data[name], False)

    def test_policy_pin_corruption_and_other_configuration_are_refused(self):
        with self.assertRaises(ValueError):
            target.checked_policy(PROFILE, self.policy, "0" * 64)
        changed = json.loads(self.policy.read_bytes())
        changed["settings"]["layout"]["max_sectors"] = 128
        path = self.work / "tampered.json"
        path.write_bytes(target.canonical(changed) + b"\n")
        with self.assertRaises(ValueError):
            target.checked_policy(PROFILE, path, sign.sha(path.read_bytes()))
        other = self.work / "other-authority"
        other.mkdir(mode=0o700)
        settings = copy.deepcopy(self.data["settings"])
        settings["configuration_sha256"] = "0" * 64
        pin = sign.initialize(other, settings, os.urandom(48).hex().encode("ascii"))
        with self.assertRaises(ValueError):
            target.checked_policy(PROFILE, other / "policy.json", pin)

    def test_development_and_inconsistent_native_profiles_are_refused(self):
        with self.assertRaises(ValueError):
            target.plan("esp32s3-camera")
        original = target.select(PROFILE)
        for update in (lambda p: p["boot"]["layout"].update(max_sectors=128),
                       lambda p: p["boot"].update(scope="production"),
                       lambda p: p["kconfig"]["set"].append("ESP32S3_OTA_SLOT_SIZE=0x200000"),
                       lambda p: p["kconfig"]["enable"].append("RAW_BINARY")):
            changed = copy.deepcopy(original)
            update(changed)
            with patch.object(target.platform_config, "resolve", return_value=changed):
                with self.assertRaises(ValueError):
                    target.select(PROFILE)

    def test_sdk_configuration_omits_every_fuse_and_flash_enabling_flag(self):
        config = target.bootloader_config(self.data, self.public)
        self.assertIn("CONFIG_ESP_SIGN_RSA_LEN=3072\n", config)
        self.assertIn("CONFIG_ESP_DOWNGRADE_PREVENTION_SECURITY_COUNTER=1\n", config)
        for forbidden in ("CONFIG_SECURE_BOOT", "CONFIG_SECURE_FLASH_ENC_ENABLED", "CONFIG_EFUSE_VIRTUAL"):
            self.assertNotIn(forbidden, config)

    def test_resolved_native_geometry_debug_and_packaging_mutations_are_refused(self):
        clean = self.config.read_text()
        target.verify_config(self.data, clean)
        for altered in (clean.replace("CONFIG_ESP32S3_OTA_SLOT_SIZE=0x100000", "CONFIG_ESP32S3_OTA_SLOT_SIZE=0x200000"),
                        clean + "CONFIG_RAW_BINARY=y\n", clean + "CONFIG_ESPRESSIF_SIMPLE_BOOT=y\n",
                        clean + "CONFIG_ESPRESSIF_SECURE_FLASH_ENC_ENABLED=y\n",
                        clean + "CONFIG_INTELHEX_BINARY=y\n",
                        clean.replace("CONFIG_ESP32S3_APP_FORMAT_MCUBOOT=y\n", "")):
            with self.subTest(altered=hashlib.sha256(altered.encode()).hexdigest()):
                with self.assertRaises(ValueError):
                    target.verify_config(self.data, altered)

    def test_payload_matches_actual_vendor_intelhex_interpretation(self):
        result = self.record()
        # Independent actual SDK loader, including address gaps and header stripping.
        vendor = Image(header_size=32, pad_header=True)
        vendor.load(str(self.hex))
        body = (self.output / "payload.bin").read_bytes()
        self.assertEqual(body, bytes(vendor.payload[32:]))
        self.assertEqual(body, b"\xe9\x03\xff\xff\xff\x7e")
        self.assertEqual(result["payload_sha256"], sign.sha(body))
        self.assertEqual(result["intelhex_base_address"], 0x42000020)
        self.assertEqual((self.output / "boot-policy.json").read_bytes(), self.policy.read_bytes())
        self.assertIs(result["deployment_permitted"], False)
        with self.assertRaises(FileExistsError):
            self.record()
        self.assertEqual((self.output / "payload.bin").read_bytes(), body)

    def test_empty_oversized_already_wrapped_and_overlapping_payloads_are_refused(self):
        for values in ({}, {0: 1, 0x100000: 2}, {0: 0x7F, 1: 0x45, 2: 0x4C, 3: 0x46},
                       {0: 0x3D, 1: 0xB8, 2: 0xF3, 3: 0x96}):
            self.make_hex(values)
            with self.assertRaises((ValueError, TypeError)):
                self.record()
            self.assertEqual(list(self.output.iterdir()), [])
        self.make_hex()
        lines = self.hex.read_text().splitlines()
        self.hex.write_text("\n".join([*lines[:-1], lines[-2], lines[-1]]) + "\n")
        with self.assertRaises(Exception):
            self.record()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_plan_output_never_replaces_existing_or_symlinked_output(self):
        path = self.work / "plan"
        target.emit(PROFILE, path)
        original = (path / "boot-plan.json").read_bytes()
        with self.assertRaises(FileExistsError):
            target.emit(PROFILE, path)
        self.assertEqual((path / "boot-plan.json").read_bytes(), original)
        alias = self.work / "alias"
        alias.symlink_to(path, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            target.emit(PROFILE, alias)

    def sdk(self, kind, arguments, extra=""):
        script = "import sys; sys.path.insert(0,sys.argv[1]); import sdk_cli;\n"
        script += "try:\n sdk_cli.main(*__import__('json').loads(sys.argv[2]))\nexcept SystemExit as e:\n assert e.code in (0,None)\n"
        script += extra
        params = [kind, str(self.public), self.der_hash, str(self.work / "image.elf"),
                  str(self.work / "image.bin"), arguments]
        return subprocess.run([sys.executable, "-c", script, str(ROOT / "tools/boot"), json.dumps(params)],
                              capture_output=True, timeout=10, cwd=self.work)

    def test_actual_sdk_getpub_and_network_serial_denial(self):
        extra = '''import socket,serial
for operation in (lambda: socket.getaddrinfo("localhost",1),lambda: socket.socket().connect(("127.0.0.1",1)),lambda: socket.socket().bind(("127.0.0.1",0)),lambda: serial.Serial("/dev/does-not-exist"),lambda: serial.serial_for_url("loop://")):
 try: operation()
 except RuntimeError: pass
 else: raise AssertionError("live operation escaped")
print("LIVE_OPERATIONS_REFUSED")
'''
        result = self.sdk("imgtool", ["getpub", "-k", str(self.public)], extra)
        self.assertEqual(result.returncode, 0, "actual SDK public-only command failed")
        self.assertIn(b"rsa_pub_key", result.stdout)
        self.assertIn(b"LIVE_OPERATIONS_REFUSED", result.stdout)
        self.assertNotIn(b"PRIVATE KEY", result.stdout)

    def test_sdk_flash_fuse_alternate_output_and_duplicate_options_are_refused(self):
        valid = ["--chip", "esp32s3", "elf2image", "--min-rev", "0", "--flash_mode", "dio",
                 "--flash_freq", "40m", "--flash_size", "16MB", "-o", str(self.work / "image.bin"),
                 str(self.work / "image.elf")]
        (self.work / "image.elf").write_bytes(b"public-invalid-elf")
        for kind, args in (("imgtool", ["sign", "-k", str(self.public)]),
                           ("esptool", ["write_flash", "0", "image.bin"]),
                           ("esptool", ["burn_efuse", "SECURE_BOOT_EN"]),
                           ("esptool", [*valid[:-1], "--port", "/dev/does-not-exist", valid[-1]]),
                           ("esptool", [*valid[:-1], "--flash_mode", "dio", valid[-1]]),
                           ("esptool", [*valid[:-2], "foreign.bin", valid[-1]])):
            with self.subTest(kind=kind, operation=args[0]):
                result = self.sdk(kind, args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"offline SDK command refused", result.stderr)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["image.elf", "nuttx", "nuttx.hex", "output", "resolved.config"])

    def test_sdk_output_symlinks_and_hardlinks_are_refused_before_conversion(self):
        (self.work / "image.elf").write_bytes(b"invalid-elf-must-not-be-opened")
        other = self.work / "other.bin"
        other.write_bytes(b"preserved")
        out = self.work / "image.bin"
        args = ["--chip", "esp32s3", "elf2image", "--min-rev", "0", "--flash_mode", "dio",
                "--flash_freq", "40m", "--flash_size", "16MB", "-o", "image.bin", "image.elf"]
        for link in (lambda: out.symlink_to(other), lambda: os.link(other, out)):
            link()
            result = self.sdk("esptool", args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"offline SDK command refused", result.stderr)
            self.assertEqual(other.read_bytes(), b"preserved")
            out.unlink()

    def test_frontend_rejects_missing_or_wrong_policy_before_creating_output(self):
        environment = dict(os.environ, NXRS_BOOT_PYTHON=sys.executable,
                           PATH=str(Path(sys.executable).parent) + ":" + os.environ["PATH"])
        for suffix in ([], ["--boot-policy", str(self.policy), "--boot-policy-sha256", "0" * 64]):
            out = self.work / "no-build"
            result = subprocess.run(["cargo", "+1.90.0", "firmware", "--app", "std-demo", "--platform", PROFILE,
                "--out", str(out), *suffix], cwd=ROOT, env=environment, capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(out.exists())

    def test_frontend_plan_and_mode_conflicts(self):
        environment = dict(os.environ, NXRS_BOOT_PYTHON=sys.executable)
        out = self.work / "frontend-plan"
        args = ["cargo", "+1.90.0", "firmware", "--platform", PROFILE]
        good = subprocess.run([*args, "--emit-boot-plan", "--out", str(out)], cwd=ROOT,
                              env=environment, capture_output=True, timeout=60)
        self.assertEqual(good.returncode, 0, "actual frontend plan failed")
        self.assertEqual(json.loads((out / "signer-settings.json").read_bytes()), self.data["settings"])
        for suffix in (["--emit-boot-plan", "--bootloader"], ["--bootloader"],
                       ["--emit-boot-plan", "--app", "std-demo"],
                       ["--boot-policy", str(self.policy)]):
            result = subprocess.run([*args, *suffix], cwd=ROOT, env=environment,
                                    capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
