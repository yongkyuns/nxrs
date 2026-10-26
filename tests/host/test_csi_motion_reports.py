"""Motion report version, unavailable-state and source-clock boundaries."""

import importlib.util
from pathlib import Path
import struct
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "motion_report_protocol", ROOT / "tests/csi-radio/presence/run.py"
)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


REPORT = (
    f"PRESENCE_MOTION v=2 time_us=100 phase=Ready state=Idle fault=None "
    f"phase_bins=10 positive_bins=0 quiet_bins=10 thresholds=1 centre_bits={bits(.01)} "
    f"mad_bits={bits(.001)} enter_bits={bits(.016)} exit_bits={bits(.013)} "
    "advisory=1 physical_detector_admitted=0"
).encode()


class Reports(unittest.TestCase):
    def test_threshold_bits_and_unavailable_are_preserved(self):
        r = M.motion_status(REPORT)
        self.assertEqual(
            (r["phase"], r["state"], r["enter_bits"]), ("ready", "idle", bits(0.016))
        )
        r = M.motion_status(
            b"PRESENCE_MOTION_UNAVAILABLE v=2 time_us=101 state=unknown reason=owner_unavailable advisory=1 physical_detector_admitted=0"
        )
        self.assertEqual(r["kind"], "motion_trial_unavailable")

    def test_old_schema_forged_admission_and_invalid_thresholds_fail(self):
        for old, new in (
            (b"v=2", b"v=1"),
            (b"state=Idle", b"state=Clear"),
            (b"phase=Ready", b"phase=Holdout"),
            (b"fault=None", b"fault=Some(Expired)"),
            (b"positive_bins=0", b"positive_bins=1"),
            (b"thresholds=1", b"thresholds=0"),
            (b"physical_detector_admitted=0", b"physical_detector_admitted=1"),
            (f"enter_bits={bits(.016)}".encode(), f"enter_bits={bits(1.)}".encode()),
            (
                f"enter_bits={bits(.016)}".encode(),
                f"enter_bits={bits(float('nan'))}".encode(),
            ),
        ):
            with self.subTest(replacement=new):
                with self.assertRaises(ValueError):
                    M.motion_status(REPORT.replace(old, new))
        with self.assertRaises(ValueError):
            M.motion_status(REPORT + b" v=2")

    def test_clock_is_monotonic_and_reports_require_the_declared_record_case(self):
        class Console:
            data = bytearray(REPORT + b"\n" + REPORT + b"\n")

            def read(self, n):
                r = self.data[:n]
                del self.data[:n]
                return bytes(r)

        client = M.Client(Console())
        with self.assertRaisesRegex(ValueError, "unexpected motion"):
            client.read(time.monotonic() + 1)
        client.motion_case, client.record_sink = "temporal-motion-v1", lambda _: None
        client.read(time.monotonic() + 1)
        # Use a fresh identical report to exercise the client's cross-report clock.
        client.console.data.extend(REPORT + b"\n")
        with self.assertRaisesRegex(ValueError, "clock reversed or duplicate"):
            client.read(time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
