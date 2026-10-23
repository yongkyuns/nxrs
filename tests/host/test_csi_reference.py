"""Static reference evidence must never promote input identity to RF qualification."""

import copy
import hashlib
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "csi_reference", ROOT / "tests/csi-radio/reference.py"
)
REFERENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REFERENCE)


def write_archive(path, entries):
    """Minimal standard ar fixture; native macOS ar creation drops non-Mach-O data."""
    data = bytearray(b"!<arch>\n")
    for name, payload in entries:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(payload):<10}`\n"
        data.extend(header.encode("ascii"))
        data.extend(payload)
        if len(payload) % 2:
            data.extend(b"\n")
    path.write_bytes(data)


@unittest.skipUnless(shutil.which("ar"), "archive tool required")
class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.ref = self.root / "reference"
        self.sdk = self.root / "sdk"
        self.ref.mkdir()
        self.sdk.mkdir()
        self.ar = shutil.which("ar")
        for directory in (self.ref, self.sdk):
            write_archive(
                directory / "libtest.a",
                [("receive.o", b"same controlled object bytes")],
            )
        self.pins = {
            "measured_image_sha256": "measured-image",
            "archives": {
                "libtest.a": {
                    "sha256": REFERENCE.digest((self.ref / "libtest.a").read_bytes()),
                    "sdk_path": "libtest.a",
                    "focus_members": ["receive.o"],
                }
            },
        }
        self.evidence = {
            "latest_build": {
                "image_sha256": "measured-image",
                "radio_archives_sha256": {
                    "libtest.a": REFERENCE.digest((self.sdk / "libtest.a").read_bytes())
                },
            }
        }

    def compare(self):
        return REFERENCE.compare(self.ref, self.sdk, self.ar, self.pins, self.evidence)

    def test_even_identical_receive_objects_cannot_qualify_gain_or_presence(self):
        report = self.compare()
        self.assertTrue(
            report["archives"]["libtest.a"]["focus_members"]["receive.o"][
                "byte_identical"
            ]
        )
        for flag in (
            "private_gain_metadata_qualified",
            "presence_detection_qualified",
            "reference_firmware_built",
            "gain_forcing_executed",
            "hardware_accessed",
        ):
            self.assertIs(report[flag], False)

    def test_substituting_either_reference_or_measured_archive_fails_before_tools(self):
        for directory in (self.ref, self.sdk):
            with self.subTest(directory=directory.name):
                archive = directory / "libtest.a"
                original = archive.read_bytes()
                archive.write_bytes(original + b"different build")
                with patch.object(REFERENCE, "members") as reader:
                    with self.assertRaisesRegex(ValueError, "SHA256"):
                        self.compare()
                    reader.assert_not_called()
                archive.write_bytes(original)

    def test_omitting_an_archive_or_selecting_another_image_fails(self):
        for change in ("archive", "image"):
            with self.subTest(change=change):
                evidence = copy.deepcopy(self.evidence)
                if change == "archive":
                    evidence["latest_build"]["radio_archives_sha256"][
                        "extra.a"
                    ] = "unknown"
                else:
                    evidence["latest_build"]["image_sha256"] = "other-image"
                with self.assertRaises(ValueError):
                    REFERENCE.compare(self.ref, self.sdk, self.ar, self.pins, evidence)

    def test_missing_required_receive_object_fails(self):
        self.pins["archives"]["libtest.a"]["focus_members"] = ["missing.o"]
        with self.assertRaisesRegex(ValueError, "member missing"):
            self.compare()

    def test_duplicate_member_names_are_ambiguous(self):
        write_archive(
            self.ref / "libtest.a",
            [("receive.o", b"first"), ("receive.o", b"second")],
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            REFERENCE.members(self.ar, self.ref / "libtest.a")

    def test_rejected_download_cannot_create_an_archive(self):
        directory = self.root / "download"
        pins = {
            "archives": {
                "libtest.a": {
                    "repository": "esp32-wifi-lib",
                    "revision": "0" * 40,
                    "sha256": hashlib.sha256(b"expected").hexdigest(),
                }
            }
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"substituted"
        with patch.object(REFERENCE.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                REFERENCE.fetch(directory, pins)
        self.assertFalse((directory / "libtest.a").exists())


if __name__ == "__main__":
    unittest.main()
