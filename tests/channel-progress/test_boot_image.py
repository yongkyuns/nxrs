"""P1 boot-profile binding controls; synthetic images are never firmware evidence."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock

import test_analyze as fixtures
import test_build as build_tests


analyze = fixtures.analyze
build = build_tests.build
boot = build.boot_image
shapes = fixtures.module("progress_boot_image_shapes", build.ROOT / "tests/std-channel-perf/test_esp_image.py")
with mock.patch.dict(sys.modules, {"analyze": analyze}):
    capture = fixtures.module("progress_boot_capture_tests", build.HERE / "capture.py")


class CandidateEvidence:
    """Add real packing/proof validation to an existing authenticated fixture."""
    def __init__(self, test, fixture):
        self.test, self.fixture = test, fixture
        self.root = fixture.root
        self.firmware = self.root / "firmware"
        (self.firmware / "nuttx").mkdir(parents=True, exist_ok=True)
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
            b"/* SYNTHETIC test loader; never executable firmware */\n"
            b"if (image_header.hash_appended == 1) { offset += 32; }\n")
        # Freeze only the expected loader hash for these synthetic bytes. Every
        # mutation below keeps that independent pin unchanged. P0's native
        # regression separately compiles and exercises the exact C parsers.
        pin = mock.patch.object(boot.helper, "LOADER_AFTER", analyze.sha256(self.loader))
        pin.start()
        test.addCleanup(pin.stop)
        ledger = {
            "component": "nuttx", "upstream_revision": boot.helper.KERNEL_REVISION,
            "patches": [{"name": boot.helper.PATCH_NAME,
                         "sha256": analyze.sha256(boot.helper.PATCH),
                         "files": {boot.helper.LOADER_PATH: {
                             "before": boot.helper.LOADER_BEFORE,
                             "after": boot.helper.LOADER_AFTER}}}],
        }
        fixture.write("firmware/nuttx-patches.json", ledger)
        self.candidate, fields, _ = boot.prepare(self.firmware, self.original, self.root)
        self.fields = tuple(fields)
        self.record = fixture.receipt
        self.record.update(fields)
        self.record.update(
            boot_image_profile=boot.PROFILE, platform="esp32s3-service-footprint",
            target="xtensa-esp32s3-nuttx", sdk="esp-1.90.0.0",
            kernel_revision=boot.helper.KERNEL_REVISION,
            image_file=self.candidate.relative_to(self.root).as_posix(),
            elf_file="firmware/nuttx/nuttx", config_file="firmware/resolved.config",
        )
        self.source_names = (
            "tests/std-channel-perf/simpleboot_digest.py",
            "tests/std-channel-perf/inspect_esp_image.py",
            "tools/apply-nuttx-patches.py",
            "upstream/nuttx/patches/" + boot.helper.PATCH_NAME,
        )
        self.record["source_sha256"].update({
            name: analyze.sha256(build.ROOT / name) for name in self.source_names})
        fixture.refresh()

    def rehash(self):
        """Reseal outer claims so mutations reach the independent semantic checks."""
        for name in self.record["artifacts_sha256"]:
            self.record["artifacts_sha256"][name] = analyze.sha256(self.root / name)
        for name in ("image", "elf", "config"):
            self.record[name + "_sha256"] = analyze.sha256(self.root / self.record[name + "_file"])
        self.record["build_id"] = analyze.canonical({key: self.record[key] for key in
                                                   analyze.identity_fields(self.record["mode"], self.record)})
        (self.root / "build.json").write_text(json.dumps(self.record, indent=2) + "\n")

    def read(self, field):
        return analyze.read_json(self.root / self.record[field])

    def write(self, field, value):
        (self.root / self.record[field]).write_text(json.dumps(value) + "\n")

    def refused(self, message=None):
        self.rehash()
        self.test.assertEqual(self.record["build_id"], build.canonical({
            key: self.record[key] for key in build.identity_fields(self.record["mode"], self.record)}))
        with self.test.assertRaisesRegex(ValueError, message or "."):
            analyze.build_receipt(self.root, self.record["mode"])


class BootImageControls(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BuildEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.evidence = CandidateEvidence(self, self.fixture)

    def test_imports_restore_search_path_and_refuse_foreign_cached_helpers(self):
        before = list(sys.path)
        self.assertIs(boot._load_helper(), boot.helper)
        self.assertEqual(sys.path, before)
        for name in ("inspect_esp_image", "simpleboot_digest"):
            foreign = ModuleType(name)
            foreign.__file__ = str(self.fixture.root / (name + ".py"))
            with self.subTest(name=name), mock.patch.dict(sys.modules, {name: foreign}):
                with self.assertRaisesRegex(ImportError, "own"):
                    boot._load_helper()
                self.assertEqual(sys.path, before)
        with mock.patch.object(boot.importlib, "import_module", side_effect=RuntimeError("synthetic import failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                boot._load_helper()
        self.assertEqual(sys.path, before)

    def test_candidate_preserves_original_retains_proofs_and_cannot_overwrite_outputs(self):
        evidence = self.evidence
        self.assertEqual(analyze.build_receipt(evidence.root, "select-reserved"), evidence.record)
        self.assertEqual(evidence.original.read_bytes(), evidence.data)
        self.assertEqual(len(evidence.candidate.read_bytes()), len(evidence.data))
        self.assertNotEqual(analyze.sha256(evidence.original), analyze.sha256(evidence.candidate))
        for field in evidence.fields:
            self.assertIn(evidence.record[field], evidence.record["artifacts_sha256"])
        packing = evidence.read("simpleboot_digest_report")
        self.assertEqual(packing["qualification"], "build-candidate-only")
        for claim in ("boot_warning_resolved", "secure_boot_qualified", "physical_timing_qualified"):
            self.assertIs(packing[claim], False)
        before = {path: path.read_bytes() for path in (evidence.original, evidence.candidate)}
        with self.assertRaisesRegex(ValueError, "already exists"):
            boot.prepare(evidence.firmware, evidence.original, evidence.root)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_unpatched_archived_image_cannot_be_prepared_as_a_candidate(self):
        root = self.fixture.root / "legacy-build"
        firmware = root / "firmware"
        original = firmware / "nuttx/nuttx.merged.bin"
        original.parent.mkdir(parents=True)
        original.write_bytes(self.evidence.data)
        (firmware / "resolved.config").write_bytes((self.evidence.firmware / "resolved.config").read_bytes())
        loader = firmware / "nuttx" / boot.helper.LOADER_PATH
        loader.parent.mkdir(parents=True)
        loader.write_bytes(b"/* synthetic unpatched loader */")
        (firmware / "nuttx-patches.json").write_text(json.dumps({
            "component": "nuttx", "upstream_revision": boot.helper.KERNEL_REVISION, "patches": []}))
        with self.assertRaisesRegex(ValueError, "must be built first"):
            boot.prepare(firmware, original, root)
        self.assertEqual(original.read_bytes(), self.evidence.data)
        for name in ("nuttx/nuttx.rom-digest.bin", "simpleboot-digest.json", boot.helper.PATCH_NAME):
            self.assertFalse((firmware / name).exists())

    def test_default_and_missing_profiles_refuse_rehashed_candidate_downgrades(self):
        evidence = self.evidence
        original = copy.deepcopy(evidence.record)
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                evidence.record = copy.deepcopy(original)
                if explicit:
                    evidence.record["boot_image_profile"] = boot.DEFAULT
                else:
                    del evidence.record["boot_image_profile"]
                evidence.refused("explicit boot image profile")
                for field in evidence.fields:
                    del evidence.record[field]
                evidence.refused("candidate output")
                # Removing profile/proof fields and renaming the bytes cannot
                # turn a digest-bearing candidate into a historical image.
                del evidence.record["artifacts_sha256"][evidence.record["image_file"]]
                evidence.record["image_file"] = evidence.original.relative_to(evidence.root).as_posix()
                evidence.original.write_bytes(evidence.candidate.read_bytes())
                evidence.refused("digest-bearing ESP image")

    def test_valid_prefix_digest_does_not_admit_changed_mapped_payload(self):
        evidence = self.evidence
        changed = bytearray(evidence.candidate.read_bytes())
        changed[-1] ^= 1
        evidence.candidate.write_bytes(changed)
        self.assertIs(boot.helper.parse_image(changed)["ram_prefix_digest_verified"], True)
        packing = evidence.read("simpleboot_digest_report")
        packing["output_sha256"] = analyze.sha256(evidence.candidate)
        evidence.write("simpleboot_digest_report", packing)
        evidence.refused("exact repacking")

    def test_rehashed_loader_and_matching_ledgers_cannot_replace_loader_pin(self):
        evidence = self.evidence
        evidence.loader.write_bytes(evidence.loader.read_bytes() + b"/* changed loader */\n")
        altered = analyze.sha256(evidence.loader)
        ledger = evidence.read("simpleboot_patch_receipt")
        ledger["patches"][0]["files"][boot.helper.LOADER_PATH]["after"] = altered
        evidence.write("simpleboot_patch_receipt", ledger)
        packing = evidence.read("simpleboot_digest_report")
        packing["loader"]["loader_source_sha256"] = altered
        evidence.write("simpleboot_digest_report", packing)
        evidence.refused("source/ledger mismatch")

    def test_rehashed_patch_and_matching_claims_cannot_replace_patch_pin(self):
        evidence = self.evidence
        patch = evidence.root / evidence.record["simpleboot_loader_patch"]
        patch.write_bytes(patch.read_bytes() + b"\nUnreviewed patch change\n")
        altered = analyze.sha256(patch)
        ledger = evidence.read("simpleboot_patch_receipt")
        ledger["patches"][0]["sha256"] = altered
        evidence.write("simpleboot_patch_receipt", ledger)
        packing = evidence.read("simpleboot_digest_report")
        packing["loader"]["patch_sha256"] = altered
        evidence.write("simpleboot_digest_report", packing)
        evidence.record["source_sha256"]["upstream/nuttx/patches/" + boot.helper.PATCH_NAME] = altered
        evidence.refused("source inventory mismatch")

    def test_candidate_source_helpers_inspector_driver_and_patch_are_bound(self):
        evidence = self.evidence
        original = dict(evidence.record["source_sha256"])
        for name in evidence.source_names:
            with self.subTest(source=name):
                evidence.record["source_sha256"] = {**original, name: "0" * 64}
                evidence.refused("source inventory mismatch")

    def test_rehashed_target_sdk_kernel_and_configuration_mismatches_refuse(self):
        evidence = self.evidence
        original = copy.deepcopy(evidence.record)
        for field, value in (("platform", "mps2-an521-mock"), ("kernel_revision", "0" * 40),
                             ("target", "xtensa-other-nuttx"), ("sdk", "esp-other")):
            with self.subTest(field=field):
                evidence.record = {**copy.deepcopy(original), field: value}
                evidence.refused("service-footprint|kernel pin|target/SDK")
        evidence.record = original
        config = evidence.firmware / "resolved.config"
        data = config.read_bytes()
        for changed in (data.replace(b"CONFIG_ESPRESSIF_SIMPLE_BOOT=y", b"# CONFIG_ESPRESSIF_SIMPLE_BOOT is not set"),
                        data + b"CONFIG_ARCH_CHIP_ESP32S3=y\n"):
            config.write_bytes(changed)
            evidence.refused("exactly one enabled")

    def test_missing_proofs_duplicate_patch_and_duplicate_json_keys_refuse(self):
        evidence = self.evidence
        original = copy.deepcopy(evidence.record)
        for field in evidence.fields:
            with self.subTest(missing=field):
                evidence.record = copy.deepcopy(original)
                del evidence.record["artifacts_sha256"][evidence.record[field]]
                evidence.refused("absent from artifact inventory")
        evidence.record = original
        ledger = evidence.read("simpleboot_patch_receipt")
        ledger["patches"] *= 2
        evidence.write("simpleboot_patch_receipt", ledger)
        evidence.refused("must be built first")
        ledger["patches"] = ledger["patches"][:1]
        path = evidence.root / evidence.record["simpleboot_patch_receipt"]
        path.write_text(json.dumps(ledger)[:-1] + ', "component": "nuttx"}')
        evidence.refused("duplicate boot evidence JSON key")

    def test_rehashed_packing_claims_and_wrong_boolean_types_refuse(self):
        evidence = self.evidence
        original = evidence.read("simpleboot_digest_report")
        for field, value in (("boot_warning_resolved", True), ("secure_boot_qualified", 0),
                             ("prefix_digest_verified", 1), ("physical_timing_qualified", float("nan")),
                             ("digest_offset", original["digest_offset"] + 32),
                             ("qualification", "physical-qualified")):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(original)
                changed[field] = value
                evidence.write("simpleboot_digest_report", changed)
                evidence.refused()

    def test_retained_capture_replays_after_build_removal_and_relocation(self):
        evidence = self.evidence
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "capture"
            destination.mkdir()
            capture.retain_build(evidence.record, evidence.root, destination)
            shutil.rmtree(evidence.root)
            moved = Path(temporary) / "relocated"
            shutil.move(destination, moved)
            with mock.patch.object(boot.helper, "prepare", side_effect=AssertionError("offline verification wrote images")), \
                    mock.patch.object(capture.subprocess, "Popen", side_effect=AssertionError("offline verification ran a process")):
                self.assertEqual(analyze.build_receipt(moved, "select-reserved"), evidence.record)
            self.assertEqual((moved / evidence.record["unmodified_source_image"]).read_bytes(), evidence.data)


class SuiteCandidateControls(unittest.TestCase):
    def test_candidate_keeps_std_primitive_source_binding_and_narrow_acceptance(self):
        import test_suite_binding
        suite = test_suite_binding.SuiteBindingTests()
        suite.setUp()
        self.addCleanup(suite.doCleanups)
        suite.prepare_clean_suite()
        candidate = CandidateEvidence(self, suite.build)
        library = analyze.read_json(candidate.root / "prepared/std-library-sources.json")
        library.update(analyze.std_transfer_validator().ESP_STD_SOURCE_SHA256)
        suite.reseal_std_library(library)
        for mode in analyze.STD_TRANSFER_MODES:
            with self.subTest(mode=mode):
                receipt = analyze.build_receipt(candidate.root, mode)
                bound = analyze.bind_build(suite.summary(mode), receipt)
                analyze.require_no_repair(bound)
                self.assertEqual(receipt["boot_image_profile"], boot.PROFILE)
                self.assertEqual(bound["scope"], "std-transfer-primitives")
                self.assertFalse(bound["physical_execution_verified"])
                self.assertFalse(bound["production_transport_qualified"])
        library["std/src/sys/sync/mutex/mod.rs"] = "0" * 64
        suite.reseal_std_library(library)
        candidate.refused("audited std primitive source")

    def test_candidate_keeps_fifo_kernel_binding_and_narrow_acceptance(self):
        import test_fifo_binding
        suite = test_fifo_binding.FifoBindingTests()
        suite.setUp()
        self.addCleanup(suite.doCleanups)
        suite.prepare_fifo_suite()
        candidate = CandidateEvidence(self, suite.build)
        library = analyze.read_json(candidate.root / "prepared/std-library-sources.json")
        library.update(analyze.std_transfer_validator().ESP_STD_SOURCE_SHA256)
        library.update(suite.kernel.ESP_STD_SOURCE_SHA256)
        suite.reseal_std_library(library)
        for mode in analyze.FIFO_MODES:
            with self.subTest(mode=mode):
                receipt = analyze.build_receipt(candidate.root, mode)
                bound = analyze.bind_build(suite.summary(mode), receipt)
                analyze.require_no_repair(bound)
                self.assertEqual(receipt["boot_image_profile"], boot.PROFILE)
                self.assertEqual(bound["scope"], "equal-fifo-reservation-progress")
                self.assertFalse(bound["physical_execution_verified"])
                self.assertFalse(bound["production_transport_qualified"])
        relative = next(iter(suite.kernel_bytes))
        (candidate.root / "prepared/fifo-kernel" / relative).write_bytes(b"changed retained kernel")
        candidate.refused("changed audited FIFO kernel source")


if __name__ == "__main__":
    unittest.main()
