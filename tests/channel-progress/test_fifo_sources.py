"""Negative controls for the source assumptions of ordered FIFO witnesses."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("channel_fifo_sources", HERE / "fifo_sources.py")
sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sources)


class KernelSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kernel = self.root / "kernel"
        self.output = self.root / "retained"
        self.data = {path: f"// synthetic source for {path}\n".encode()
                     for path in sources.KERNEL_SOURCE_SHA256}
        self.pins = {path: hashlib.sha256(data).hexdigest() for path, data in self.data.items()}
        for path, data in self.data.items():
            target = self.kernel / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        patched = mock.patch.object(sources, "KERNEL_SOURCE_SHA256", self.pins)
        patched.start()
        self.addCleanup(patched.stop)

    def validate(self, source_map=None, revision=None):
        return sources.validate_retained_kernel(
            self.output, self.pins if source_map is None else source_map,
            sources.KERNEL_REVISION if revision is None else revision)

    def test_verified_independent_copy_preserves_input_and_relative_paths(self):
        self.assertEqual(sources.verify_kernel(self.kernel), self.pins)
        record = sources.prepare_kernel(self.kernel, self.output)
        self.assertEqual(record, self.pins)
        self.assertEqual(self.validate(), record)
        for relative, data in self.data.items():
            source = self.kernel / relative
            retained = self.output / relative
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(retained.read_bytes(), data)
            self.assertNotEqual(source.stat().st_ino, retained.stat().st_ino)
            self.assertEqual(retained.stat().st_nlink, 1)
        record.clear()
        self.assertEqual(sources.verify_kernel(self.kernel), self.pins)

    def test_changed_or_missing_input_is_refused_before_creating_output(self):
        path = self.kernel / next(iter(self.pins))
        path.write_bytes(b"unreviewed scheduler change\n")
        with self.assertRaisesRegex(ValueError, "changed audited"):
            sources.prepare_kernel(self.kernel, self.output)
        self.assertFalse(self.output.exists())
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing or unreadable"):
            sources.prepare_kernel(self.kernel, self.output)
        self.assertFalse(self.output.exists())

    def test_preexisting_or_symlink_output_is_refused(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "must be new"):
            sources.prepare_kernel(self.kernel, self.output)
        self.output.rmdir()
        self.output.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(ValueError, "must be new"):
            sources.prepare_kernel(self.kernel, self.output)

    def test_source_symlinks_and_hardlinks_are_refused(self):
        relative = next(iter(self.pins))
        path = self.kernel / relative
        other = self.root / "outside.c"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "independent regular file"):
            sources.verify_kernel(self.kernel)
        path.unlink()
        path.hardlink_to(other)
        with self.assertRaisesRegex(ValueError, "independent regular file"):
            sources.verify_kernel(self.kernel)

    def test_root_and_intermediate_directory_symlinks_are_refused(self):
        link = self.root / "kernel-link"
        link.symlink_to(self.kernel, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "regular directory"):
            sources.verify_kernel(link)
        original = self.kernel / "sched"
        moved = self.root / "moved-sched"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "regular directory"):
            sources.verify_kernel(self.kernel)

    def test_unknown_revision_and_forged_source_map_are_refused(self):
        sources.prepare_kernel(self.kernel, self.output)
        with self.assertRaisesRegex(ValueError, "revision"):
            self.validate(revision="0" * 40)
        changed = dict(self.pins)
        changed[next(iter(changed))] = "0" * 64
        missing = dict(self.pins)
        missing.pop(next(iter(missing)))
        extra = {**self.pins, "sched/extra.c": "0" * 64}
        for mapping in (changed, missing, extra, [], True):
            with self.subTest(mapping=mapping):
                with self.assertRaisesRegex(ValueError, "source map"):
                    self.validate(source_map=mapping)

    def test_modified_retained_bytes_and_resealed_inventory_are_refused(self):
        sources.prepare_kernel(self.kernel, self.output)
        relative = next(iter(self.pins))
        path = self.output / relative
        path.write_bytes(b"unreviewed scheduler change\n")
        with self.assertRaisesRegex(ValueError, "changed audited"):
            self.validate()
        resealed = {**self.pins, relative: hashlib.sha256(path.read_bytes()).hexdigest()}
        with self.assertRaisesRegex(ValueError, "source map"):
            self.validate(source_map=resealed)

    def test_missing_extra_files_and_extra_directories_are_refused(self):
        sources.prepare_kernel(self.kernel, self.output)
        relative = next(iter(self.pins))
        path = self.output / relative
        data = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing or extra"):
            self.validate()
        path.write_bytes(data)
        extra = self.output / "extra.c"
        extra.write_bytes(b"unexpected source\n")
        with self.assertRaisesRegex(ValueError, "missing or extra"):
            self.validate()
        extra.unlink()
        extra.mkdir()
        with self.assertRaisesRegex(ValueError, "missing or extra"):
            self.validate()

    def test_retained_symlink_hardlink_and_nonregular_file_are_refused(self):
        sources.prepare_kernel(self.kernel, self.output)
        relative = next(iter(self.pins))
        path = self.output / relative
        path.unlink()
        path.symlink_to(self.kernel / relative)
        with self.assertRaisesRegex(ValueError, "link or nonregular"):
            self.validate()
        path.unlink()
        path.hardlink_to(self.kernel / relative)
        with self.assertRaisesRegex(ValueError, "independent regular file"):
            self.validate()
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "link or nonregular"):
            self.validate()


class ParkerSourceTests(unittest.TestCase):
    def test_exact_prepared_sdk_maps_pass(self):
        for sdk, pins in (("nightly-2025-09-15", sources.STD_SOURCE_SHA256),
                          ("esp-1.90.0.0", sources.ESP_STD_SOURCE_SHA256)):
            with self.subTest(sdk=sdk):
                sources.validate_std_sources({**pins, "unrelated/source.rs": "0" * 64}, sdk)

    def test_missing_changed_and_wrong_sdk_sources_are_refused(self):
        for sdk, pins, wrong in (
            ("nightly-2025-09-15", sources.STD_SOURCE_SHA256, sources.ESP_STD_SOURCE_SHA256),
            ("esp-1.90.0.0", sources.ESP_STD_SOURCE_SHA256, sources.STD_SOURCE_SHA256),
        ):
            for path in pins:
                missing = dict(pins)
                missing.pop(path)
                changed = {**pins, path: "0" * 64}
                for ledger in (missing, changed, wrong):
                    with self.subTest(sdk=sdk, path=path):
                        with self.assertRaisesRegex(ValueError, "missing or changed"):
                            sources.validate_std_sources(ledger, sdk)
        with self.assertRaisesRegex(ValueError, "unqualified SDK"):
            sources.validate_std_sources(sources.STD_SOURCE_SHA256, "unqualified")
        with self.assertRaisesRegex(ValueError, "must be an object"):
            sources.validate_std_sources([], "nightly-2025-09-15")


if __name__ == "__main__":
    unittest.main()
