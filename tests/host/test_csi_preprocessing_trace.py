"""Exact source identities, unavailable gain and corruption must be rejected."""

import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WIRE = load("rf_trace_wire", "tests/csi-radio/presence/trace.py")
SNAPSHOTS = load("rf_trace_snapshots", "tests/csi-radio/presence/snapshots.py")
RUN = load("rf_trace_run", "tests/csi-radio/presence/run.py")
INSTRUMENT = load("rf_instrument", "tests/csi-radio/presence/instrument.py")
FIXTURE = (
    (ROOT / "tests/csi-radio/presence/fixtures/trace-v1.txt").read_bytes().splitlines()
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


def decoded(lines=FIXTURE):
    iterator = iter(lines)
    return WIRE.read(next(iterator), lambda: next(iterator))


class TraceTests(unittest.TestCase):
    def test_shared_rust_fixture_checks_source_times_gain_and_preprocessing(self):
        trace = decoded()
        result = WIRE.inspect(trace)
        self.assertEqual(result["preprocessing_max_absolute_error"], 0)
        self.assertEqual(result["gain_factor_min"], 0.5)
        self.assertEqual(result["oldest_rx_age_at_compute_us"], 3000)
        self.assertEqual(result["queue_delay_us"], 1000)
        self.assertFalse(result["paired_snapshot_verified"])
        self.assertNotIn(
            "rows", {key for key, val in result.items() if isinstance(val, list)}
        )

    def test_only_explicit_window_and_exact_matrix_bits_pair_with_numerical_snapshot(
        self,
    ):
        trace = decoded()
        snapshot = {
            "header": dict(window=7, tag=1, cols=2, rows0=2, rows1=0, status=0),
            "words": {"matrix": [0x40A00000] * 4},
        }
        self.assertTrue(WIRE.inspect(trace, snapshot)["paired_snapshot_verified"])
        for key, value in (
            ("window", 8),
            ("tag", 2),
            ("cols", 3),
            ("rows0", 1),
            ("status", -1),
        ):
            bad = {
                "header": {**snapshot["header"], key: value},
                "words": snapshot["words"],
            }
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "exactly"):
                WIRE.inspect(trace, bad)
        snapshot["words"]["matrix"][0] += 1
        with self.assertRaisesRegex(ValueError, "exactly"):
            WIRE.inspect(trace, snapshot)

    def test_corruption_reordering_duplicates_and_incomplete_fragments_fail(self):
        for lines in (
            [FIXTURE[0].replace(b"v=1", b"v=2"), *FIXTURE[1:]],
            [FIXTURE[0] + b" cols=2", *FIXTURE[1:]],
            [FIXTURE[0].replace(b"rows=2", b"rows=33"), *FIXTURE[1:]],
            [FIXTURE[0].replace(b"cols=2", b"cols=17"), *FIXTURE[1:]],
            [
                FIXTURE[0].replace(b"computed_us=4000", b"computed_us=2999"),
                *FIXTURE[1:],
            ],
            [FIXTURE[0], FIXTURE[2], FIXTURE[1], *FIXTURE[3:]],
            [FIXTURE[0], FIXTURE[1], FIXTURE[1], *FIXTURE[2:]],
            [*FIXTURE[:-1], FIXTURE[-1].replace(b"fnv=", b"fnv=f")],
        ):
            with self.subTest(first=lines[0]), self.assertRaises(ValueError):
                decoded(lines)
        with self.assertRaises(StopIteration):
            decoded(FIXTURE[:-1])

    def test_unavailable_gain_and_reversed_rf_progress_are_rejected_before_checksum(
        self,
    ):
        # Mutate specific row metadata; deliberate failure must be provenance,
        # not only the final FNV, which is tested separately above.
        for row, index, new in (
            (0, 7, 0),
            (0, 9, 0x7FC00000),
            (0, 16, 0),
            (0, 18, 4001),
            (1, 0, 1),
            (1, 1, 1000),
        ):
            lines = FIXTURE[:]
            at = 1 + row * 2 + index // 16
            prefix, bits = lines[at].split(b"bits=")
            words = bits.split(b",")
            words[index % 16] = f"{new:08x}".encode()
            lines[at] = prefix + b"bits=" + b",".join(words)
            with self.subTest(index=index), self.assertRaisesRegex(
                ValueError, "provenance|RF identity"
            ):
                decoded(lines)

    def test_trace_requires_exact_mode_acknowledgement_before_credentials(self):
        for option in (
            b"PRESENCE_OPTIONS seconds=30 force=0 engine=espressif_full amplitude=linear snapshots=1 frozen_inputs=1",
            b"PRESENCE_OPTIONS seconds=30 force=0 engine=espressif_full amplitude=linear snapshots=1 frozen_inputs=1 rf_trace=1 enqueue_frozen=0",
        ):
            console = Console(
                [
                    b"PRESENCE_COMMAND_READY force=0",
                    option,
                    b"PRESENCE_CREDENTIALS_READY",
                ]
            )
            client = RUN.Client(console)
            with patch.object(
                RUN.IDF.RADIO, "write_console", side_effect=lambda c, b: c.write(b)
            ):
                with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                    client.start(
                        b"test-network",
                        b"test-password",
                        30,
                        "linear",
                        snapshots=True,
                        trace=True,
                    )
            self.assertEqual(console.writes, [b"PRESENCE_TRACE 30 linear\n"])
        with self.assertRaises(ValueError):
            RUN.Client(Console([])).start(
                b"test-network", b"test-password", 30, trace=True
            )

    def test_successful_trace_handshake_and_duplicate_source_identity(self):
        console = Console(
            [
                b"PRESENCE_COMMAND_READY force=0",
                b"PRESENCE_OPTIONS seconds=30 force=0 engine=espressif_full amplitude=linear snapshots=1 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap",
                b"PRESENCE_CREDENTIALS_READY",
                b"PRESENCE_BEGIN seconds=30 channel=1 calibration=required rf_freshness=not_exported",
                *FIXTURE,
                *FIXTURE,
            ]
        )
        client = RUN.Client(console)
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, b: c.write(b)
        ):
            client.start(
                b"test-network",
                b"test-password",
                30,
                "linear",
                snapshots=True,
                trace=True,
            )
        self.assertEqual(console.writes[0], b"PRESENCE_TRACE 30 linear\n")
        self.assertEqual(
            client.read(time.monotonic() + 1)["kind"], "rf_preprocessing_trace"
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            client.read(time.monotonic() + 1)

    def test_linkage_guard_rejects_the_previous_getter_only_observer(self):
        with self.assertRaisesRegex(ValueError, "actual compensation"):
            INSTRUMENT.require_compensation_observer(
                "4200: call8 4201 <esp_csi_gain_ctrl_compensate_rx_gain>"
            )
        self.assertTrue(
            INSTRUMENT.require_compensation_observer(
                "4200: call8 4201 <__wrap_esp_csi_gain_ctrl_compensate_rx_gain>"
            )["rx_calls_compensation_wrapper"]
        )

    def test_trace_status_contends_or_exceeds_budget_instead_of_claiming_valid(self):
        for line in (
            b"PRESENCE_TRACE_STATUS faults=1 first_fault_line=100 rf_evictions=0 gain_unavailable_rows=49 gain_ready=1 other_peer_frames=3 static_bytes=90000",
            b"PRESENCE_TRACE_STATUS faults=0 first_fault_line=0 rf_evictions=0 gain_unavailable_rows=49 gain_ready=1 other_peer_frames=3 static_bytes=999999",
        ):
            client = RUN.Client(Console([line]))
            client.trace = True
            with self.assertRaises(ValueError):
                client.read(time.monotonic() + 1)
        client = RUN.Client(
            Console(
                [
                    b"PRESENCE_TRACE_STATUS faults=0 first_fault_line=0 rf_evictions=2 gain_unavailable_rows=49 gain_ready=1 other_peer_frames=3 static_bytes=90000"
                ]
            )
        )
        client.trace = True
        status = client.read(time.monotonic() + 1)
        self.assertEqual(status["rf_evictions"], 2)
        self.assertTrue(status["gain_ready"])
        self.assertEqual(status["gain_unavailable_rows"], 49)
        client = RUN.Client(
            Console(
                [
                    b"PRESENCE_TRACE_STATUS faults=0 first_fault_line=0 rf_evictions=0 gain_unavailable_rows=2 gain_ready=0 other_peer_frames=3 static_bytes=90000"
                ]
            )
        )
        client.trace = True
        status = client.read(time.monotonic() + 1)
        self.assertFalse(status["gain_ready"])
        self.assertEqual(status["gain_unavailable_rows"], 2)
        client = RUN.Client(Console(FIXTURE))
        with self.assertRaisesRegex(ValueError, "unexpected"):
            client.read(time.monotonic() + 1)

    def test_snapshot_zero_sequence_or_failed_or_nonfinite_gain_stays_unavailable(self):
        fixture = (
            (ROOT / "tests/csi-radio/presence/fixtures/snapshot-v1.txt")
            .read_bytes()
            .splitlines()
        )
        iterator = iter(fixture)
        SNAPSHOTS.read(next(iterator), lambda: next(iterator))
        snapshot = SNAPSHOTS.read(next(iterator), lambda: next(iterator))
        self.assertTrue(SNAPSHOTS.inspect(snapshot)["gain_factor_available"])
        for key, v in (
            ("gain_seq", 0),
            ("gain_status", -1),
            ("factor", 0x7FC00000),
            ("factor", 0),
        ):
            header = snapshot["header"].copy()
            header[key] = v
            self.assertFalse(
                SNAPSHOTS.inspect({**snapshot, "header": header})[
                    "gain_factor_available"
                ]
            )

    def test_instrumentation_refuses_source_drift(self):
        # The positive pinned-source/compiled-hook case is the native builder's
        # mandatory hash/layout/linkage gate, not a machine-local host fixture.
        with self.assertRaisesRegex(ValueError, "exact pinned"):
            INSTRUMENT.radar(b"new or already instrumented source")
