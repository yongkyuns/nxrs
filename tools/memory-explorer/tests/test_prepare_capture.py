"""A session is portable, byte-bound, exclusive, and honest about capture identity."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "prepare_capture.py"
SPEC = importlib.util.spec_from_file_location("prepare_capture", SCRIPT)
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


def executable():
    """A complete ELF64 ET_EXEC with one mapped instruction and section names."""
    names = b"\0.text\0.shstrtab\0"
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    header = ident + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0x400078, 64, 144,
                                 0, 64, 56, 1, 64, 3, 2)
    segment = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, 121, 121, 4096)
    body = header + segment + b"\xc3" + names
    body += b"\0" * (144 - len(body))
    null = bytes(64)
    text = struct.pack("<IIQQQQIIQQ", 1, 1, 6, 0x400078, 120, 1, 0, 0, 1, 0)
    strings = struct.pack("<IIQQQQIIQQ", 7, 3, 0, 0, 121, len(names), 0, 0, 1, 0)
    return body + null + text + strings


class PrepareCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.elf = self.root / "selected.elf"
        self.elf.write_bytes(executable())
        self.identity = hashlib.sha256(self.elf.read_bytes()).hexdigest()
        self.console = self.root / "uart.log"
        self.console.write_text("worker-7 [0] 1.000000010: tracing_mark_write: I|7|imu/bmi270/wake_timer\n")
        self.output = self.root / "session"

    def prepare(self, **kwargs):
        return PREPARE.prepare(elf=self.elf, console=self.console, output=self.output, **kwargs)

    def test_portable_manifest_preserves_exact_sources_and_all_content_digests(self):
        snapshot = self.root / "memory.json"
        snapshot.write_text(json.dumps({"schema_version": 1, "build": {"elf_sha256": self.identity}, "snapshots": []}))
        manifest_path = self.prepare(snapshots=[snapshot])
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["build"]["elf_sha256"], self.identity)
        self.assertEqual(manifest["snapshots"], ["snapshot-0001.json"])
        self.assertEqual((self.output / "firmware.elf").read_bytes(), self.elf.read_bytes())
        self.assertEqual((self.output / "console.txt").read_bytes(), self.console.read_bytes())
        self.assertEqual((self.output / "snapshot-0001.json").read_bytes(), snapshot.read_bytes())
        for name, record in manifest["files"].items():
            self.assertEqual(Path(name).name, name)
            self.assertFalse((self.output / name).is_symlink())
            data = (self.output / name).read_bytes()
            self.assertEqual(record, {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertNotIn("layout", manifest)  # Addresses do not identify a board.
        trace = json.loads((self.output / manifest["trace"]).read_text())
        self.assertEqual(trace["otherData"]["nxrs"]["elf_sha256"], self.identity)
        self.assertTrue(any("producer assertion" in item for item in trace["otherData"]["nxrs"]["warnings"]))
        self.assertEqual(list(self.root.glob(".session-preparing-*")), [])

    def test_mismatched_snapshot_and_capture_identities_leave_no_session(self):
        for identity in ["a" * 64, "not-a-hash", None]:
            snapshot = self.root / "wrong.json"
            snapshot.write_text(json.dumps({"schema_version": 1, "build": {"elf_sha256": identity}, "snapshots": []}))
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "ELF SHA-256"):
                self.prepare(snapshots=[snapshot])
            self.assertFalse(self.output.exists())
            self.assertEqual(list(self.root.glob(".session-preparing-*")), [])
        snapshot.write_text(json.dumps({"schema_version": 1, "build": {"elf_sha256": self.identity},
                                        "capture": {"elf_sha256": "b" * 64}, "snapshots": []}))
        with self.assertRaisesRegex(ValueError, "capture"):
            self.prepare(snapshots=[snapshot])

    def test_console_cannot_override_an_explicit_conflicting_firmware_hash(self):
        self.console.write_text(
            "NXRS_NOTE_BEGIN schema=1 source=fixture clock=fixture mode=0 notes_lost=0 elf_sha256=" + "a" * 64 + "\n"
            "worker-7 [0] 1.000000010: tracing_mark_write: I|7|ready\n"
            "NXRS_NOTE_END schema=1 mode=0 notes_lost=0 complete=1 elf_sha256=" + "a" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "console begin frame"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_existing_output_and_publication_races_preserve_existing_directory(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            self.prepare()
        staging = self.root / "staging"
        staging.mkdir()
        (staging / "new").write_text("new")
        with self.assertRaises(OSError):
            PREPARE._publish(staging, self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual((staging / "new").read_text(), "new")

    def test_input_symlink_is_rejected_without_touching_its_target(self):
        link = self.root / "linked.log"
        try:
            link.symlink_to(self.console)
        except OSError:
            self.skipTest("symlinks unavailable on this host")
        original = self.console.read_bytes()
        with self.assertRaisesRegex(ValueError, "regular file"):
            PREPARE.prepare(elf=self.elf, console=link, output=self.output)
        self.assertEqual(self.console.read_bytes(), original)
        self.assertFalse(self.output.exists())

    def test_input_and_total_byte_bounds_fail_before_publication(self):
        with mock.patch.dict(PREPARE.LIMITS, {"console": 8}):
            with self.assertRaisesRegex(ValueError, "1..8 bytes"):
                self.prepare()
        self.assertFalse(self.output.exists())
        with mock.patch.object(PREPARE, "MAX_SESSION_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "combined input/output"):
                self.prepare()
        self.assertFalse(self.output.exists())

    def test_duplicate_identity_fields_are_rejected(self):
        snapshot = self.root / "duplicate.json"
        snapshot.write_text('{"schema_version":1,"build":{"elf_sha256":"' + "a" * 64 + '","elf_sha256":"' + self.identity + '"},"snapshots":[]}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON field"):
            self.prepare(snapshots=[snapshot])
        self.assertFalse(self.output.exists())

    def test_board_layout_requires_explicit_choice_and_is_copied_exactly(self):
        manifest = json.loads(self.prepare(board="mps2-an521-qemu").read_text())
        self.assertEqual((self.output / manifest["layout"]).read_bytes(),
                         (SCRIPT.parent / "layouts/mps2-an521-qemu.json").read_bytes())
        self.output = self.root / "different-session"
        with self.assertRaisesRegex(ValueError, "either --board or --layout"):
            self.prepare(board="mps2-an521-qemu", layout=self.elf)

    def test_automatic_companions_are_bound_and_carried_with_the_recording(self):
        map_path = self.elf.with_suffix(".map")
        map_path.write_text("Memory Configuration\nFLASH 0x00400000 0x10000 xr\n")
        layout_path = self.elf.with_name("layout.json")
        layout_path.write_text(json.dumps({"schema_version": 1, "build": {"elf_sha256": self.identity}, "regions": []}))
        manifest = json.loads(self.prepare().read_text())
        self.assertTrue(manifest["automatic_layout"])
        self.assertEqual((self.output / manifest["map"]).read_bytes(), map_path.read_bytes())
        self.assertEqual((self.output / manifest["layout"]).read_bytes(), layout_path.read_bytes())
        self.assertIn(manifest["layout"], manifest["files"])
        self.assertTrue(any("Automatically selected" in item for item in manifest["discovery"]["warnings"]))

    def test_auto_board_specialization_retains_configured_capacity_in_portable_session(self):
        config = ("CONFIG_ARCH_BOARD_ESP32S3_M5_CORES3=y\nCONFIG_ARCH_CHIP_ESP32S3=y\n"
                  "CONFIG_ESP32S3_SPIRAM=y\nCONFIG_ESP32S3_SPIRAM_SIZE=8388608\nCONFIG_ESP32S3_FLASH_16M=y\n")
        for name, newline in (("lf", "\n"), ("crlf", "\r\n")):
            with self.subTest(line_endings=name):
                config_bytes = config.replace("\n", newline).encode("utf-8")
                # Hash exactly the bytes retained by the build, independently
                # of the host's text-mode newline translation.
                (self.root / "resolved.config").write_bytes(config_bytes)
                (self.root / "provenance.txt").write_text(
                    "board=esp32s3-m5-cores3:nsh\nnuttx=2b5c0de5e933281c209f954a63b99cf10c132346\n")
                (self.root / "binaries.sha256").write_text(
                    f"{self.identity}  /old/build/selected.elf\n"
                    f"{hashlib.sha256(config_bytes).hexdigest()}  /old/build/resolved.config\n")
                self.output = self.root / f"session-{name}"
                manifest = json.loads(self.prepare().read_text())
                self.assertTrue(manifest["automatic_layout"])
                layout = json.loads((self.output / manifest["layout"]).read_text())
                self.assertEqual(layout["build"]["elf_sha256"], self.identity)
                self.assertEqual(next(resource["size"] for resource in layout["physical_resources"]
                                      if resource["id"] == "configured-psram"), 8388608)
                data = (self.output / manifest["layout"]).read_bytes()
                self.assertEqual(manifest["files"][manifest["layout"]],
                                 {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})

    def test_heap_journal_must_name_the_same_elf(self):
        journal = self.root / "journal.json"
        journal.write_text(json.dumps({"schema_version": 1, "build": {"elf_sha256": "c" * 64}, "records": []}))
        with self.assertRaisesRegex(ValueError, "ELF SHA-256"):
            self.prepare(heap_journal=journal)
        self.assertFalse(self.output.exists())

    def test_invalid_console_does_not_publish_or_leave_staging_data(self):
        self.console.write_text("not a trace\n")
        with self.assertRaisesRegex(ValueError, "no timestamped"):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".session-preparing-*")), [])


if __name__ == "__main__":
    unittest.main()
