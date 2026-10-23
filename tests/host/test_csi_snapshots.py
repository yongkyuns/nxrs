"""Private snapshot protocol must preserve exact paired inputs or reject them."""

import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


WIRE = load("snapshot_wire", "tests/csi-radio/presence/snapshots.py")
RUN = load("snapshot_protocol", "tests/csi-radio/presence/run.py")
FIXTURE = (
    (ROOT / "tests/csi-radio/presence/fixtures/snapshot-v1.txt")
    .read_bytes()
    .splitlines()
)


class Console:
    def __init__(self, lines):
        self.data = bytearray(b"\n".join(lines) + b"\n")
        self.writes = []

    def read(self, n):
        result = self.data[:n]
        del self.data[:n]
        return bytes(result)

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        pass


class SnapshotTests(unittest.TestCase):
    def test_shared_rust_encoder_fixture_round_trips_exact_words(self):
        lines = iter(FIXTURE)
        a = WIRE.read(next(lines), lambda: next(lines))
        b = WIRE.read(next(lines), lambda: next(lines))
        self.assertEqual(a["header"]["status"], -1)
        self.assertEqual(a["words"]["output"], [])
        self.assertEqual(WIRE.floats(b["words"]["output"]), [2.0, 3.0])
        self.assertEqual(WIRE.inspect(b)["template_comparisons"][0]["double"], 0.0)
        self.assertEqual(b["header"]["fft"], -7)

    def test_incomplete_corrupt_reordered_duplicate_or_unbounded_snapshot_is_rejected(
        self,
    ):
        first = FIXTURE[:3]
        for lines in (
            [first[0].replace(b"v=1", b"v=2"), *first[1:]],
            [first[0].replace(b"cols=2", b"cols=65"), *first[1:]],
            [first[0].replace(b"rows0=2", b"rows0=1024"), *first[1:]],
            [first[0] + b" cols=2", *first[1:]],
            [first[0].replace(b"status=-1", b"status=0"), *first[1:]],
            [first[0], first[1].replace(b"index=0", b"index=1"), first[2]],
            [first[0], first[1].replace(b"3f800000", b"3f800001"), first[2]],
            [first[0], first[1].replace(b",40000000", b""), first[2]],
            [first[0], first[1], first[1], first[2]],
            [first[0], first[1], b"PRESENCE_SNAPSHOT_END window=99 fnv=00000000"],
            [first[0], first[1], b"PRESENCE_SAMPLE interrupted=1"],
        ):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                iterator = iter(lines)
                WIRE.read(next(iterator), lambda: next(iterator))

    def test_client_requires_capture_mode_and_rejects_duplicate_samples(self):
        client = RUN.Client(Console(FIXTURE + FIXTURE))
        with self.assertRaises(ValueError):
            client.read(time.monotonic() + 1)
        client = RUN.Client(Console(FIXTURE + FIXTURE))
        client.snapshots = True
        self.assertEqual(client.read(time.monotonic() + 1)["kind"], "snapshot")
        client.read(time.monotonic() + 1)
        with self.assertRaises(ValueError):
            client.read(time.monotonic() + 1)

    def test_explicit_frozen_input_ack_required_before_credentials(self):
        for suffix in (b"", b" snapshots=1 frozen_inputs=0"):
            console = Console(
                [
                    b"PRESENCE_COMMAND_READY force=0",
                    b"PRESENCE_OPTIONS seconds=30 force=0 engine=espressif_full amplitude=linear"
                    + suffix,
                    b"PRESENCE_CREDENTIALS_READY",
                ]
            )
            with patch.object(
                RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
            ):
                with self.assertRaises(RuntimeError):
                    RUN.Client(console).start(
                        "test-network", "test-password", 30, "linear", snapshots=True
                    )
            self.assertEqual(console.writes, [b"PRESENCE_CAPTURE 30 linear\n"])

    def test_matching_ack_and_bounded_phase_command(self):
        console = Console(
            [
                b"PRESENCE_COMMAND_READY force=0",
                b"PRESENCE_OPTIONS seconds=30 force=0 engine=espressif_full amplitude=linear snapshots=1 frozen_inputs=1",
                b"PRESENCE_CREDENTIALS_READY",
                b"PRESENCE_BEGIN seconds=30 channel=6 calibration=required rf_freshness=not_exported",
            ]
        )
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            client = RUN.Client(console)
            client.start("test-network", "test-password", 30, "linear", snapshots=True)
            client.arm_snapshot(16)
            for tag in (0, 17, "1", True):
                with self.assertRaises(ValueError):
                    client.arm_snapshot(tag)
        self.assertEqual(console.writes[-1], b"SNAPSHOT 16\n")

    def test_any_loss_or_pairing_error_rejects_capture_qualification(self):
        for fields in (
            b"skipped=1 foreign=0 invalid=0 static_bytes=22000",
            b"skipped=0 foreign=1 invalid=0 static_bytes=22000",
            b"skipped=0 foreign=0 invalid=1 static_bytes=22000",
            b"skipped=0 foreign=0 invalid=0 static_bytes=65536",
        ):
            c = RUN.Client(Console([b"PRESENCE_CAPTURE_STATUS " + fields]))
            c.snapshots = True
            with self.assertRaises(ValueError):
                c.read(time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
