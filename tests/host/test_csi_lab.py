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
ACK = b"PRESENCE_OPTIONS seconds=180 force=0 engine=espressif_full amplitude=linear snapshots=0 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap calibration=post_training_v2 numeric_failures=source_v1 other_inputs=frozen_enqueue record=csi_lab_v4 model_owner=control_v1"


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
                snapshots=False,
                trace=True,
                calibrated=True,
                diagnostic_pairs=False,
                numeric_failures=True,
                record_sink=recorded.append,
            )
            client.command("LABEL_WALKING")
        self.assertEqual(console.writes[0], b"PRESENCE_RECORD 180 linear\n")
        self.assertEqual(recorded, [b"CSI_LAB fixture-session\n"])
        self.assertEqual(console.writes[-1], b"LABEL_WALKING\n")

    def test_motion_case_requires_exact_ack_and_refuses_historical_calibration(self):
        ack = ACK.replace(
            b"csi_lab_v4 model_owner=control_v1",
            b"csi_lab_v6 model_owner=control_v1 motion=temporal_motion_v1",
        )
        old_motion = ack.replace(b"csi_lab_v6", b"csi_lab_v5")
        for actual in (ACK, old_motion, ack):
            client, console = self.client(actual)
            with patch.object(
                RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
            ):
                args = dict(
                    trace=True,
                    calibrated=True,
                    numeric_failures=True,
                    record_sink=lambda _: None,
                    motion_case="temporal-motion-v1",
                )
                if actual != ack:
                    with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                        client.start(
                            b"fixture-network",
                            b"fixture-password",
                            180,
                            "linear",
                            **args,
                        )
                    self.assertEqual(len(console.writes), 1)
                else:
                    client.start(
                        b"fixture-network", b"fixture-password", 180, "linear", **args
                    )
                    for command in (
                        "CALIBRATE_BACKGROUND",
                        "CALIBRATE_VALIDATE",
                        "CALIBRATE_ACCEPT",
                    ):
                        with self.assertRaises(ValueError):
                            client.command(command)
                    client.command("LABEL_EMPTY")
                    client.command("TRAIN_STOP")
                    writes = len(console.writes)
                    with self.assertRaisesRegex(ValueError, "cannot retrain"):
                        client.command("TRAIN_START")
                    self.assertEqual(len(console.writes), writes)
            self.assertEqual(console.writes[0], b"PRESENCE_MOTION 180 linear\n")

    def test_motion_case_without_recording_is_refused_before_console_access(self):
        for case in ("temporal-motion-v1", "undeclared"):
            client, console = self.client()
            with self.assertRaises(ValueError):
                client.start(
                    b"fixture-network",
                    b"fixture-password",
                    180,
                    "linear",
                    motion_case=case,
                )
            self.assertEqual(console.writes, [])

    def test_legacy_ack_withholds_credentials_for_record_case(self):
        client, console = self.client(
            ACK.replace(b" record=csi_lab_v4 model_owner=control_v1", b"")
        )
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                client.start(
                    b"fixture-network",
                    b"fixture-password",
                    180,
                    "linear",
                    snapshots=False,
                    trace=True,
                    calibrated=True,
                    diagnostic_pairs=False,
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

    def test_unpacked_v2_firmware_is_rejected_before_credentials(self):
        client, console = self.client(
            ACK.replace(b"csi_lab_v4 model_owner=control_v1", b"csi_lab_v2")
        )
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                client.start(
                    b"fixture-network",
                    b"fixture-password",
                    180,
                    "linear",
                    trace=True,
                    calibrated=True,
                    numeric_failures=True,
                    record_sink=lambda _: None,
                )
        self.assertEqual(len(console.writes), 1)

    def test_recording_requires_the_new_model_owner_before_credentials(self):
        for replacement in (
            b"csi_lab_v3",
            b"csi_lab_v4",
            b"csi_lab_v4 model_owner=control_v2",
        ):
            with self.subTest(ack=replacement):
                client, console = self.client(
                    ACK.replace(b"csi_lab_v4 model_owner=control_v1", replacement)
                )
                with patch.object(
                    RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
                ):
                    with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                        client.start(
                            b"fixture-network",
                            b"fixture-password",
                            180,
                            "linear",
                            trace=True,
                            calibrated=True,
                            numeric_failures=True,
                            record_sink=lambda _: None,
                        )
                self.assertEqual(console.writes, [b"PRESENCE_RECORD 180 linear\n"])

    def test_record_case_rejects_legacy_snapshot_modes_before_console_access(self):
        options = dict(
            snapshots=False,
            trace=True,
            calibrated=True,
            diagnostic_pairs=False,
            numeric_failures=True,
            record_sink=lambda _: None,
        )
        for change in (
            {"snapshots": True},
            {"diagnostic_pairs": True},
            {"trace": False},
            {"calibrated": False},
            {"numeric_failures": False},
        ):
            with self.subTest(change=change):
                client, console = self.client()
                with self.assertRaises(ValueError):
                    client.start(
                        b"fixture-network",
                        b"fixture-password",
                        180,
                        "linear",
                        **{**options, **change},
                    )
                self.assertEqual(console.writes, [])

    def test_record_case_withholds_credentials_for_old_combined_mode_ack(self):
        old_ack = ACK.replace(b"snapshots=0", b"snapshots=1").replace(
            b" record=csi_lab_v4 model_owner=control_v1",
            b" capture=calibration_pairs_v2 record=csi_lab_v1",
        )
        client, console = self.client(old_ack)
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                client.start(
                    b"fixture-network",
                    b"fixture-password",
                    180,
                    "linear",
                    snapshots=False,
                    trace=True,
                    calibrated=True,
                    diagnostic_pairs=False,
                    numeric_failures=True,
                    record_sink=lambda _: None,
                )
        self.assertEqual(console.writes, [b"PRESENCE_RECORD 180 linear\n"])

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


STACK = load("csi_lab_stack", "tests/csi-radio/presence/stack.py")


class ProducerStackTests(unittest.TestCase):
    def assembly(self, frame=128):
        names = list(STACK.REQUIRED) + [
            "csi_lab_core::recorder::Recorder::append_admitted"
        ]
        return "\n".join(
            f"{0x42000000+i*256:08x} <{name}>:\n  42000000: 004136 entry a1, {frame if name.endswith('floats') else 128}\n"
            for i, name in enumerate(names)
        )

    def test_actual_3248_byte_frame_from_failed_capture_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "floats"):
            STACK.qualify(self.assembly(3248))
        receipt = STACK.qualify(self.assembly())
        self.assertEqual(receipt["frame_limit_bytes"], 512)
        self.assertEqual(receipt["worker_stack_bytes"], 3072)

    def test_missing_or_unreadable_target_producer_cannot_qualify(self):
        with self.assertRaises(ValueError):
            STACK.qualify("")
        with self.assertRaises(ValueError):
            STACK.qualify(
                self.assembly().replace("entry a1, 128", "unreadable instruction")
            )
        with self.assertRaises(ValueError):
            STACK.qualify(
                self.assembly().replace(
                    "csi_lab_core::recorder::Lease::wander",
                    "unqualified_packed_producer",
                )
            )


class RecorderStatusTests(unittest.TestCase):
    def test_model_owner_reports_pending_source_work_explicitly(self):
        client = RUN.Client(
            Console(
                [
                    b"PRESENCE_MODEL_OWNER v=1 slots=64 received=103 applied=101 pending=2"
                ]
            )
        )
        client.record_sink = lambda _: None
        self.assertEqual(
            client.read(__import__("time").monotonic() + 1),
            dict(
                kind="model_owner_status",
                slots=64,
                received=103,
                applied=101,
                pending=2,
            ),
        )

    def test_model_owner_rejects_legacy_mode_and_invalid_accounting(self):
        good = b"PRESENCE_MODEL_OWNER v=1 slots=64 received=103 applied=101 pending=2"
        for line in (
            good.replace(b"v=1", b"v=2"),
            good.replace(b"slots=64", b"slots=65"),
            good + b" extra=1",
            good.replace(b"applied=101", b"applied=104"),
            good.replace(b"pending=2", b"pending=1"),
            good.replace(b"received=103", b"received=166").replace(
                b"pending=2", b"pending=65"
            ),
            good.replace(b"received=103", b"received=2147483648"),
            good.replace(b"received=103", b"received=-1"),
        ):
            with self.subTest(line=line):
                client = RUN.Client(Console([line]))
                client.record_sink = lambda _: None
                with self.assertRaises(ValueError):
                    client.read(__import__("time").monotonic() + 1)
        with self.assertRaises(ValueError):
            RUN.Client(Console([good])).read(__import__("time").monotonic() + 1)

    def test_live_status_keeps_loss_and_unobserved_stack_values_explicit(self):
        console = Console(
            [
                b"PRESENCE_LAB_STATUS v=1 losses=2 slots=128 rf_stack=2048 gain_stack=4294967295 compute_stack=800"
            ]
        )
        client = RUN.Client(console)
        client.record_sink = lambda _: None
        value = client.read(__import__("time").monotonic() + 1)
        self.assertEqual(value["losses"], 2)
        self.assertIsNone(value["gain_stack"])
        self.assertEqual(value["compute_stack"], 800)

    def test_status_requires_opt_in_exact_schema_and_bounded_metrics(self):
        good = b"PRESENCE_LAB_STATUS v=1 losses=0 slots=128 rf_stack=2048 gain_stack=1000 compute_stack=800"
        for line in (
            good.replace(b"v=1", b"v=2"),
            good + b" extra=1",
            good.replace(b"compute_stack=800", b"compute_stack=999999"),
            good.replace(b"losses=0", b"losses=-1"),
        ):
            client = RUN.Client(Console([line]))
            client.record_sink = lambda _: None
            with self.assertRaises(ValueError):
                client.read(__import__("time").monotonic() + 1)
        with self.assertRaises(ValueError):
            RUN.Client(Console([good])).read(__import__("time").monotonic() + 1)
