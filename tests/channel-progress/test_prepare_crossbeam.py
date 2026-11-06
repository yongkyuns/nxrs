"""Offline source-isolation, identity and hook-placement negative controls."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("progress_private_crossbeam", HERE / "prepare-crossbeam.py")
prep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prep)

# Frozen before adding fifo-ordered, using the published 0bc150ce recipe.
# The optional real-archive test authenticates every byte before comparison;
# this pins the whole legacy receipt, not only the visible hook locations.
LEGACY_AUTHENTIC_RECEIPTS = {
    "clean": "72867ae12ba8c32f6b792e0e396016261d87024aba0557fd104ac638a750066f",
    "send-reserved": "e1af9444e0c8a23e22309ac553502ad9c81d943223ad9eb7b14e837a9a6a1f8b",
    "send-published": "d50dd81375f035bf6d2447cdf6d1a7c9b75ddcfbfbc56054e9e7b3a8d4e1b475",
    "recv-reserved": "6dd5ad7b8ac3deaf228160ba67f368df03dc5aea6bf9bc527e8335d08886d63d",
    "recv-reclaimed": "a5c7c820317dc4e93a18171c612e32fc7148429d34c71fec576e20833f0c2ccf",
}


def synthetic_array() -> bytes:
    # Unit fixtures exercise anchor and refusal logic. Actual distribution bytes
    # are authenticated separately by the build's fixed archive checksums.
    return (prep.ARRAY_IMPORT + "fn send_reserve() {\n" + prep.RESERVE_SEND
            + "} }\nfn send_publish() {\n" + prep.PUBLISH_SEND
            + "}\nfn recv_reserve() {\n" + prep.RESERVE_RECV
            + "} }\nfn recv_reclaim() {\n" + prep.RECLAIM_RECV + "}\n").encode()


def synthetic_select() -> bytes:
    return (prep.SELECT_IMPORT + "\nfn run_select(\n) {\n"
            "registered_count += 1;\n" + prep.SELECT_WAIT + "}\n"
            "fn run_ready(\n) {\n" + prep.SELECT_WAIT + "}\n").encode()


def archive(name: str, files: dict[str, bytes], *, extra=None) -> bytes:
    content = io.BytesIO()
    prefix = f"{name}-{prep.PINS[name]['version']}"
    with tarfile.open(fileobj=content, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(f"{prefix}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if extra is not None:
            tar.addfile(extra, io.BytesIO(b"x") if extra.size else None)
    return content.getvalue()


class HookTransformTests(unittest.TestCase):
    def test_clean_is_byte_identical(self):
        self.assertEqual(prep.transform_array(synthetic_array(), "clean"), synthetic_array())
        self.assertEqual(prep.transform_select(synthetic_select(), "clean"), synthetic_select())

    def test_each_stage_has_one_correct_boundary(self):
        for mode, stage in prep.MODES.items():
            if stage not in (1, 2, 3, 4):
                continue
            with self.subTest(mode=mode):
                text = prep.transform_array(synthetic_array(), mode).decode()
                invocation = f"{prep.HOOK}({stage});"
                self.assertEqual(text.count(invocation), 1)
                self.assertIn('#[cfg(target_os = "nuttx")]', text)
                if stage in (1, 3):
                    operation = "write" if stage == 1 else "read"
                    self.assertLess(text.index(invocation), text.index(f"call to `{operation}`"))
                else:
                    operation = "receiver" if stage == 2 else "sender"
                    position = text.index(invocation)
                    self.assertLess(text.rfind("slot.stamp.store", 0, position), position)
                    self.assertLess(position, text.index(f"Wake a sleeping {operation}"))

    def test_legacy_transform_bytes_unchanged(self):
        array_hashes = {
            "clean": "dd02f035223a37046113c734be45d451072e60f0d0346cca0a9a00e3a04249f7",
            "send-reserved": "6a3987424026b17a055e0ddff224c7bb483c46fe8506418397239b75d1a16414",
            "send-published": "645f6d84dce116c643f4074171615e20411729ad0a29a33940297a0a543d0ef8",
            "recv-reserved": "5b3eff71278259e632f5e5d95780f5411d2dbd779dfcc864e33318d8fd4b8276",
            "recv-reclaimed": "b5984622db722e7db629083bd43e4ccf00dff5e118a2c25decaaf669489152ce",
        }
        for mode, expected in array_hashes.items():
            with self.subTest(mode=mode):
                self.assertEqual(prep.digest(prep.transform_array(synthetic_array(), mode)), expected)
                selected = "ab98c735d97a30561ad370c801180a646235f76217a06c0f0eaf06ef69d30b2e" \
                    if mode == "clean" else "24eec257e37b39772464764707c8d0992db0c67338bbb8098714d9a37be2ab96"
                self.assertEqual(prep.digest(prep.transform_select(synthetic_select(), mode)), selected)

    def test_fifo_records_all_four_actual_slot_boundaries(self):
        text = prep.transform_array(synthetic_array(), "fifo-ordered").decode()
        markers = [f"{prep.HOOK}(0x{stage:x});" for stage in range(0x301, 0x305)]
        self.assertTrue(all(text.count(marker) == 1 for marker in markers))
        positions = [text.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[0], text.index("call to `write`"))
        self.assertLess(positions[2], text.index("call to `read`"))
        for position, role in ((positions[1], "receiver"), (positions[3], "sender")):
            store = text.rfind("slot.stamp.store", 0, position)
            self.assertGreaterEqual(store, 0)
            self.assertLess(store, position)
            self.assertLess(position, text.index(f"Wake a sleeping {role}"))
        self.assertEqual(text.count(f"fn {prep.HOOK}(stage: u32);"), 1)
        self.assertEqual(text.count('#[cfg(target_os = "nuttx")]'), 5)

    def test_fifo_wait_return_marker_is_after_wait_only_in_run_select(self):
        text = prep.transform_select(synthetic_select(), "fifo-ordered").decode()
        before = f"{prep.HOOK}(0x100 + registered_count as u32);"
        after = f"{prep.HOOK}(0x200);"
        self.assertEqual(text.count(before), 1)
        self.assertEqual(text.count(after), 1)
        self.assertLess(text.index(before), text.index("sel = cx.wait_until(deadline)"))
        self.assertLess(text.index("sel = cx.wait_until(deadline)"), text.index(after))
        self.assertLess(text.index(after), text.index("fn run_ready("))
        self.assertNotIn(after, text[text.index("fn run_ready("):])

    def test_marker_uses_actual_registration_count_only_in_run_select(self):
        text = prep.transform_select(synthetic_select(), "send-reserved").decode()
        invocation = f"{prep.HOOK}(0x100 + registered_count as u32);"
        self.assertEqual(text.count(invocation), 1)
        self.assertLess(text.index("registered_count += 1;"), text.index(invocation))
        self.assertLess(text.index(invocation), text.index("sel = cx.wait_until(deadline)"))
        self.assertNotIn(invocation, text[text.index("fn run_ready("):])

    def test_refuses_missing_duplicate_reordered_or_preinstrumented_anchors(self):
        original = synthetic_array().decode()
        invalid = [original.replace(prep.RESERVE_SEND, "", 1),
                   original + prep.RESERVE_SEND,
                   original.replace(prep.RESERVE_SEND, prep.RESERVE_RECV, 1).replace(
                       prep.RESERVE_RECV, prep.RESERVE_SEND, 1),
                   original + prep.HOOK]
        # Explicitly swap the two complete operation anchors.
        invalid[2] = original.replace(prep.RESERVE_SEND, "SWAP").replace(
            prep.RESERVE_RECV, prep.RESERVE_SEND).replace("SWAP", prep.RESERVE_RECV)
        for text in invalid:
            for mode in ("send-reserved", "fifo-ordered"):
                with self.subTest(text=text, mode=mode):
                    with self.assertRaises(ValueError):
                        prep.transform_array(text.encode(), mode)
        for text in (synthetic_select().replace(prep.SELECT_WAIT.encode(), b""),
                     synthetic_select() + b"\nfn run_select(\n",
                     synthetic_select() + prep.HOOK.encode()):
            for mode in ("send-reserved", "fifo-ordered"):
                with self.assertRaises(ValueError):
                    prep.transform_select(text, mode)

    def test_refuses_unknown_mode(self):
        with self.assertRaises(ValueError):
            prep.transform_array(synthetic_array(), "latency")
        with self.assertRaises(ValueError):
            prep.transform_select(synthetic_select(), "latency")

    def test_real_lock_identity_is_pinned(self):
        prep.verify_lock()

    @unittest.skipUnless(os.environ.get("CHANNEL_PROGRESS_AUTHENTIC_ARCHIVES"),
                         "set CHANNEL_PROGRESS_AUTHENTIC_ARCHIVES to retained original crate directory")
    def test_legacy_receipts_match_authenticated_published_sources(self):
        root = Path(os.environ["CHANNEL_PROGRESS_AUTHENTIC_ARCHIVES"])
        archives = {name: (root / f"{name}-{pin['version']}.crate").read_bytes()
                    for name, pin in prep.PINS.items()}
        for mode, expected in LEGACY_AUTHENTIC_RECEIPTS.items():
            with self.subTest(mode=mode):
                record = prep.expected_receipt(Path("/isolated/crossbeam"), mode, archives)
                self.assertEqual(prep.digest((json.dumps(record, indent=2) + "\n").encode()), expected)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = {}
        self.files = {}
        for name, pin in prep.PINS.items():
            files = {"Cargo.toml": (f'[package]\nname="{name}"\nversion="{pin["version"]}"\n').encode(),
                     "src/lib.rs": b"// complete synthetic archive fixture\n"}
            if name == "crossbeam-channel":
                files.update({prep.ARRAY: synthetic_array(), prep.SELECT: synthetic_select()})
            else:
                files["src/backoff.rs"] = b"// unchanged backoff source\n"
            self.files[name] = files
            path = self.root / f"{name}.crate"
            path.write_bytes(archive(name, files))
            self.inputs[name] = path
        pins = copy.deepcopy(prep.PINS)
        for name, pin in pins.items():
            pin["sha256"] = prep.digest(self.inputs[name].read_bytes())
        self.pin_mock = mock.patch.object(prep, "PINS", pins)
        self.pin_mock.start()
        self.addCleanup(self.pin_mock.stop)
        self.out = self.root / "prepared"

    def prepare(self, mode="send-reserved"):
        return prep.prepare(self.inputs["crossbeam-channel"], self.inputs["crossbeam-utils"],
                            self.out, mode)

    def source(self, name="crossbeam-channel", relative=prep.ARRAY):
        return self.out / "sources" / f"{name}-{prep.PINS[name]['version']}" / relative

    def test_prepare_all_modes_without_modifying_input(self):
        before = {name: path.read_bytes() for name, path in self.inputs.items()}
        for mode, stage in prep.MODES.items():
            self.out = self.root / mode
            record = self.prepare(mode)
            self.assertEqual(record["hook_stage"], stage)
            self.assertEqual(prep.validate_receipt(self.out, mode), record)
            self.assertEqual(self.source("crossbeam-utils", "src/backoff.rs").read_bytes(),
                             self.files["crossbeam-utils"]["src/backoff.rs"])
            self.assertEqual(record["instrumented"], stage != 0)
            if mode == "fifo-ordered":
                self.assertEqual(record["fifo_order_hooks"]["markers"], prep.FIFO_MARKERS)
            else:
                self.assertNotIn("fifo_order_hooks", record)
            if stage == 0:
                self.assertEqual(record["crates"]["crossbeam-channel"]["original_files"],
                                 record["crates"]["crossbeam-channel"]["selected_files"])
            else:
                self.assertEqual(set(record["patches_sha256"]),
                                 {"array-witness.patch", "select-witness.patch"})
        self.assertEqual(before, {name: path.read_bytes() for name, path in self.inputs.items()})

    def test_existing_output_and_wrong_mode_refused(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "must be new"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "mode mismatch"):
            prep.validate_receipt(self.out, "clean")

    def test_archive_drift_rejected_before_any_output_is_created(self):
        self.inputs["crossbeam-utils"].write_bytes(b"different archive")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_wrong_archive_manifest_rejected(self):
        files = dict(self.files["crossbeam-channel"])
        files["Cargo.toml"] = b'[package]\nname="fake"\nversion="0.5.17"\n'
        data = archive("crossbeam-channel", files)
        prep.PINS["crossbeam-channel"]["sha256"] = prep.digest(data)
        with self.assertRaisesRegex(ValueError, "manifest"):
            prep.archive_files(data, "crossbeam-channel")

    def test_unsafe_archive_members_rejected(self):
        prefix = "crossbeam-channel-0.5.17/"
        for name, kind in ((prefix + "../escape", tarfile.REGTYPE),
                           ("/absolute", tarfile.REGTYPE),
                           (prefix + "link", tarfile.SYMTYPE),
                           (prefix + "hardlink", tarfile.LNKTYPE),
                           (prefix + "Cargo.toml", tarfile.REGTYPE)):
            with self.subTest(name=name, kind=kind):
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.linkname = "elsewhere"
                entry.size = 1 if kind == tarfile.REGTYPE else 0
                data = archive("crossbeam-channel", self.files["crossbeam-channel"], extra=entry)
                prep.PINS["crossbeam-channel"]["sha256"] = prep.digest(data)
                with self.assertRaises(ValueError):
                    prep.archive_files(data, "crossbeam-channel")

    def test_modified_array_or_other_source_refused_even_with_forged_inventory(self):
        self.prepare()
        target = self.source(relative="src/lib.rs")
        target.write_bytes(b"// unreviewed change\n")
        receipt_path = self.out / "crossbeam-hook.json"
        record = json.loads(receipt_path.read_text())
        crate = record["crates"]["crossbeam-channel"]
        crate["selected_files"]["src/lib.rs"] = prep.digest(target.read_bytes())
        crate["selected_tree_sha256"] = prep.inventory_digest(crate["selected_files"])
        receipt_path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "authenticated reconstruction"):
            prep.validate_receipt(self.out)

    def test_modified_selected_bytes_rejected(self):
        self.prepare()
        self.source().write_bytes(b"// changed array\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            prep.validate_receipt(self.out)

    def test_extra_source_file_refused(self):
        self.prepare()
        self.source(relative="src/additional.rs").write_text("// extra source\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            prep.validate_receipt(self.out)

    def test_source_symlink_and_hardlink_refused(self):
        self.prepare()
        target = self.source(relative="src/lib.rs")
        original = target.read_bytes()
        external = self.root / "external.rs"
        external.write_bytes(original)
        target.unlink()
        target.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "symlink"):
            prep.validate_receipt(self.out)
        target.unlink()
        target.hardlink_to(external)
        with self.assertRaisesRegex(ValueError, "independent copy"):
            prep.validate_receipt(self.out)

    def test_changed_config_or_patch_refused(self):
        self.prepare()
        config = self.out / "cargo-config.toml"
        original = config.read_bytes()
        config.write_text('[patch.crates-io]\n')
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            prep.validate_receipt(self.out)
        config.write_bytes(original)
        (self.out / "array-witness.patch").write_text("different patch\n")
        with self.assertRaisesRegex(ValueError, "patch changed"):
            prep.validate_receipt(self.out)

    def test_retained_original_archive_drift_refused(self):
        self.prepare()
        next((self.out / "original").glob("*.crate")).write_bytes(b"changed original")
        with self.assertRaisesRegex(ValueError, "checksum"):
            prep.validate_receipt(self.out)

    def test_fifo_marker_receipt_cannot_be_resealed_or_downgraded(self):
        self.prepare("fifo-ordered")
        path = self.out / "crossbeam-hook.json"
        record = json.loads(path.read_text())
        record["fifo_order_hooks"]["markers"]["send_published"] = 0x303
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "authenticated reconstruction"):
            prep.validate_receipt(self.out, "fifo-ordered")
        record["mode"] = "send-reserved"
        record["hook_stage"] = 1
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "mode mismatch"):
            prep.validate_receipt(self.out, "fifo-ordered")


if __name__ == "__main__":
    unittest.main()
