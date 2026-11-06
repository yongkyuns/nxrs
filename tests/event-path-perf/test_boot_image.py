"""Paired boot-profile evidence controls; images and loader here are synthetic."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load("event_path_boot_capture_fixtures", HERE / "test_capture.py")
capture = fixtures.capture
boot = capture.builder.boot_image
shapes = load("event_path_boot_image_shapes", ROOT / "tests/std-channel-perf/test_esp_image.py")


class BootImageControls(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CaptureControls(
            "test_successful_host_capture_is_functional_only_and_output_is_never_reused")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.out = self.fixture.build
        self.firmware = self.out / "firmware"
        (self.firmware / "nuttx").mkdir(parents=True)
        self.original = self.firmware / "nuttx/nuttx.merged.bin"
        self.data, _ = shapes.image()
        self.original.write_bytes(self.data)
        (self.firmware / "nuttx/nuttx").write_bytes(b"synthetic ELF, never executed")
        (self.firmware / "resolved.config").write_text(
            "CONFIG_BUILD_FLAT=y\n# CONFIG_SMP is not set\n"
            "CONFIG_ARCH_CHIP_ESP32S3=y\nCONFIG_ESPRESSIF_SIMPLE_BOOT=y\n")
        self.loader = self.firmware / "nuttx" / boot.helper.LOADER_PATH
        self.loader.parent.mkdir(parents=True)
        self.loader.write_bytes(
            b"/* SYNTHETIC test loader; not executable firmware */\n"
            b"if (image_header.hash_appended == 1) { offset += 32; }\n")
        # Freeze one synthetic loader hash, as P0's own provenance tests do.
        # Mutated evidence below never changes this expected hash. Native CI
        # separately exercises the actual pinned original and patched C parser.
        pinned = patch.object(boot.helper, "LOADER_AFTER", capture.digest(self.loader))
        pinned.start()
        self.addCleanup(pinned.stop)
        ledger = {
            "component": "nuttx", "upstream_revision": boot.helper.KERNEL_REVISION,
            "patches": [{"name": boot.helper.PATCH_NAME,
                         "sha256": capture.digest(boot.helper.PATCH),
                         "files": {boot.helper.LOADER_PATH: {
                             "before": boot.helper.LOADER_BEFORE,
                             "after": boot.helper.LOADER_AFTER}}}],
        }
        (self.firmware / "nuttx-patches.json").write_text(json.dumps(ledger))
        self.candidate, fields, extra = boot.prepare(self.firmware, self.original, self.out)
        self.record = copy.deepcopy(self.fixture.receipt)
        self.record.update(fields)
        self.record.update(
            boot_image_profile=boot.PROFILE, platform="esp32s3-service-footprint",
            target="xtensa-esp32s3-nuttx", sdk="esp-1.90.0.0",
            kernel_revision=boot.helper.KERNEL_REVISION,
            image_file=self.candidate.relative_to(self.out).as_posix(),
            elf_file="firmware/nuttx/nuttx", config_file="firmware/resolved.config",
        )
        self.source_names = (
            "tests/std-channel-perf/simpleboot_digest.py",
            "tests/std-channel-perf/inspect_esp_image.py",
            "tools/apply-nuttx-patches.py",
            "upstream/nuttx/patches/" + boot.helper.PATCH_NAME,
        )
        self.record["source_sha256"].update({name: capture.digest(ROOT / name) for name in self.source_names})
        for name in ("image", "elf", "config"):
            del self.record["artifacts_sha256"][name]
        for path in [self.candidate, self.firmware / "nuttx/nuttx",
                     self.firmware / "resolved.config", *extra]:
            self.record["artifacts_sha256"][path.relative_to(self.out).as_posix()] = capture.digest(path)
        self.rehash()

    def rehash(self):
        """Keep every outer hash/identity consistent so negatives reach the semantic gate."""
        for name in self.record["artifacts_sha256"]:
            self.record["artifacts_sha256"][name] = capture.digest(self.out / name)
        for name in ("image", "elf", "config"):
            self.record[name + "_sha256"] = capture.digest(self.out / self.record[name + "_file"])
        self.record["build_id"] = capture.builder.build_identity(self.record)
        (self.out / "build.json").write_text(json.dumps(self.record, indent=2) + "\n")

    def read(self, field):
        return json.loads((self.out / self.record[field]).read_text())

    def write(self, field, value):
        (self.out / self.record[field]).write_text(json.dumps(value))

    def refused(self, message=None):
        self.rehash()
        self.assertEqual(capture.builder.build_identity(self.record), self.record["build_id"])
        if message:
            with self.assertRaisesRegex(ValueError, message):
                capture.build_receipt(self.out)
        else:
            with self.assertRaises(ValueError):
                capture.build_receipt(self.out)

    def test_profile_imports_are_scoped_and_conflicting_cached_modules_refuse(self):
        before = list(sys.path)
        self.assertIs(boot._load_helper(), boot.helper)
        self.assertEqual(sys.path, before)
        for name in ("inspect_esp_image", "simpleboot_digest"):
            foreign = ModuleType(name)
            foreign.__file__ = str(self.fixture.root / (name + ".py"))
            with self.subTest(name=name), patch.dict(sys.modules, {name: foreign}):
                with self.assertRaisesRegex(ImportError, "own"):
                    boot._load_helper()
                self.assertEqual(sys.path, before)
        with patch.object(boot.importlib, "import_module", side_effect=RuntimeError("synthetic import failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                boot._load_helper()
        self.assertEqual(sys.path, before)

    def test_only_explicit_esp_candidate_profile_is_supported(self):
        boot.validate_profile(boot.DEFAULT, "mps2-an521-mock")
        boot.validate_profile(boot.PROFILE, "esp32s3-service-footprint")
        for profile, platform in (("unknown", "esp32s3-service-footprint"),
                                  (None, "esp32s3-service-footprint"),
                                  (boot.PROFILE, "mps2-an521-mock"),
                                  (boot.PROFILE, "esp32s3-qemu-mock")):
            with self.subTest(profile=profile, platform=platform), self.assertRaises(ValueError):
                boot.validate_profile(profile, platform)

    def test_preparation_preserves_original_and_retains_every_loader_proof(self):
        self.assertEqual(capture.build_receipt(self.out), self.record)
        self.assertEqual(self.original.read_bytes(), self.data)
        self.assertEqual(len(self.candidate.read_bytes()), len(self.data))
        self.assertNotEqual(capture.digest(self.original), capture.digest(self.candidate))
        for field in ("unmodified_source_image", "simpleboot_digest_report", "simpleboot_patch_receipt",
                      "simpleboot_loader_source", "simpleboot_loader_patch"):
            self.assertIn(self.record[field], self.record["artifacts_sha256"])
        packing = self.read("simpleboot_digest_report")
        self.assertEqual(packing["qualification"], "build-candidate-only")
        self.assertIs(packing["boot_warning_resolved"], False)
        self.assertIs(packing["secure_boot_qualified"], False)
        self.assertIs(packing["physical_timing_qualified"], False)
        before = {path: path.read_bytes() for path in (self.original, self.candidate)}
        with self.assertRaisesRegex(ValueError, "already exists"):
            boot.prepare(self.firmware, self.original, self.out)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_legacy_and_default_profiles_remain_readable(self):
        boot.verify(self.out, self.fixture.receipt)
        legacy = copy.deepcopy(self.fixture.receipt)
        legacy.update(platform="esp32s3-service-footprint", image_file=self.original.relative_to(self.out).as_posix())
        boot.verify(self.out, legacy)
        legacy["boot_image_profile"] = boot.DEFAULT
        boot.verify(self.out, legacy)
        # Existing synthetic receipts do not represent an ESP image header.
        legacy["image_file"] = "image"
        boot.verify(self.out, legacy)

    def test_default_profile_refuses_candidate_fields_and_reserved_output(self):
        self.record["boot_image_profile"] = boot.DEFAULT
        self.refused("explicit boot image profile")
        for field in ("unmodified_source_image", "simpleboot_digest_report", "simpleboot_patch_receipt",
                      "simpleboot_loader_source", "simpleboot_loader_patch"):
            del self.record[field]
        self.refused("candidate output")

    def test_rehashed_renamed_candidate_cannot_be_downgraded_to_default_or_legacy(self):
        candidate_bytes = self.candidate.read_bytes()
        for field in ("unmodified_source_image", "simpleboot_digest_report", "simpleboot_patch_receipt",
                      "simpleboot_loader_source", "simpleboot_loader_patch"):
            del self.record[field]
        del self.record["artifacts_sha256"][self.record["image_file"]]
        self.record["image_file"] = self.original.relative_to(self.out).as_posix()
        self.original.write_bytes(candidate_bytes)
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                if explicit:
                    self.record["boot_image_profile"] = boot.DEFAULT
                else:
                    del self.record["boot_image_profile"]
                self.refused("digest-bearing ESP image")

    def test_rehashed_packing_claims_and_wrong_boolean_types_refuse(self):
        original = self.read("simpleboot_digest_report")
        for field, value in (("boot_warning_resolved", True), ("secure_boot_qualified", 0),
                             ("prefix_digest_verified", 1), ("physical_timing_qualified", float("nan")),
                             ("digest_offset", original["digest_offset"] + 32),
                             ("qualification", "physical-qualified")):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(original)
                changed[field] = value
                self.write("simpleboot_digest_report", changed)
                self.refused()

    def test_valid_prefix_digest_does_not_hide_changed_mapped_payload(self):
        changed = bytearray(self.candidate.read_bytes())
        changed[-1] ^= 1
        self.candidate.write_bytes(changed)
        self.assertIs(boot.helper.parse_image(changed)["ram_prefix_digest_verified"], True)
        packing = self.read("simpleboot_digest_report")
        packing["output_sha256"] = capture.digest(self.candidate)
        self.write("simpleboot_digest_report", packing)
        self.refused("exact repacking")

    def test_rehashed_loader_source_and_matching_ledgers_do_not_replace_the_pin(self):
        self.loader.write_bytes(self.loader.read_bytes() + b"/* changed loader */\n")
        altered = capture.digest(self.loader)
        ledger = self.read("simpleboot_patch_receipt")
        ledger["patches"][0]["files"][boot.helper.LOADER_PATH]["after"] = altered
        self.write("simpleboot_patch_receipt", ledger)
        packing = self.read("simpleboot_digest_report")
        packing["loader"]["loader_source_sha256"] = altered
        self.write("simpleboot_digest_report", packing)
        self.refused("source/ledger mismatch")

    def test_rehashed_patch_source_and_matching_ledgers_do_not_replace_the_patch(self):
        path = self.out / self.record["simpleboot_loader_patch"]
        path.write_bytes(path.read_bytes() + b"\nUnreviewed patch change\n")
        altered = capture.digest(path)
        ledger = self.read("simpleboot_patch_receipt")
        ledger["patches"][0]["sha256"] = altered
        self.write("simpleboot_patch_receipt", ledger)
        packing = self.read("simpleboot_digest_report")
        packing["loader"]["patch_sha256"] = altered
        self.write("simpleboot_digest_report", packing)
        self.record["source_sha256"]["upstream/nuttx/patches/" + boot.helper.PATCH_NAME] = altered
        self.refused("source inventory mismatch")

    def test_source_helper_inspector_and_patch_driver_hashes_are_bound(self):
        original = dict(self.record["source_sha256"])
        for name in self.source_names:
            with self.subTest(source=name):
                self.record["source_sha256"] = {**original, name: "0" * 64}
                self.refused("source inventory mismatch")

    def test_rehashed_kernel_pin_configuration_and_sdk_mismatches_refuse(self):
        original_record = copy.deepcopy(self.record)
        original_config = (self.firmware / "resolved.config").read_bytes()
        for field, value in (("kernel_revision", "0" * 40), ("target", "xtensa-other-nuttx"),
                             ("sdk", "esp-other")):
            with self.subTest(field=field):
                self.record = {**copy.deepcopy(original_record), field: value}
                self.refused("kernel pin|target/SDK")
        self.record = original_record
        for data in (original_config.replace(b"CONFIG_ESPRESSIF_SIMPLE_BOOT=y", b"# CONFIG_ESPRESSIF_SIMPLE_BOOT is not set"),
                     original_config + b"CONFIG_ARCH_CHIP_ESP32S3=y\n"):
            (self.firmware / "resolved.config").write_bytes(data)
            self.refused("exactly one enabled")

    def test_missing_proofs_duplicate_patch_and_duplicate_json_keys_refuse(self):
        original_record = copy.deepcopy(self.record)
        for field in ("simpleboot_loader_source", "simpleboot_loader_patch", "simpleboot_digest_report"):
            with self.subTest(missing=field):
                self.record = copy.deepcopy(original_record)
                del self.record["artifacts_sha256"][self.record[field]]
                self.refused("absent from artifact inventory")
        self.record = original_record
        ledger = self.read("simpleboot_patch_receipt")
        ledger["patches"] *= 2
        self.write("simpleboot_patch_receipt", ledger)
        self.refused("must be built first")
        ledger["patches"] = ledger["patches"][:1]
        path = self.out / self.record["simpleboot_patch_receipt"]
        path.write_text(json.dumps(ledger)[:-1] + ', "component": "nuttx"}')
        self.refused("duplicate boot evidence JSON key")

    def test_escaping_or_oversized_loader_source_refuses(self):
        original = self.loader.read_bytes()
        outside = self.fixture.root / "outside-loader.c"
        outside.write_bytes(original)
        self.loader.unlink()
        self.loader.symlink_to(outside)
        self.refused("escapes")
        self.loader.unlink()
        self.loader.write_bytes(b"x" * (1024 * 1024 + 1))
        self.refused("size bound")

    def test_retained_bundle_verifies_after_original_build_is_removed_and_it_is_relocated(self):
        destination = self.fixture.root / "capture"
        destination.mkdir()
        args = SimpleNamespace(build_directory=self.out, output=destination, artifact_root=None)
        retained = capture.retain_build(args, self.record)
        self.assertEqual(retained["mode"], "standalone")
        shutil.rmtree(self.out)
        moved = self.fixture.root / "relocated"
        shutil.move(destination, moved)
        with patch.object(boot.helper, "prepare", side_effect=AssertionError("offline verifier tried to repack files")), \
                patch.object(capture.subprocess, "Popen", side_effect=AssertionError("offline verifier executed a process")):
            self.assertEqual(capture.build_receipt(moved / "build"), self.record)
        self.assertEqual((moved / "build" / self.record["unmodified_source_image"]).read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
