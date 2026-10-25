"""Software-only capture gates and pinned numerical replay protocols."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RUN = load("csi_lab_protocol", "tests/csi-radio/presence/run.py")
NATIVE = load("csi_lab_native", "tests/csi-lab/native_replay.py")
ACK = b"PRESENCE_OPTIONS seconds=180 force=0 engine=espressif_full amplitude=linear snapshots=1 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap calibration=post_training_v2 capture=calibration_pairs_v2 numeric_failures=source_v1 other_inputs=frozen_enqueue record=csi_lab_v1"


class Console:
    def __init__(self, lines):
        self.data = bytearray(b"\n".join(lines) + b"\n")
        self.writes = []

    def read(self, n):
        value = self.data[:n]
        del self.data[:n]
        return bytes(value)

    def write(self, value):
        self.writes.append(value)
        return len(value)

    def flush(self):
        pass


class LabTests(unittest.TestCase):
    def client(self, ack=ACK):
        console = Console(
            [
                b"PRESENCE_COMMAND_READY force=0",
                ack,
                b"PRESENCE_CREDENTIALS_READY",
                b"CSI_LAB fixture-session",
                b"PRESENCE_BEGIN seconds=180 channel=6 calibration=required rf_freshness=not_exported",
            ]
        )
        return RUN.Client(console), console

    def test_record_case_has_exact_ack_and_streams_session_before_begin(self):
        client, console = self.client()
        recorded = []
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            client.start(
                b"fixture-network",
                b"fixture-password",
                180,
                "linear",
                snapshots=True,
                trace=True,
                calibrated=True,
                diagnostic_pairs=True,
                numeric_failures=True,
                record_sink=recorded.append,
            )
            client.command("LABEL_WALKING")
        self.assertEqual(console.writes[0], b"PRESENCE_RECORD 180 linear\n")
        self.assertEqual(recorded, [b"CSI_LAB fixture-session\n"])
        self.assertEqual(console.writes[-1], b"LABEL_WALKING\n")

    def test_legacy_ack_withholds_credentials_for_record_case(self):
        client, console = self.client(ACK.replace(b" record=csi_lab_v1", b""))
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                client.start(
                    b"fixture-network",
                    b"fixture-password",
                    180,
                    "linear",
                    snapshots=True,
                    trace=True,
                    calibrated=True,
                    diagnostic_pairs=True,
                    numeric_failures=True,
                    record_sink=lambda _: None,
                )
        self.assertEqual(len(console.writes), 1)

    def test_record_sink_is_explicit_and_labels_are_not_admitted_in_legacy_mode(self):
        client = RUN.Client(Console([]))
        with self.assertRaises(ValueError):
            client.start(
                b"fixture-network", b"fixture-password", 180, record_sink=lambda _: None
            )
        with self.assertRaises(ValueError):
            client.command("LABEL_EMPTY")

    def test_native_output_requires_exact_status_identity_and_bits(self):
        call = dict(id=1, status=0, output_bits=[0])
        NATIVE.verify("CSI_LAB_KERNEL_RESULT id=1 status=0 words=1 bits=00000000", call)
        for line in (
            "CSI_LAB_KERNEL_RESULT id=2 status=0 words=1 bits=00000000",
            "CSI_LAB_KERNEL_RESULT id=1 status=-1 words=1 bits=00000000",
            "CSI_LAB_KERNEL_RESULT id=1 status=0 words=1 bits=00000001",
        ):
            with self.assertRaises(ValueError):
                NATIVE.verify(line, call)

    def test_native_export_rejects_shapes_and_malformed_bit_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calls.jsonl"
            good = dict(
                kind="wander",
                id=1,
                cols=4,
                rows0=0,
                rows1=0,
                input0_bits=[1, 2, 3, 4],
                input1_bits=[1, 2, 3, 4],
                status=0,
                output_bits=[0],
            )
            for change in (
                {},
                {"cols": 257},
                {"input1_bits": [0]},
                {"output_bits": [-1]},
                {"id": True},
            ):
                path.write_text(
                    json.dumps({"csi_lab_kernels": 1})
                    + "\n"
                    + json.dumps({**good, **change})
                    + "\n"
                )
                if change:
                    with self.assertRaises(ValueError):
                        list(NATIVE.calls(path))
                else:
                    self.assertEqual(len(list(NATIVE.calls(path))), 1)


MANIFEST = load("csi_lab_manifest", "tests/csi-lab/manifest.py")


class ManifestTests(unittest.TestCase):
    def test_source_and_image_provenance_is_private_and_exclusive(self):
        import hashlib
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "checkout").resolve()
            root.mkdir()
            build = Path(tmp) / "build"
            build.mkdir()
            source = root / "source.rs"
            source.write_text("fixture")
            image = build / "reference-flash.bin"
            image.write_bytes(b"synthetic build")
            sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            receipt = dict(
                continuous_recording={"wire_version": 1},
                image_sha256=sha(image),
                source_sha256={"source.rs": sha(source)},
                idf_commit="0" * 40,
                config_sha256="1" * 64,
                radio_archives_sha256={},
                gain_archive_sha256="2" * 64,
                motion_archive_sha256="3" * 64,
            )
            (build / "reference-build.json").write_text(json.dumps(receipt))
            output = Path(tmp) / "manifest.json"
            with patch.object(MANIFEST, "ROOT", root), patch.object(
                MANIFEST.subprocess, "check_output", return_value="4" * 40 + "\n"
            ):
                MANIFEST.create(build, output)
                with self.assertRaises(FileExistsError):
                    MANIFEST.create(build, output)
                with self.assertRaisesRegex(ValueError, "outside checkout"):
                    MANIFEST.create(build, root / "private.json")
            result = json.loads(output.read_text())
            self.assertFalse(result["physical_flash_identity_verified"])
            self.assertEqual(result["source_files_sha256"], receipt["source_sha256"])
            if os.name == "posix":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_changed_sources_or_image_cannot_create_provenance(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source.rs"
            source.write_text("changed")
            image = root / "reference-flash.bin"
            image.write_bytes(b"image")
            receipt = dict(
                continuous_recording={"wire_version": 1},
                image_sha256=hashlib.sha256(b"image").hexdigest(),
                source_sha256={"source.rs": "0" * 64},
            )
            (root / "reference-build.json").write_text(json.dumps(receipt))
            with patch.object(MANIFEST, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "source differs"):
                    MANIFEST.create(root, root / "out.json")
                image.write_bytes(b"changed image")
                with self.assertRaisesRegex(ValueError, "image differs"):
                    MANIFEST.create(root, root / "out.json")
            self.assertFalse((root / "out.json").exists())
