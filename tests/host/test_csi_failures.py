import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

R = Path(__file__).resolve().parents[2]


def load(n, p):
    s = importlib.util.spec_from_file_location(n, R / p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


F = load("failure_wire", "tests/csi-radio/presence/failures.py")
RUN = load("failure_runner", "tests/csi-radio/presence/run.py")
REPLAY = load("gain_replay", "tests/csi-radio/presence/replay.py")
Q = load("nonfinite_qemu", "tests/csi-radio/qemu.py")
FIX = (
    (R / "tests/csi-radio/presence/fixtures/numeric-failure-v1.txt")
    .read_bytes()
    .splitlines()
)


class Console:
    def __init__(self, lines):
        self.data = bytearray(b"\n".join(lines) + b"\n")
        self.writes = []

    def read(self, n):
        v = self.data[:n]
        del self.data[:n]
        return bytes(v)

    def write(self, v):
        self.writes.append(v)
        return len(v)

    def flush(self):
        pass


def decode(lines):
    it = iter(lines)
    return F.read(next(it), lambda: next(it))


class FailureTests(unittest.TestCase):
    def test_golden_rust_fixture_preserves_nan_payload_and_source(self):
        r = decode(FIX)
        self.assertEqual(r["words"]["output"][0], 0x7FC01234)
        self.assertEqual(F.inspect(r)["source"], "other")
        self.assertEqual(F.inspect(r)["nonfinite_output_values"], 1)

    def test_corrupt_reordered_truncated_wrong_source_and_reason_fail(self):
        for lines in (
            [FIX[0].replace(b"source=other", b"source=selected"), *FIX[1:]],
            [
                FIX[0].replace(b"reason=nonfinite_output", b"reason=native_status"),
                *FIX[1:],
            ],
            [FIX[0], FIX[2], FIX[1], FIX[3]],
            [*FIX[:-1], FIX[-1] + b"0"],
        ):
            with self.assertRaises(ValueError):
                decode(lines)
        with self.assertRaises(StopIteration):
            decode(FIX[:-1])

    def test_rechecks_actual_bits_with_valid_checksum(self):
        lines = [*FIX]
        lines[2] = lines[2].replace(b"7fc01234", b"00000000")
        h = F.hash_words(
            [1, 1, 3, 7, 99, 0, 2, 1, 0, 0, 0x3F800000, 0x40000000, 0, 0x3F800000]
        )
        lines[-1] = f"PRESENCE_NUMERIC_FAILURE_END window=7 fnv={h:08x}".encode()
        with self.assertRaisesRegex(ValueError, "actual bits"):
            decode(lines)

    def test_native_status_never_demands_output_fragments(self):
        first = (
            FIX[0]
            .replace(b"reason=nonfinite_output", b"reason=native_status")
            .replace(b"status=0", b"status=-1")
        )
        h = F.hash_words([1, 1, 1, 7, 99, 0, 2, 1, 0, -1, 0x3F800000, 0x40000000])
        r = decode(
            [
                first,
                FIX[1],
                f"PRESENCE_NUMERIC_FAILURE_END window=7 fnv={h:08x}".encode(),
            ]
        )
        self.assertEqual(r["words"]["output"], [])

    def test_duplicate_or_unexpected_failure_cannot_be_accepted(self):
        c = RUN.Client(Console(FIX + FIX))
        c.numeric_failures = True
        c.read(time.monotonic() + 1)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            c.read(time.monotonic() + 1)
        with self.assertRaisesRegex(ValueError, "unexpected"):
            RUN.Client(Console(FIX)).read(time.monotonic() + 1)

    def test_status_requires_zero_loss_monotonic_calls_and_previously_delivered_capture(
        self,
    ):
        base = b"PRESENCE_FAILURE_STATUS source=other calls=1 bad_input=0 bad_output=1 native_errors=0 captured=1 losses=0"
        for line in (
            base.replace(b"losses=0", b"losses=1"),
            base.replace(b"calls=1", b"calls=0"),
            base,
        ):
            c = RUN.Client(Console([line]))
            c.numeric_failures = True
            with self.assertRaises(ValueError):
                c.read(time.monotonic() + 1)
        c = RUN.Client(Console(FIX + [base, base.replace(b"calls=1", b"calls=0")]))
        c.numeric_failures = True
        c.read(time.monotonic() + 1)
        self.assertTrue(c.read(time.monotonic() + 1)["captured"])
        with self.assertRaises(ValueError):
            c.read(time.monotonic() + 1)

    def test_explicit_ack_before_credentials(self):
        ack = b"PRESENCE_OPTIONS seconds=180 force=0 engine=espressif_full amplitude=linear snapshots=1 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap calibration=post_training_v2 capture=calibration_pairs_v2 numeric_failures=source_v1 other_inputs=frozen_enqueue"
        for matching in (True, False):
            console = Console(
                [
                    b"PRESENCE_COMMAND_READY force=0",
                    (
                        ack
                        if matching
                        else ack.replace(b" other_inputs=frozen_enqueue", b"")
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
                        b"fixture-pass",
                        180,
                        "linear",
                        snapshots=True,
                        trace=True,
                        calibrated=True,
                        diagnostic_pairs=True,
                        numeric_failures=True,
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                        RUN.Client(console).start(
                            b"fixture",
                            b"fixture-pass",
                            180,
                            "linear",
                            snapshots=True,
                            trace=True,
                            calibrated=True,
                            diagnostic_pairs=True,
                            numeric_failures=True,
                        )
            self.assertEqual(console.writes[0], b"PRESENCE_FAILURES 180 linear\n")
            self.assertEqual(len(console.writes), 2 if matching else 1)


class ReplayTests(unittest.TestCase):
    def test_gain_counterfactuals_are_explicit_and_scale_controls_preserve_direction(
        self,
    ):
        data = [[1.0, 2.0, 4.0], [3.0, 6.0, 12.0]]
        v = REPLAY.variants(data, [1.0, 2.0])
        self.assertEqual(data, [[1.0, 2.0, 4.0], [3.0, 6.0, 12.0]])
        refs = [REPLAY.NUM.reference(m)["projection"] for m in v.values()]
        for ref in refs:
            self.assertAlmostEqual(REPLAY.distance(ref, refs[0]), 0.0, places=12)
        self.assertEqual(v["continuous_divide_reported_gain"][1], [1.5, 3.0, 6.0])

    def test_invalid_gain_zero_energy_and_nonfinite_data_do_not_produce_fake_results(
        self,
    ):
        for matrix, gain in (
            ([[0.0, 0.0]], [1.0]),
            ([[1.0, 2.0]], [0.0]),
            ([[float("nan"), 2.0]], [1.0]),
        ):
            with self.assertRaises(ValueError):
                REPLAY.variants(matrix, gain)

    def test_host_model_reproduces_success_with_nonfinite_zero_energy(self):
        r = REPLAY.NUM.power([[0.0] * 13 for _ in range(10)])
        self.assertEqual(r["status"], 0)
        self.assertTrue(any(not REPLAY.math.isfinite(v) for v in r["vector"]))


class NonfiniteRunnerTests(unittest.TestCase):
    def transcript(self):
        lines = ["CSI_PROBE_ENTERED"]
        for name in ("zero", "underflow", "finite_control"):
            for rows in (10, 7):
                for repeat in range(2):
                    lines.append(
                        f'CSI_NONFINITE_CASE input={name} split={rows},{10-rows} repeat={repeat} status=0 finite={13 if name=="finite_control" else 0} allocations=4 live=0 guards=0'
                    )
        return (
            "\n".join(
                lines + ["CSI_NONFINITE_REPORT checks=48 failures=0 guards=0 live=0"]
            )
            + "\n"
        )

    def test_full_native_witness_and_negative_parser_controls(self):
        valid = self.transcript()
        Q.verify_nonfinite(valid, "CSI_NSH_STATUS_0\n")
        for text in (
            "",
            valid + valid,
            valid.replace("checks=48", "checks=47"),
            valid.replace("finite=0", "finite=13", 1),
            valid.replace("live=0", "live=1", 1),
            valid.replace("repeat=1", "repeat=0", 1),
            valid + "PANIC\n",
        ):
            with self.assertRaises(ValueError):
                Q.verify_nonfinite(text, "CSI_NSH_STATUS_0\n")
        with self.assertRaises(ValueError):
            Q.verify_nonfinite(valid, "CSI_NSH_STATUS_1\n")


if __name__ == "__main__":
    unittest.main()
