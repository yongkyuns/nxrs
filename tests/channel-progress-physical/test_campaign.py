"""Host-only controls; synthetic repositories and files are not board evidence."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import campaign


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def commit(repository, message="Synthetic source inventory"):
    campaign.git(repository, "add", "-A")
    campaign.git(repository, "-c", "user.name=Campaign fixture", "-c",
                 "user.email=campaign-tests@example.invalid", "-c", "commit.gpgSign=false",
                 "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", message)
    return campaign.git(repository, "rev-parse", "HEAD").decode().strip()


class ManifestTests(unittest.TestCase):
    def test_exact_build_case_order_and_scope(self):
        manifest = campaign.load_manifest()
        self.assertEqual(manifest["artifact"]["id"], 13323366472)
        self.assertEqual(len(manifest["builds"]), 8)
        self.assertEqual(len(manifest["cases"]), 19)
        self.assertEqual([case["index"] for case in manifest["cases"]], list(range(1, 20)))
        self.assertEqual(len({item["build_id"] for item in manifest["builds"]}), 8)
        self.assertEqual(len({item["image_sha256"] for item in manifest["builds"]}), 8)
        self.assertEqual({case["mode"] for case in manifest["cases"]
                          if case["expected_no_repair_exit"] == 2},
                         {"direct-reserved", "select-reserved", "send-reserved"})
        self.assertTrue(all(manifest[flag] is False for flag in campaign.QUALIFICATION_FLAGS))

    def test_manifest_changes_cannot_select_other_firmware_or_claim_physical_success(self):
        mutators = (
            lambda value: value["artifact"].update(id=13321779022),
            lambda value: value["source"].update(revision="0" * 40),
            lambda value: value["builds"][0].update(build_receipt_sha256="0" * 64),
            lambda value: value["builds"][0].update(image_file="../image.bin"),
            lambda value: value["cases"].pop(),
            lambda value: value["cases"].append(value["cases"][0]),
            lambda value: value["cases"][0].update(build_mode="select-reserved"),
            lambda value: value.update(boot_image_profile="platform-default"),
            lambda value: value.update(physical_execution_verified=True),
        )
        for index, mutate in enumerate(mutators):
            with self.subTest(mutation=index):
                value = copy.deepcopy(campaign.load_manifest())
                mutate(value)
                with self.assertRaisesRegex(ValueError, "frozen candidate"):
                    campaign.validate_manifest(value)

    def test_duplicate_json_keys_and_linked_manifest_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "duplicate.json"
            path.write_text('{"schema": "one", "schema": "two"}')
            with self.assertRaises(ValueError):
                campaign.load_manifest(path)
            path.unlink()
            path.symlink_to(campaign.DEFAULT_MANIFEST)
            with self.assertRaisesRegex(ValueError, "linked"):
                campaign.load_manifest(path)

    def test_output_is_never_written_inside_the_input_build_root(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            out = root / "preflight.json"
            with mock.patch.object(campaign, "preflight") as preflight:
                self.assertEqual(campaign.main(["preflight", "--build-root", str(root),
                                                "--output", str(out)]), 1)
            preflight.assert_not_called()
            self.assertFalse(out.exists())

    def test_output_never_overwrites_an_existing_record(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            out = root / "preflight.json"
            out.write_text("original result\n")
            with mock.patch.object(campaign, "preflight") as preflight:
                self.assertEqual(campaign.main(["preflight", "--build-root", str(root / "builds"),
                                                "--output", str(out)]), 1)
            preflight.assert_not_called()
            self.assertEqual(out.read_text(), "original result\n")


class SourceInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.source_tmp.cleanup)
        cls.root = Path(cls.source_tmp.name) / "source"
        cls.root.mkdir()
        spec = importlib.util.spec_from_file_location("physical_campaign_builder",
                                                      campaign.FIXTURE / "build.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        profile = builder.baseline.resolve_platform(campaign.PLATFORM, builder.ROOT)
        cls.inputs = builder.inventory(profile)
        for name in cls.inputs:
            destination = cls.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(builder.ROOT / name, destination)
        campaign.git(cls.root, "init", "--quiet")
        cls.revision = commit(cls.root)
        cls.tree = campaign.git(cls.root, "rev-parse", "HEAD^{tree}").decode().strip()

    def synthetic_binding(self):
        # Change only the synthetic source anchor, never the production fixture
        # loader. Git inventory selection and byte verification remain real.
        manifest = copy.deepcopy(campaign.load_manifest())
        manifest["source"].update(head=self.revision, revision=self.revision, tree=self.tree,
                                  input_count=len(self.inputs),
                                  inventory_sha256=campaign.evidence.canonical(self.inputs))
        receipt = {"source_revision": self.revision, "platform": campaign.PLATFORM,
                   "target": campaign.TARGET, "source_sha256": copy.deepcopy(self.inputs)}
        return manifest, receipt

    def copy_repository(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "source"
        shutil.copytree(self.root, root)
        return root

    def test_independent_git_policy_matches_build_recipe_and_excludes_host_campaign_tools(self):
        inventory = campaign.git_source_inventory(self.root, self.revision)
        self.assertEqual(inventory, self.inputs)
        self.assertEqual(len(inventory), len(self.inputs))
        self.assertIn("platform/build/src/nuttx.rs", inventory)
        self.assertIn("upstream/build-patches.json", inventory)
        # Frozen firmware remains a separate immutable source cohort.
        self.assertEqual(campaign.load_manifest()["source"]["input_count"], 163)
        self.assertFalse(any(name.startswith("tests/channel-progress-physical/") for name in inventory))

    def test_complete_git_binding_succeeds_without_using_current_working_files(self):
        root = self.copy_repository()
        (root / "Cargo.toml").write_text("uncommitted unrelated replacement")
        manifest, receipt = self.synthetic_binding()
        with mock.patch.object(campaign, "load_manifest", return_value=manifest):
            result = campaign.verify_sources(root, receipt)
        self.assertEqual(result["source_revision"], self.revision)
        self.assertEqual(result["source_input_count"], len(self.inputs))

    def test_omitted_extra_changed_and_stale_receipt_source_inputs_are_rejected(self):
        for mutation in ("missing", "extra", "changed", "revision", "tree"):
            with self.subTest(mutation=mutation):
                manifest, receipt = self.synthetic_binding()
                if mutation == "missing":
                    receipt["source_sha256"].pop("Cargo.toml")
                elif mutation == "extra":
                    receipt["source_sha256"]["forged/input.rs"] = "0" * 64
                elif mutation == "changed":
                    receipt["source_sha256"]["Cargo.toml"] = "0" * 64
                elif mutation == "revision":
                    receipt["source_revision"] = "0" * 40
                else:
                    manifest["source"]["tree"] = "0" * 40
                with mock.patch.object(campaign, "load_manifest", return_value=manifest):
                    with self.assertRaises(ValueError):
                        campaign.verify_sources(self.root, receipt)

    def test_missing_required_git_input_is_rejected(self):
        root = self.copy_repository()
        (root / "tools/prepare-esp-hal-cache.sh").unlink()
        revision = commit(root)
        with self.assertRaisesRegex(ValueError, "lacks required ESP"):
            campaign.git_source_inventory(root, revision)

    def test_symlinked_git_input_is_rejected(self):
        root = self.copy_repository()
        source = root / "tests/std-channel-perf/native.c"
        source.unlink()
        source.symlink_to("native.h")
        revision = commit(root)
        with self.assertRaisesRegex(ValueError, "regular Git blobs"):
            campaign.git_source_inventory(root, revision)

    def test_new_fixture_sources_are_included_but_sibling_campaign_tools_are_excluded(self):
        root = self.copy_repository()
        fixture = root / "tests/channel-progress/new_fixture.py"
        fixture.write_text("# New firmware input\n")
        sibling = root / "tests/channel-progress-physical/new_host_tool.py"
        sibling.parent.mkdir()
        sibling.write_text("# Host-only tool\n")
        revision = commit(root)
        inventory = campaign.git_source_inventory(root, revision)
        self.assertIn(fixture.relative_to(root).as_posix(), inventory)
        self.assertNotIn(sibling.relative_to(root).as_posix(), inventory)
        self.assertEqual(len(inventory), len(self.inputs) + 1)

    def test_unsupported_profile_inheritance_target_or_hal_is_rejected(self):
        for change in ('extends = "other"\n', 'target = "different"\n', 'hal = {imu="mock"}\n'):
            with self.subTest(change=change):
                root = self.copy_repository()
                path = root / campaign.PROFILE_SOURCE
                text = path.read_text()
                if change.startswith("target"):
                    text = text.replace('target = "xtensa-esp32s3-nuttx"', change.strip())
                elif change.startswith("hal"):
                    text = text.replace("hal = {}", change.strip())
                else:
                    text = change + text
                path.write_text(text)
                revision = commit(root)
                with self.assertRaisesRegex(ValueError, "source policy only supports"):
                    campaign.git_source_inventory(root, revision)


class FixedBuildBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.receipt = {"mode": "direct-reserved", "boot_image_profile": campaign.PROFILE,
                        "platform": campaign.PLATFORM, "target": campaign.TARGET,
                        "source_revision": campaign.PINNED_REVISION, "build_id": "1" * 64,
                        "image_file": campaign.IMAGE_FILE, "runtime_kind": "unmeasured"}
        for relative, data in ((campaign.IMAGE_FILE, b"candidate"),
                               (campaign.ORIGINAL_IMAGE_FILE, b"original!")):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.receipt["image_sha256"] = campaign.evidence.sha256(self.root / campaign.IMAGE_FILE)
        write_json(self.root / "build.json", self.receipt)
        self.selected = {key: self.receipt[key] for key in ("mode", "build_id", "image_file", "image_sha256")}
        self.selected.update(build_receipt_sha256=campaign.evidence.sha256(self.root / "build.json"),
                             original_image_sha256=campaign.evidence.sha256(self.root / campaign.ORIGINAL_IMAGE_FILE))

    def test_receipt_pin_checked_before_expensive_build_validation(self):
        self.receipt["build_id"] = "2" * 64
        write_json(self.root / "build.json", self.receipt)
        with mock.patch.object(campaign.evidence, "build_receipt") as validate:
            with self.assertRaisesRegex(ValueError, "receipt bytes differ"):
                campaign.verify_build(self.root, self.selected)
        validate.assert_not_called()

    def test_matching_receipt_pin_still_requires_real_proof_validator_success(self):
        with mock.patch.object(campaign.evidence, "build_receipt", side_effect=ValueError("missing proof")):
            with self.assertRaisesRegex(ValueError, "missing proof"):
                campaign.verify_build(self.root, self.selected)

    def test_different_profile_cannot_pass_even_with_matching_receipt_bytes(self):
        self.receipt["boot_image_profile"] = "platform-default"
        write_json(self.root / "build.json", self.receipt)
        self.selected["build_receipt_sha256"] = campaign.evidence.sha256(self.root / "build.json")
        with mock.patch.object(campaign.evidence, "build_receipt", return_value=self.receipt):
            with self.assertRaisesRegex(ValueError, "profile, platform or source"):
                campaign.verify_build(self.root, self.selected)

    def test_selected_and_original_images_must_be_complete_flash_sized_files(self):
        with mock.patch.object(campaign.evidence, "build_receipt", return_value=self.receipt):
            with self.assertRaisesRegex(ValueError, "complete selected 16 MiB"):
                campaign.verify_build(self.root, self.selected)
            with mock.patch.object(campaign, "FLASH_BYTES", 9):
                result = campaign.verify_build(self.root, self.selected)
                self.assertEqual(result, self.receipt)
                (self.root / campaign.ORIGINAL_IMAGE_FILE).write_bytes(b"short")
                with self.assertRaisesRegex(ValueError, "original image differs"):
                    campaign.verify_build(self.root, self.selected)

    def test_linked_retained_proof_is_rejected(self):
        (self.root / "linked-proof").symlink_to(self.root / "build.json")
        with self.assertRaisesRegex(ValueError, "linked or non-regular"):
            campaign.verify_build(self.root, self.selected)

    def test_extra_or_missing_build_directory_refuses_before_reading_receipts(self):
        manifest = campaign.load_manifest()
        for mutation in ("extra", "missing"):
            with self.subTest(mutation=mutation):
                build_root = self.root / mutation
                build_root.mkdir()
                for mode in campaign.BUILDS:
                    (build_root / mode).mkdir()
                if mutation == "extra":
                    (build_root / "unexpected-build").mkdir()
                else:
                    (build_root / campaign.BUILDS[-1]).rmdir()
                with mock.patch.object(campaign, "verify_build") as validate:
                    with self.assertRaisesRegex(ValueError, "exactly the eight"):
                        campaign.preflight(build_root, self.root, manifest)
                validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
