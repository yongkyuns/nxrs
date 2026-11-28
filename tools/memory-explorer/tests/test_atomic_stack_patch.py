"""Retained-byte application, failure restoration and receipt admission."""

from contextlib import ExitStack
import hashlib
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import build_timing_nuttx as build


class AtomicStackPatch(unittest.TestCase):
    def setUp(self):
        self.cleanup = ExitStack()
        self.addCleanup(self.cleanup.close)
        root = Path(self.cleanup.enter_context(TemporaryDirectory())).resolve()
        self.nuttx, self.repo, self.output = [root / name for name in ("nuttx", "repo", "output")]
        for directory in (self.nuttx, self.repo, self.output):
            directory.mkdir()
        subprocess.run(["git", "init", "-q", str(self.nuttx)], check=True)
        subprocess.run(["git", "-C", str(self.nuttx), "config", "core.autocrlf", "true"], check=True)
        self.source = self.nuttx / build.ATOMIC_STACK_SOURCE
        self.source.parent.mkdir(parents=True)
        self.before, self.after = b"original\n", b"corrected\n"
        self.source.write_bytes(self.before)
        self.patch = self.repo / build.ATOMIC_STACK_PATCH
        self.patch.parent.mkdir(parents=True)
        self.patch.write_text(
            f"diff --git a/{build.ATOMIC_STACK_SOURCE} b/{build.ATOMIC_STACK_SOURCE}\n"
            f"--- a/{build.ATOMIC_STACK_SOURCE}\n+++ b/{build.ATOMIC_STACK_SOURCE}\n"
            "@@ -1 +1 @@\n-original\n+corrected\n"
        )
        self.patch_bytes = self.patch.read_bytes()
        self.cleanup.enter_context(patch.multiple(
            build, ATOMIC_STACK_BEFORE=hashlib.sha256(self.before).hexdigest(),
            ATOMIC_STACK_AFTER=hashlib.sha256(self.after).hexdigest()))
        self.commands = []

    def apply(self):
        return build.atomic_stack_transition(self.nuttx, self.repo, self.output, self.commands)

    def test_success_retains_applied_bytes_and_restores_exact_source(self):
        with self.apply() as receipt:
            self.assertEqual(self.source.read_bytes(), self.after)
            self.assertFalse(receipt["source_restored"])
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertTrue(receipt["source_restored"])
        self.assertEqual((self.output / "atomic-stack.patch").read_bytes(), self.patch_bytes)
        build.validate_atomic_stack_patch({"atomic_stack_patch": receipt}, self.repo, self.output)

    def test_compiler_failure_still_restores_exact_source(self):
        with self.assertRaisesRegex(RuntimeError, "compiler failure"):
            with self.apply() as receipt:
                raise RuntimeError("compiler failure")
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertTrue(receipt["source_restored"])

    def test_repository_patch_change_cannot_change_reversal(self):
        with self.assertRaisesRegex(RuntimeError, "patch changed"):
            with self.apply():
                self.patch.write_text("changed after application\n")
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_retained_patch_change_cannot_change_reversal(self):
        with self.assertRaisesRegex(RuntimeError, "patch changed"):
            with self.apply():
                (self.output / "atomic-stack.patch").write_text("changed after application\n")
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_unrelated_native_edit_is_not_discarded_or_admitted_as_restored(self):
        with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
            with self.apply() as receipt:
                self.source.write_bytes(self.after + b"unrelated\n")
        self.assertFalse(receipt["source_restored"])
        self.assertIn(b"unrelated", self.source.read_bytes())

    def test_wrong_preimage_is_rejected_without_native_mutation(self):
        self.source.write_bytes(b"different\n")
        with self.assertRaisesRegex(RuntimeError, "pinned preimage"):
            with self.apply():
                self.fail("entered context")
        self.assertEqual(self.source.read_bytes(), b"different\n")
        self.assertEqual(self.commands, [])

    def test_unexpected_patch_path_is_rejected_without_native_mutation(self):
        self.patch.write_text(self.patch.read_text().replace(build.ATOMIC_STACK_SOURCE, "other.c"))
        with self.assertRaisesRegex(RuntimeError, "unexpected source"):
            with self.apply():
                self.fail("entered context")
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertEqual(self.commands, [])

    def test_receipt_rejects_missing_changed_or_unrestored_identity(self):
        with self.apply() as receipt:
            pass
        for field, value in (
            ("path", "other.patch"), ("sha256", "0" * 64), ("source", "other.c"),
            ("before", "0" * 64), ("after", "0" * 64), ("source_restored", False),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "identity differs"):
                build.validate_atomic_stack_patch({"atomic_stack_patch": {**receipt, field: value}},
                                                  self.repo, self.output)
        with self.assertRaisesRegex(RuntimeError, "identity differs"):
            build.validate_atomic_stack_patch({}, self.repo, self.output)
        (self.output / "atomic-stack.patch").write_text("different retained patch\n")
        with self.assertRaisesRegex(RuntimeError, "Retained atomic"):
            build.validate_atomic_stack_patch({"atomic_stack_patch": receipt}, self.repo, self.output)


if __name__ == "__main__":
    unittest.main()
