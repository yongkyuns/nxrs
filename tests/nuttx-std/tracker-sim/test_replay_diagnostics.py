"""Host controls for the temporary replay diagnostic branch; no simulator required."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import native
import replay
import test_replay


class ReplayDiagnosticsTests(unittest.TestCase):
    def guest(self, directory, counts):
        (directory / replay.SOURCES["gnss"]).write_bytes(b"")
        process = SimpleNamespace(poll=Mock(return_value=None))
        recording = replay.Recording(10_000, 5_200, counts, False)
        guest = native.ReplayGuest(process, io.BytesIO(), directory, recording)
        guest.epoch = 100.0
        guest.command = Mock(return_value=0)
        guest.until = Mock()
        guest.pump = Mock(side_effect=AssertionError("unexpected extra replay work"))
        return guest

    def test_expired_drain_reports_existing_state_without_pumping_again(self):
        with TemporaryDirectory() as folder:
            counts = {"imu_primary": 520, "imu_secondary": 521, "gnss": 5}
            guest = self.guest(Path(folder), counts)
            guest.imu_totals = {"imu_secondary": 521}
            guest.receiver = SimpleNamespace(rate=1)
            guest.uart = object()
            guest.pending.extend(b"TRACKER_REPLAY_IMU_DONE 1 520")
            with patch.object(native.time, "monotonic", return_value=110.25):
                with self.assertRaisesRegex(TimeoutError, "did not drain the recording") as caught:
                    guest.play()
            state = caught.exception.replay_state
            self.assertEqual(state["expected_counts"], counts)
            self.assertEqual(state["observed_imu_totals"], {"imu_secondary": 521})
            self.assertEqual(state["sent_gnss_fixes"], 0)
            self.assertEqual(state["elapsed_seconds"], 10.25)
            self.assertAlmostEqual(state["drain_deadline_elapsed_seconds"], 10.2)
            self.assertAlmostEqual(state["deadline_overrun_seconds"], 0.05)
            self.assertEqual(bytes.fromhex(state["pending_line_hex"]), guest.pending)
            self.assertEqual(state["receiver_rate"], 1)
            self.assertTrue(state["uart_open"])
            self.assertIsNone(state["process_returncode"])
            guest.pump.assert_not_called()
            guest.process.poll.assert_called_once_with()

    def test_completed_drain_does_not_collect_failure_diagnostics(self):
        with TemporaryDirectory() as folder:
            counts = {"imu_primary": 520, "imu_secondary": 521, "gnss": 0}
            guest = self.guest(Path(folder), counts)
            guest.imu_totals = {"imu_primary": 520, "imu_secondary": 521}
            with patch.object(native.time, "monotonic", return_value=105.5), redirect_stdout(io.StringIO()):
                guest.play()
            guest.pump.assert_not_called()
            guest.process.poll.assert_not_called()

    def test_failure_retains_full_console_and_reraises_the_same_error(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            log = io.BytesIO()
            error = TimeoutError("the simulated IMUs did not drain the recording")
            error.replay_state = {"observed_imu_totals": {"imu_secondary": 521}}
            finished = False

            def run(*_):
                nonlocal finished
                try:
                    log.write(b"complete\r\npartial\xff")
                    raise error
                finally:
                    finished = True

            with patch.object(native, "run", side_effect=run):
                with self.assertRaises(TimeoutError) as caught:
                    test_replay.run_with_evidence(Path("image"), log, Path("trace"), directory)
            self.assertIs(caught.exception, error)
            self.assertTrue(finished)
            self.assertEqual((directory / "replay.log").read_bytes(), log.getvalue())
            state = json.loads((directory / "replay-failure.json").read_text())
            self.assertEqual(state["replay_state"], error.replay_state)
            self.assertEqual(state["error_type"], "TimeoutError")

    def test_success_overwrites_console_and_removes_stale_failure(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "replay.log").write_bytes(b"old success")
            (directory / "replay-failure.json").write_text("old failure")
            log = io.BytesIO()
            with patch.object(native, "run", side_effect=lambda *_: log.write(b"new console")):
                test_replay.run_with_evidence(Path("image"), log, Path("trace"), directory)
            self.assertEqual((directory / "replay.log").read_bytes(), b"new console")
            self.assertFalse((directory / "replay-failure.json").exists())


if __name__ == "__main__":
    unittest.main()
