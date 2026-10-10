"""Offline safety and provenance tests for optional Rust std proposals."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "rust_std_proposals", ROOT / "tools/apply-rust-std-proposals.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class StdProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rust-std-proposal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input_files = {"std/src/num/f32.rs": b"pinned original\n"}
        self.revision = "a" * 40
        self.proposal_name = "fixture-rfc"
        self.patch_name = "0001-fixture.patch"
        self.patch_dir = self.root / "proposals"
        self.patch_dir.mkdir()
        self.patch_path = self.patch_dir / self.patch_name
        self.patch_path.write_text(
            "diff --git a/std/src/num/f32.rs b/std/src/num/f32.rs\n"
            "--- a/std/src/num/f32.rs\n"
            "+++ b/std/src/num/f32.rs\n"
            "@@ -1 +1 @@\n"
            "-pinned original\n"
            "+patched fixture\n")
        self.manifest_path = self.root / "series.json"
        self.write_manifest()
        self.archive_path = self.root / "source.tar"
        self.write_archive(self.archive_path, self.input_files, directories=("./",))
        self.output = self.root / "private-output"

    @staticmethod
    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def write_manifest(self, *, patch_file=None, patch_sha=None, before=None,
                       revision=None):
        entry = {
            "state": "rfc",
            "qualification": "unqualified",
            "selection_scope": "evaluation-preparation-only",
            "source_revision": revision or self.revision,
            "target_os": "nuttx",
            "patch": {
                "file": patch_file or self.patch_name,
                "sha256": patch_sha or tool.sha256_file(self.patch_path),
            },
            "before_sha256": before or {
                path: self.digest(data) for path, data in self.input_files.items()},
        }
        self.manifest_path.write_text(json.dumps({
            "schema": 1,
            "proposals": {self.proposal_name: entry},
        }))

    @staticmethod
    def write_archive(path, files, *, directories=(), links=(), extra=()):
        with tarfile.open(path, "w") as archive:
            for directory in directories:
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            for name, payload in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            for name, linkname in links:
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
                archive.addfile(info)
            for member in extra:
                if isinstance(member, tuple):
                    archive.addfile(*member)
                else:
                    archive.addfile(member)

    def apply(self, *, archive=None, output=None, revision=None):
        return tool.apply_proposal(
            archive or self.archive_path,
            output or self.output,
            self.proposal_name,
            revision or self.revision,
            manifest_path=self.manifest_path,
            proposal_dir=self.patch_dir,
        )

    def test_fixture_archive_is_copied_patched_and_provenance_is_durable(self):
        original_archive = self.archive_path.read_bytes()
        provenance = self.apply()

        self.assertEqual(self.archive_path.read_bytes(), original_archive)
        self.assertEqual(
            (self.output / "snapshot/std/src/num/f32.rs").read_bytes(),
            b"patched fixture\n")
        self.assertEqual(provenance["snapshot_archive_sha256"], self.digest(original_archive))
        self.assertEqual(provenance["source_revision"], self.revision)
        self.assertEqual(provenance["selection_scope"], "evaluation-preparation-only")
        self.assertEqual(provenance["files"]["std/src/num/f32.rs"], {
            "before_sha256": self.digest(b"pinned original\n"),
            "after_sha256": self.digest(b"patched fixture\n"),
        })
        self.assertEqual(json.loads((self.output / "provenance.json").read_text()), provenance)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ["provenance.json", "snapshot"])

    def test_production_rust_source_pins_remain_the_three_frozen_blobs(self):
        manifest = json.loads(tool.PROPOSAL_MANIFEST.read_text())
        entry = manifest["proposals"]["nuttx-libm-inverse-hyperbolic-rfc"]
        self.assertEqual(entry["source_revision"],
                         "abf50ae2e46066e67e29fe856f9764c23aa9a3ca")
        self.assertEqual(entry["before_sha256"], {
            "std/src/num/f32.rs":
                "2ee2ec8aac6b6d6720e12c8b3d405c638c8328b0765c90a868a02942ec247daa",
            "std/src/num/f64.rs":
                "9e0e5a8539f90a791b6860e5139bf7a1f0e1578ce02b8043b0e36c2ee8e97eb9",
            "std/src/sys/cmath.rs":
                "2d3c6c5e70f6db7f76e8563f590b9fe33d473dcbf77fa155933bc7f1349b3884",
        })
        patch = tool.PROPOSAL_DIR / entry["patch"]["file"]
        self.assertEqual(tool.sha256_file(patch), entry["patch"]["sha256"])

    def test_changed_source_hash_is_rejected_without_output(self):
        changed_archive = self.root / "changed.tar"
        self.write_archive(changed_archive, {
            "std/src/num/f32.rs": b"modified by another patch\n"})

        with self.assertRaisesRegex(ValueError, "incompatible or already patched"):
            self.apply(archive=changed_archive)

        self.assertFalse(self.output.exists())

    def test_reapplication_is_rejected_from_postimage(self):
        self.apply()
        patched_archive = self.root / "patched.tar"
        self.write_archive(patched_archive, {
            "std/src/num/f32.rs":
                (self.output / "snapshot/std/src/num/f32.rs").read_bytes()})

        with self.assertRaisesRegex(ValueError, "incompatible or already patched"):
            self.apply(archive=patched_archive, output=self.root / "second-output")

        self.assertFalse((self.root / "second-output").exists())

    def test_wrong_revision_is_rejected_before_output(self):
        with self.assertRaisesRegex(ValueError, "revision does not match"):
            self.apply(revision="b" * 40)
        self.assertFalse(self.output.exists())

    def test_selection_scope_must_remain_preparation_only(self):
        manifest = json.loads(self.manifest_path.read_text())
        manifest["proposals"][self.proposal_name]["selection_scope"] = "application-build"
        self.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "selection must remain preparation-only"):
            self.apply()
        self.assertFalse(self.output.exists())

    def test_patch_hash_mismatch_is_rejected(self):
        self.write_manifest(patch_sha="0" * 64)
        with self.assertRaisesRegex(ValueError, "patch hash mismatch"):
            self.apply()
        self.assertFalse(self.output.exists())

    def test_archive_git_metadata_is_rejected(self):
        archive = self.root / "git.tar"
        self.write_archive(archive, self.input_files,
                           directories=(".git",),
                           extra=(self._tar_file(".git/config", b"[core]\n"),))
        with self.assertRaisesRegex(ValueError, "unsafe archive member path"):
            self.apply(archive=archive)
        self.assertFalse(self.output.exists())

    def test_archive_path_escape_is_rejected(self):
        archive = self.root / "escape.tar"
        self.write_archive(archive, self.input_files,
                           extra=(self._tar_file("../escaped", b"outside"),))
        with self.assertRaisesRegex(ValueError, "unsafe archive member path"):
            self.apply(archive=archive)
        self.assertFalse((self.root / "escaped").exists())
        self.assertFalse(self.output.exists())

    def test_archive_symlink_is_rejected(self):
        archive = self.root / "symlink.tar"
        self.write_archive(archive, self.input_files,
                           links=(("std/src/num/linked.rs", "../../outside"),))
        with self.assertRaisesRegex(ValueError, "links and special files"):
            self.apply(archive=archive)
        self.assertFalse(self.output.exists())

    def test_patch_paths_must_match_the_pinned_std_files(self):
        escaping_patch = (
            "diff --git a/../outside b/../outside\n"
            "--- a/../outside\n+++ b/../outside\n"
            "@@ -1 +1 @@\n-old\n+new\n")
        self.patch_path.write_text(escaping_patch)
        self.write_manifest(patch_sha=tool.sha256_file(self.patch_path))
        with self.assertRaises(ValueError):
            self.apply()
        self.assertFalse(self.output.exists())

    def test_unsafe_manifest_source_path_is_rejected(self):
        self.write_manifest(before={"std/src/../../outside": "0" * 64})
        with self.assertRaisesRegex(ValueError, "unsafe Rust std proposal path"):
            self.apply()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        witness = self.output / "keep"
        witness.write_text("user data")
        with self.assertRaisesRegex(ValueError, "output already exists"):
            self.apply()
        self.assertEqual(witness.read_text(), "user data")

    def test_duplicate_archive_path_is_rejected(self):
        archive = self.root / "duplicate.tar"
        self.write_archive(archive, self.input_files,
                           extra=(self._tar_file("std/src/num/f32.rs", b"shadow"),))
        with self.assertRaisesRegex(ValueError, "duplicate archive member path"):
            self.apply(archive=archive)
        self.assertFalse(self.output.exists())

    def test_failed_application_cleans_private_staging_directory(self):
        self.patch_path.write_text(self.patch_path.read_text().replace(
            "pinned original", "different context"))
        self.write_manifest(patch_sha=tool.sha256_file(self.patch_path))

        with self.assertRaisesRegex(ValueError, "incompatible or already applied"):
            self.apply()

        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".private-output.std-rfc-*")))

    def _tar_file(self, name, data):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        return info, io.BytesIO(data)


if __name__ == "__main__":
    unittest.main()
