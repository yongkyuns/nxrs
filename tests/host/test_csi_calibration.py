"""Correction admission stays explicit; legacy vendor output is preserved."""

import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "corrected_calibration_run", ROOT / "tests/csi-radio/presence/run.py"
)
RUN = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RUN)


class Console:
    def __init__(self, lines):
        self.data = bytearray(b"\n".join(lines) + b"\n")
        self.writes = []

    def read(self, n):
        data = self.data[:n]
        del self.data[:n]
        return bytes(data)

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass


ACK = b"PRESENCE_OPTIONS seconds=180 force=0 engine=espressif_full amplitude=linear snapshots=1 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap calibration=post_training_v1"
STATUS = b"PRESENCE_CORRECTED v=1 ms=1200 phase=holdout state=unknown features=128 average=0.00100000 background_min=0.00090000 background_max=0.00110000 enter=0.00120000 exit=0.00110000 advisory=1"


def read_status(line):
    client = RUN.Client(Console([line]))
    client.calibrated = True
    return client.read(time.monotonic() + 1)


class CalibrationProtocolTests(unittest.TestCase):
    def test_explicit_corrected_case_and_exact_ack_before_credentials(self):
        c = Console(
            [
                b"PRESENCE_COMMAND_READY force=0",
                ACK,
                b"PRESENCE_CREDENTIALS_READY",
                b"PRESENCE_BEGIN seconds=180 channel=6 calibration=required rf_freshness=not_exported",
            ]
        )
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            RUN.Client(c).start(
                b"fixture",
                b"fixture-password",
                180,
                "linear",
                snapshots=True,
                trace=True,
                calibrated=True,
            )
        self.assertEqual(c.writes[0], b"PRESENCE_CALIBRATED 180 linear\n")
        self.assertEqual(
            c.writes[1], RUN.IDF.credential_frame(b"fixture", b"fixture-password")[0]
        )

    def test_legacy_image_or_wrong_policy_cannot_receive_credentials(self):
        for ack in (
            ACK.replace(b" calibration=post_training_v1", b""),
            ACK.replace(b"v1", b"v2"),
        ):
            c = Console(
                [b"PRESENCE_COMMAND_READY force=0", ack, b"PRESENCE_CREDENTIALS_READY"]
            )
            with patch.object(
                RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
            ):
                with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                    RUN.Client(c).start(
                        b"fixture",
                        b"fixture-password",
                        180,
                        "linear",
                        snapshots=True,
                        trace=True,
                        calibrated=True,
                    )
            self.assertEqual(c.writes, [b"PRESENCE_CALIBRATED 180 linear\n"])

    def test_correction_requires_owned_inputs_gain_trace_and_linear_amplitudes(self):
        for kwargs in (
            {"snapshots": True, "trace": True},
            {"amplitude": "linear", "snapshots": True},
            {"amplitude": "linear", "snapshots": False},
            {"calibrated": 1},
        ):
            c = Console([])
            with self.assertRaises(ValueError):
                RUN.Client(c).start(
                    b"fixture",
                    b"fixture-password",
                    180,
                    **({"calibrated": True} | kwargs)
                )
            self.assertFalse(c.writes)

    def test_empty_holdout_is_unknown_until_explicit_acceptance(self):
        self.assertEqual(read_status(STATUS)["state"], "unknown")
        for phase in (b"uncalibrated", b"background", b"holdout", b"rejected"):
            bad = STATUS.replace(b"phase=holdout", b"phase=" + phase).replace(
                b"state=unknown", b"state=not_detected"
            )
            with self.assertRaisesRegex(ValueError, "treated as detection"):
                read_status(bad)

    def test_threshold_cannot_be_below_background_and_schema_corruption_fails(self):
        for line in (
            STATUS.replace(b"enter=0.00120000", b"enter=0.00010000"),
            STATUS.replace(b"exit=0.00110000", b"exit=0.00010000"),
            STATUS.replace(b"average=0.00100000", b"average=nan"),
            STATUS + b" v=1",
            STATUS.replace(b"advisory=1", b"advisory=0"),
        ):
            with self.assertRaises(ValueError):
                read_status(line)

    def test_negative_holdout_remains_visible_without_a_clear_state(self):
        value = read_status(STATUS.replace(b"phase=holdout", b"phase=rejected"))
        self.assertEqual(value["phase"], "rejected")
        self.assertEqual(value["state"], "unknown")

    def test_calibration_commands_are_unavailable_in_unchanged_reference(self):
        c = Console([])
        client = RUN.Client(c)
        with self.assertRaises(ValueError):
            client.command("CALIBRATE_ACCEPT")
        self.assertFalse(c.writes)

    def test_control_acknowledgements_require_the_corrected_case(self):
        line = b"PRESENCE_CALIBRATION_CONTROL phase=ready"
        with self.assertRaises(ValueError):
            RUN.Client(Console([line])).read(time.monotonic() + 1)
        self.assertEqual(read_status(line), line.decode())

    def test_repeated_reports_do_not_become_new_calibration_evidence(self):
        client = RUN.Client(Console([STATUS, STATUS]))
        client.calibrated = True
        client.read(time.monotonic() + 1)
        with self.assertRaisesRegex(ValueError, "report clock"):
            client.read(time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
