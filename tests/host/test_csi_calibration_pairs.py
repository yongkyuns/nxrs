"""Automatic evidence must identify the actual event, not the next sample."""

import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAIR = load("automatic_pair_wire", "tests/csi-radio/presence/calibration_pairs.py")
RUN = load("automatic_pair_run", "tests/csi-radio/presence/run.py")
FIXTURE = (
    (ROOT / "tests/csi-radio/presence/fixtures/calibration-pair-v2.txt")
    .read_bytes()
    .splitlines()
)


class Console:
    def __init__(self, lines):
        self.data = bytearray(b"\n".join(lines) + b"\n")
        self.writes = []

    def read(self, n):
        out = self.data[:n]
        del self.data[:n]
        return bytes(out)

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass


def decode(lines=FIXTURE):
    iterator = iter(lines)
    return PAIR.read(next(iterator), lambda: next(iterator))


def checksum(lines):
    h = dict(part.split("=", 1) for part in lines[0].decode().split()[1:])
    keys = ["v", "tag", "window", "sequence"]
    words = [int(h[k]) for k in keys] + [PAIR.PHASES.index(h["phase"])]
    words += [
        int(h[k], 16 if k in PAIR.FLOATS else 10)
        for k in ["features", "filled", "index", *PAIR.FLOATS, "stack_free_bytes"]
    ]
    if h["v"] == "2":
        words += [PAIR.REJECTIONS.index(h["rejection"])]
    for line in lines[1:-1]:
        for entry in line.decode().split("entries=")[1].split(","):
            words.extend(int(value, 16) for value in entry.split(":"))
    result = 2166136261
    for word in words:
        result = PAIR.hash_word(result, word)
    return [
        *lines[:-1],
        f'PRESENCE_PAIR_END window={h["window"]} fnv={result:08x}'.encode(),
    ]


class CalibrationPairTests(unittest.TestCase):
    def test_shared_rust_fixture_replays_full_history_statistic(self):
        result = decode()
        self.assertEqual(result["header"]["window"], 7)
        self.assertEqual(PAIR.number(result["header"]["average"]), 7 / 1024)
        self.assertEqual(result["sequences"], list(range(1, 26)))

    def test_legacy_pair_is_explicitly_unattributed_and_disallowed_in_current_trial(
        self,
    ):
        old = (
            (ROOT / "tests/csi-radio/presence/fixtures/calibration-pair-v1.txt")
            .read_bytes()
            .splitlines()
        )
        self.assertEqual(decode(old)["header"]["v"], 1)
        client = RUN.Client(Console(old))
        client.diagnostic_pairs = True
        with self.assertRaisesRegex(ValueError, "legacy pair"):
            client.read(time.monotonic() + 1)

    def test_shared_rust_rejection_fixture_carries_checksum_covered_reason(self):
        lines = (
            (ROOT / "tests/csi-radio/presence/fixtures/calibration-rejection-v2.txt")
            .read_bytes()
            .splitlines()
        )
        value = decode(lines)["header"]
        self.assertEqual(
            (value["phase"], value["reason"], value["rejection"]),
            ("rejected", "first_rejected", "source_contention"),
        )

    def test_reason_is_required_consistent_and_covered_by_checksum(self):
        for change in (
            FIXTURE[0].replace(b"rejection=none", b"rejection=trace_fault"),
            FIXTURE[0].replace(b"rejection=none", b"rejection=bogus"),
            FIXTURE[0].replace(b" rejection=none", b""),
        ):
            with self.assertRaises(ValueError):
                decode([change, *FIXTURE[1:]])
        rejected = [
            FIXTURE[0]
            .replace(b"tag=14", b"tag=16")
            .replace(b"reason=training_complete", b"reason=first_rejected")
            .replace(b"phase=background", b"phase=rejected")
            .replace(b"rejection=none", b"rejection=source_contention"),
            *FIXTURE[1:],
        ]
        self.assertEqual(
            decode(checksum(rejected))["header"]["rejection"], "source_contention"
        )
        corrupted = checksum(rejected)
        corrupted[0] = corrupted[0].replace(b"source_contention", b"clock_rollback")
        with self.assertRaisesRegex(ValueError, "checksum"):
            decode(corrupted)

    def test_history_semantics_are_checked_even_with_a_valid_checksum(self):
        wrong_mean = [
            FIXTURE[0].replace(b"average=3be00000", b"average=3be00001"),
            *FIXTURE[1:],
        ]
        with self.assertRaisesRegex(ValueError, "replay exactly"):
            decode(checksum(wrong_mean))
        wrong_sequence = [
            FIXTURE[0],
            FIXTURE[1].replace(b"00000002:", b"00000001:"),
            *FIXTURE[2:],
        ]
        with self.assertRaisesRegex(ValueError, "duplicate/reversed"):
            decode(checksum(wrong_sequence))

    def test_missing_reordered_corrupt_and_duplicate_fragments_are_rejected(self):
        for lines in (
            [FIXTURE[0], FIXTURE[2], FIXTURE[1], *FIXTURE[3:]],
            [FIXTURE[0], FIXTURE[1], FIXTURE[1], *FIXTURE[2:]],
            [*FIXTURE[:-1], FIXTURE[-1].replace(b"fnv=", b"fnv=0")],
            [FIXTURE[0] + b" tag=14", *FIXTURE[1:]],
        ):
            with self.assertRaises(ValueError):
                decode(lines)
        with self.assertRaises(StopIteration):
            decode(FIXTURE[:-1])

    def test_reason_bounds_phase_and_nonfinite_values_are_rejected(self):
        for before, after in [
            (b"reason=training_complete", b"reason=first_rejected"),
            (b"phase=background", b"phase=rejected"),
            (b"filled=25", b"filled=24"),
            (b"index=0", b"index=25"),
            (b"average=3be00000", b"average=7fc00000"),
        ]:
            with self.assertRaises(ValueError):
                decode([FIXTURE[0].replace(before, after), *FIXTURE[1:]])

    def test_duplicate_event_is_not_accepted_as_a_new_capture(self):
        client = RUN.Client(Console(FIXTURE + FIXTURE))
        client.diagnostic_pairs = True
        client.read(time.monotonic() + 1)
        with self.assertRaisesRegex(ValueError, "duplicate calibration event"):
            client.read(time.monotonic() + 1)

    def test_automatic_mode_requires_exact_ack_before_credentials(self):
        ack = b"PRESENCE_OPTIONS seconds=180 force=0 engine=espressif_full amplitude=linear snapshots=1 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap calibration=post_training_v2 capture=calibration_pairs_v2"
        for matching in (True, False):
            console = Console(
                [
                    b"PRESENCE_COMMAND_READY force=0",
                    (
                        ack
                        if matching
                        else ack.replace(b" capture=calibration_pairs_v2", b"")
                    ),
                    b"PRESENCE_CREDENTIALS_READY",
                    b"PRESENCE_BEGIN seconds=180 channel=6 calibration=required rf_freshness=not_exported",
                ]
            )
            with patch.object(
                RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
            ):
                if matching:
                    RUN.Client(console).start(
                        b"fixture",
                        b"fixture-password",
                        180,
                        "linear",
                        snapshots=True,
                        trace=True,
                        calibrated=True,
                        diagnostic_pairs=True,
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                        RUN.Client(console).start(
                            b"fixture",
                            b"fixture-password",
                            180,
                            "linear",
                            snapshots=True,
                            trace=True,
                            calibrated=True,
                            diagnostic_pairs=True,
                        )
            self.assertEqual(console.writes[0], b"PRESENCE_DIAGNOSTIC 180 linear\n")
            self.assertEqual(len(console.writes), 2 if matching else 1)

    def test_reserved_tags_and_unexpected_or_failed_capture_status_are_rejected(self):
        console = Console([])
        client = RUN.Client(console)
        client.snapshots = True
        client.diagnostic_pairs = True
        for tag in (14, 15, 16):
            with self.assertRaisesRegex(ValueError, "reserved"):
                client.arm_snapshot(tag)
        self.assertFalse(console.writes)
        for enabled, line in [
            (False, b"PRESENCE_PAIR_STATUS failures=0"),
            (True, b"PRESENCE_PAIR_STATUS failures=1"),
        ]:
            client = RUN.Client(Console([line]))
            client.diagnostic_pairs = enabled
            with self.assertRaises(ValueError):
                client.read(time.monotonic() + 1)
        client = RUN.Client(Console([b"PRESENCE_PAIR_STATUS failures=0"]))
        client.diagnostic_pairs = True
        self.assertEqual(client.read(time.monotonic() + 1)["failures"], 0)

    def test_matrix_identity_is_checked_before_claiming_pairing(self):
        pair = decode()
        lines = iter(
            (ROOT / "tests/csi-radio/presence/fixtures/trace-v1.txt")
            .read_bytes()
            .splitlines()
        )
        trace = PAIR.TRACE.read(next(lines), lambda: next(lines))
        h = trace["header"]
        snapshot = {
            "header": {
                "window": h["window"],
                "tag": h["tag"],
                "cols": h["cols"],
                "rows0": h["rows0"],
                "rows1": h["rows"] - h["rows0"],
                "status": h["status"],
            },
            "words": {"matrix": [0] * 4},
        }
        with self.assertRaisesRegex(ValueError, "match exactly"):
            PAIR.inspect(pair, trace, snapshot)


if __name__ == "__main__":
    unittest.main()
