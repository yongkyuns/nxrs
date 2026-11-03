"""Negative controls for pinned preimages, private copies and actual std receipts."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "channel_prepare_test", Path(__file__).with_name("prepare-std.py"))
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)

FIXTURE = (prepare.IMPORT + "\nfn start_send() {\n"
           "                match self.tail.compare_exchange_weak(tail, new_tail) {\n"
           + prepare.RESERVED
           + "                        token.array.slot = slot;\n"
           "                        return true;\n                    }\n                }\n}\n"
           "unsafe fn write() {\n        unsafe {\n"
           "            slot.msg.get().write(MaybeUninit::new(msg));\n"
           + prepare.PUBLISHED + "        self.receivers.notify();\n}\n").encode()
SDK = "nightly-2025-09-15"


class TransformTests(unittest.TestCase):
    def test_latency_is_byte_identical_and_has_no_hook(self):
        self.assertEqual(prepare.transform(FIXTURE, "latency"), FIXTURE)
        self.assertNotIn(prepare.HOOK.encode(), prepare.transform(FIXTURE, "latency"))

    def test_hooks_surround_actual_publication_not_the_whole_send(self):
        reserved = prepare.transform(FIXTURE, "witness-reserved").decode()
        self.assertLess(reserved.index("compare_exchange_weak"), reserved.index(prepare.HOOK + "(1)"))
        self.assertLess(reserved.index(prepare.HOOK + "(1)"), reserved.index("slot.msg.get().write"))
        published = prepare.transform(FIXTURE, "witness-published").decode()
        self.assertLess(published.index("slot.stamp.store"), published.index(prepare.HOOK + "(2)"))
        self.assertLess(published.index(prepare.HOOK + "(2)"), published.index("self.receivers.notify"))
        self.assertEqual(reserved.count(prepare.HOOK + "(1)"), 1)
        self.assertEqual(published.count(prepare.HOOK + "(2)"), 1)
        self.assertNotIn(prepare.HOOK + "(2)", reserved)
        self.assertNotIn(prepare.HOOK + "(1)", published)

    def test_drift_reapplication_wrong_order_and_unknown_mode_are_rejected(self):
        bad_inputs = (
            FIXTURE.replace(prepare.RESERVED.encode(), b"changed"),
            FIXTURE.replace(prepare.PUBLISHED.encode(), b"changed"),
            FIXTURE + prepare.RESERVED.encode(),
            prepare.transform(FIXTURE, "witness-published"),
            prepare.PUBLISHED.encode() + prepare.IMPORT.encode() + prepare.RESERVED.encode(),
        )
        for data in bad_inputs:
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                prepare.transform(data, "witness-reserved")
        with self.assertRaises(ValueError):
            prepare.transform(FIXTURE, "unknown")
        # Assertions being disabled must not disable production pin checks.
        for sdk in prepare.PINS:
            with self.assertRaisesRegex(ValueError, "preimage"):
                prepare.checked_transform(FIXTURE, sdk, "latency")

    def test_whole_file_hash_is_checked_even_when_anchors_match(self):
        with patch.dict(prepare.PINS, {SDK: prepare.identity(FIXTURE)}):
            self.assertEqual(prepare.checked_transform(FIXTURE, SDK, "latency"), FIXTURE)
            with self.assertRaisesRegex(ValueError, "preimage"):
                prepare.checked_transform(FIXTURE + b"\n", SDK, "latency")
            with self.assertRaisesRegex(ValueError, "preimage"):
                prepare.checked_transform(FIXTURE, "unknown", "latency")


class PrivateSdkTests(unittest.TestCase):
    def source(self, root):
        source = root / "installed"
        (source / prepare.ARRAY).parent.mkdir(parents=True)
        (source / prepare.ARRAY).write_bytes(FIXTURE)
        binary = source / "bin/rustc"
        binary.parent.mkdir()
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib,sys\n"
            "if sys.argv[1:] == ['--print', 'sysroot']:\n"
            " print(pathlib.Path(__file__).resolve().parents[1])\n"
            "else:\n print('synthetic regression compiler; not firmware evidence')\n"
        )
        binary.chmod(0o755)
        (source / "lib/librustc_driver-0123456789abcdef.so").write_bytes(
            b"synthetic regression driver; not firmware evidence")
        return source

    @staticmethod
    def common(source, out, sdk):
        shutil.copytree(source, out / "toolchain")
        (out / "std-patch.json").write_text(json.dumps({"patchset_sha256": {}}))

    def prepare_fixture(self, root, mode="witness-reserved"):
        source = self.source(root)
        out = root / "private"
        with patch.dict(prepare.PINS, {SDK: prepare.identity(FIXTURE)}), \
                patch.object(prepare, "prepare_common_sdk", self.common):
            record = prepare.prepare(source, out, SDK, mode)
        return source, out, record

    def test_installed_source_is_unchanged_and_each_mode_validates_actual_bytes(self):
        for mode in prepare.MODES:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                source, out, record = self.prepare_fixture(Path(temporary), mode)
                self.assertEqual((source / prepare.ARRAY).read_bytes(), FIXTURE)
                self.assertNotEqual((source / prepare.ARRAY).stat().st_ino,
                                    (out / "toolchain" / prepare.ARRAY).stat().st_ino)
                self.assertEqual(record["hook_stage"], prepare.MODES[mode])
                self.assertEqual(record["channel_instrumented"], mode != "latency")
                with patch.dict(prepare.PINS, {SDK: prepare.identity(FIXTURE)}):
                    prepare.validate_receipt(out, mode)

    def test_forged_receipt_cannot_relabel_a_witness_as_latency(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, out, record = self.prepare_fixture(Path(temporary))
            record.update(mode="latency", hook_stage=0, selected=prepare.identity(FIXTURE))
            (out / "std-channel-hook.json").write_text(json.dumps(record))
            with patch.dict(prepare.PINS, {SDK: prepare.identity(FIXTURE)}), \
                    self.assertRaisesRegex(ValueError, "selected std array"):
                prepare.validate_receipt(out, "latency")

    def test_changed_source_compiler_and_inventory_are_rejected(self):
        for changed in ("array", "other-source", "compiler", "inventory"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                _, out, _ = self.prepare_fixture(Path(temporary))
                if changed == "array":
                    path = out / "toolchain" / prepare.ARRAY
                    path.write_bytes(path.read_bytes() + b"\n")
                elif changed == "other-source":
                    (out / "toolchain" / prepare.LIBRARY / "new.rs").write_text("changed")
                elif changed == "compiler":
                    path = out / "toolchain/bin/rustc"
                    path.write_text(path.read_text() + "# modified compiler\n")
                else:
                    (out / "std-library-sources.json").write_text("{}")
                with patch.dict(prepare.PINS, {SDK: prepare.identity(FIXTURE)}), \
                        self.assertRaises(ValueError):
                    prepare.validate_receipt(out)

    def test_transient_driver_copy_is_ignored_but_real_driver_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, out, record = self.prepare_fixture(Path(temporary))
            library = out / "toolchain/lib"
            real_driver = "librustc_driver-0123456789abcdef.so"
            self.assertEqual(set(record["compiler"]["driver_sha256"]), {real_driver})
            for transient in ("." + real_driver + ".8mlNEd", real_driver + ".tmp"):
                (library / transient).write_bytes(b"incomplete compiler copy")
            with patch.dict(prepare.PINS, {SDK: prepare.identity(FIXTURE)}):
                prepare.validate_receipt(out)
                (library / real_driver).write_bytes(b"changed actual compiler driver")
                with self.assertRaisesRegex(ValueError, "compiler changed"):
                    prepare.validate_receipt(out)

    def test_existing_output_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source(root)
            with self.assertRaisesRegex(ValueError, "must be new"):
                prepare.prepare(source, source, SDK, "latency")
            with self.assertRaisesRegex(ValueError, "disjoint"):
                prepare.prepare(source, source / "copy", SDK, "latency")
            private = root / "symlinked"
            private.mkdir()
            (private / "outside.rs").symlink_to(source / prepare.ARRAY)
            with self.assertRaisesRegex(ValueError, "escapes"):
                prepare.contained_file(private, Path("outside.rs"))


if __name__ == "__main__":
    unittest.main()
