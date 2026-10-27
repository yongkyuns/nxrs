"""Public-policy key domains and actual compiler dependency-input boundaries."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import boot_rust
import build_esp32s3
import target_plan


class TrustGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=3072).public_key()
        cls.plan = target_plan.plan("esp32s3-camera-mcuboot")

    def field(self, source, name):
        line = next(line for line in source.decode().splitlines() if line.startswith(name + ":"))
        return bytes(json.loads(line.split(":", 1)[1].rstrip(",")))

    def test_actual_root_uses_pkcs1_and_not_operator_spki(self):
        source = boot_rust.trust_source(self.plan, "a" * 64, self.key)
        pkcs1 = self.key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
        spki = self.key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertEqual(self.field(source, "key_sha256"), hashlib.sha256(pkcs1).digest())
        self.assertNotEqual(self.field(source, "key_sha256"), hashlib.sha256(spki).digest())

    def test_policy_configuration_and_independent_canonical_layout(self):
        source = boot_rust.trust_source(self.plan, "a" * 64, self.key)
        self.assertEqual(self.field(source, "policy_sha256"), bytes.fromhex("a" * 64))
        self.assertEqual(self.field(source, "configuration_sha256"), bytes.fromhex(self.plan["settings"]["configuration_sha256"]))
        raw = json.dumps(self.plan["settings"]["layout"], sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(self.field(source, "layout_sha256"), hashlib.sha256(raw).digest())
        changed = boot_rust.trust_source(self.plan, "b" * 64, self.key)
        self.assertNotEqual(self.field(changed, "policy_sha256"), self.field(source, "policy_sha256"))

    def test_invalid_pin_is_refused_before_generation(self):
        for pin in ("z" * 64, "a" * 63, "a" * 65):
            with self.assertRaises(ValueError):
                boot_rust.trust_source(self.plan, pin, self.key)


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.a = self.root / "a.rs"
        self.b = self.root / "b.rs"
        self.a.write_text("a")
        self.b.write_text("b")
        self.dep = self.root / "actual.d"

    def test_actual_rust_format_multiple_artifacts(self):
        self.dep.write_text("libcore.rlib libcore.rmeta: " + str(self.a) + " " + str(self.b) + "\n\n" + str(self.a) + ":\n")
        self.assertEqual(boot_rust.dependency_paths(self.dep), [self.a, self.b])

    def test_manifest_relative_inputs_do_not_use_callers_directory(self):
        self.dep.write_text("target: a.rs b.rs\n")
        self.assertEqual(boot_rust.dependency_paths(self.dep, base=self.root), [self.a, self.b])

    def test_duplicate_and_canonical_alias_are_refused(self):
        (self.root / "child").mkdir()
        for name in (str(self.a), str(self.root / "child/../a.rs")):
            self.dep.write_text("target: " + str(self.a) + " " + name + "\n")
            with self.assertRaises(ValueError):
                boot_rust.dependency_paths(self.dep)

    def test_ambiguous_escaped_path_is_refused(self):
        self.dep.write_text("target: /unsafe\\ path.rs\n")
        with self.assertRaises(ValueError):
            boot_rust.dependency_paths(self.dep)

    def test_empty_dependency_set_is_refused(self):
        self.dep.write_text("target: \n")
        with self.assertRaises(ValueError):
            boot_rust.dependency_paths(self.dep)


class HookOrigin(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.archive = self.root / "boot-policy/libnxrs_boot_policy.a"
        self.archive.parent.mkdir()
        self.archive.write_bytes(b"origin-fixture")

    def record(self, owner, address="0x403b46bc", size="0xfad"):
        return " .text.boot_image_check_hook\n                " + address + " " + size + " " + owner + "(nxrs_boot_policy.o)\n"

    def test_absolute_relative_and_canonical_archive_identity(self):
        for name, kind in ((str(self.archive), "absolute"), ("boot-policy/libnxrs_boot_policy.a", "relative"),
                           ("boot-policy/../boot-policy/libnxrs_boot_policy.a", "relative")):
            self.assertEqual(build_esp32s3.verify_hook_origin(self.record(name), self.archive), (0x403b46bc, 0xfad, kind))

    def test_fallback_substitution_and_duplicate_retained_section_are_refused(self):
        record = self.record(str(self.archive))
        other = self.root / "other/libnxrs_boot_policy.a"
        other.parent.mkdir()
        other.write_bytes(b"unapproved")
        for changed in ("", record + record, self.record("libhal.a"), self.record(str(other))):
            with self.assertRaises(ValueError):
                build_esp32s3.verify_hook_origin(changed, self.archive)

    def test_discarded_section_cannot_supply_retained_ownership(self):
        dead = self.record(str(self.archive), address="0x0")
        with self.assertRaises(ValueError):
            build_esp32s3.verify_hook_origin(dead, self.archive)
        live = self.record(str(self.archive))
        self.assertEqual(build_esp32s3.verify_hook_origin(dead + live, self.archive)[:2], (0x403b46bc, 0xfad))

    def test_malformed_member_and_wrong_relative_identity_are_refused(self):
        for record in (self.record(str(self.archive)).replace("nxrs_boot_policy.o)", "../stub.o)"),
                       self.record("../libnxrs_boot_policy.a")):
            with self.assertRaises(ValueError):
                build_esp32s3.verify_hook_origin(record, self.archive)


if __name__ == "__main__":
    unittest.main()
