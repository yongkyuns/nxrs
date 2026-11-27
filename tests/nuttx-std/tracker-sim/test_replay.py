import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import native
import replay


def trace(path):
    """A dual IMU take with a FIFO batch, one missing sample, and a GNSS gap."""
    with path.open("w", encoding="utf-8") as stream:
        def write(**data):
            stream.write(json.dumps(data) + "\n")

        write(type="header", format="nxrs-motion-tracker", version=1)
        sequence = 100
        fix_sequence = 0
        for offset in range(0, 5_210, 10):
            sequence += 1
            for source, sign in (("imu_primary", 1), ("imu_secondary", -1)):
                if offset == 1_000 and sign == 1:
                    continue
                # The first two samples entered the same recorded FIFO drain.
                stamp = 10_000 + (0 if offset == 10 else offset)
                missed = {"missed_before": 1} if offset == 1_010 and sign == 1 else {}
                write(type="imu", source=source, sequence=sequence, received_ms=stamp,
                      accel_mps2=[sign * (1 if offset < 1000 else 3), 0, 9.8],
                      gyro_rps=[sign * 0.5, 0, 0], **missed)
            if offset in (0, 500, 1_000, 4_700, 5_200):
                fix_sequence += 1
                write(type="gnss", source="gnss", sequence=fix_sequence,
                      received_ms=10_000 + offset, lat_deg=45.25,
                      lon_deg=-76 + offset / 100_000, speed_mps=2)
        write(type="end", reason="stopped", complete=False)


class ReplayTests(unittest.TestCase):
    def test_log_alias_cannot_overwrite_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            capture = base / "capture.ndjson"
            contents = b"keep the recording\n"
            capture.write_bytes(contents)
            log = base / "log.txt"
            os.link(capture, log)
            image = base / "nuttx"
            image.touch()
            result = subprocess.run(
                [sys.executable, native.__file__, str(image), "--replay", str(capture), "--log", str(log)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--log refers to an input file", result.stderr)
            self.assertEqual(capture.read_bytes(), contents)

    def test_stage_retains_fifo_order_and_rejects_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            record = base / "capture.ndjson"
            trace(record)
            staged = replay.stage(record, base)
            self.assertEqual(staged.counts, {"imu_primary": 520, "imu_secondary": 521, "gnss": 5})
            self.assertFalse(staged.complete)
            self.assertEqual(staged.archive_version, 1)
            self.assertEqual(staged.duration_ms, 5_200)
            with (base / replay.SOURCES["imu_primary"]).open("rb") as first:
                self.assertEqual(replay.ORIGIN.unpack(first.read(8)), (10_000,))
                samples = [replay.IMU.unpack(first.read(replay.IMU.size)) for _ in range(2)]
                self.assertEqual(samples[0], samples[1])
                self.assertEqual(samples[0][0], 10_000)
                self.assertGreater(samples[0][1], 0)
            record.write_bytes(record.read_bytes()[:-6])
            with self.assertRaisesRegex(ValueError, "incomplete record"):
                replay.stage(record, base)

    def test_v2_complete_capture_times_stage_without_relabeling_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / "capture.ndjson"
            records = [
                {"type": "header", "format": "nxrs-motion-tracker", "version": 2},
                {"type": "imu", "source": "imu_primary", "sequence": 7,
                 "timestamp_ms": 1000, "timestamp_provenance": "capture",
                 "accel_mps2": [1, 0, 9.8], "gyro_rps": [0, 0, 0]},
                {"type": "gnss", "source": "gnss", "sequence": 8,
                 "timestamp_ms": 1050, "timestamp_provenance": "capture",
                 "lat_deg": 45, "lon_deg": -76, "speed_mps": 2},
                {"type": "end", "reason": "stopped", "complete": True},
            ]
            path.write_text(''.join(json.dumps(row) + '\n' for row in records))
            staged = replay.stage(path, base)
            self.assertEqual((staged.archive_version, staged.origin_ms, staged.duration_ms, staged.complete),
                             (2, 1000, 50, True))
            self.assertEqual(staged.counts, {"imu_primary": 1, "imu_secondary": 0, "gnss": 1})
            self.assertEqual(list(replay.fixes(base / replay.SOURCES['gnss'])), [(1050, 45, -76, 2, None)])

    def test_v2_unsupported_evidence_is_rejected_instead_of_discarded(self):
        sample = {"type": "imu", "source": "imu_primary", "sequence": 7,
                  "timestamp_ms": 1000, "timestamp_provenance": "capture",
                  "accel_mps2": [1, 0, 9.8], "gyro_rps": [0, 0, 0]}
        cases = [
            ([sample | {"timestamp_provenance": provenance}], True, "capture-qualified v2")
            for provenance in ("received", "unknown")
        ] + [
            ([sample | {"timestamp_provenance": "invented"}], True, "invalid v2 timestamp provenance"),
            ([sample | {"received_ms": 1000}], True, "ambiguous v2 timestamp fields"),
            ([sample], False, "complete v2 recording"),
            ([sample, {"type": "no_fix", "source": "gnss", "generation": 1,
                       "received_ms": 1100}], True, "no_fix lifecycle"),
            ([sample, {"type": "source_failure", "source": "gnss", "reason": "io"}], False, "source_failure history"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / "capture.ndjson"
            for samples, complete, message in cases:
                with self.subTest(message=message, samples=samples):
                    records = [{"type": "header", "format": "nxrs-motion-tracker", "version": 2},
                               *samples, {"type": "end", "reason": "stopped", "complete": complete}]
                    path.write_text(''.join(json.dumps(row) + '\n' for row in records))
                    with self.assertRaisesRegex(ValueError, message):
                        replay.stage(path, base)

    @unittest.skipUnless(os.environ.get("NXRS_TRACKER_REPLAY_IMAGE"), "requires the native tracker image")
    def test_replays_through_native_imu_and_gnss_drivers(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            input_path = base / "take.ndjson"
            trace(input_path)
            log = io.BytesIO()
            native.run(Path(os.environ["NXRS_TRACKER_REPLAY_IMAGE"]), log, input_path)
            # Replay stops the guest after the expected sensor counts. A final
            # console write may still be in progress when it stops.
            lines = log.getvalue().split(b'\n')[:-1]
            samples = [json.loads(line[line.index(b'{"v":1,'):]) for line in lines
                       if b'{"v":1,' in line]
            states = [sample["gnss"]["state"] for sample in samples]
            first_live = next(index for index, state in enumerate(states) if state in ("live", "received"))
            stale = states.index("stale", first_live)
            recovered = next(index for index in range(stale, len(states)) if states[index] in ("live", "received"))
            self.assertTrue(all(imu["state"] in ("live", "received") for imu in samples[stale]["imu"]))
            self.assertGreater(samples[recovered]["gnss"]["lon_deg"], samples[first_live]["gnss"]["lon_deg"])
            for index, expected in enumerate((1, -1)):
                first_imu = next(sample["imu"][index] for sample in samples
                                 if sample["imu"][index]["state"] in ("live", "received"))
                self.assertAlmostEqual(first_imu["accel_mps2"][0], expected, delta=0.003)
            self.assertAlmostEqual(samples[recovered]["imu"][1]["accel_mps2"][0], -3, delta=0.003)


if __name__ == "__main__":
    unittest.main()
