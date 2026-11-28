"""Timing evidence admission checks; real emulator observations are separate."""

import unittest

from qualify_timing_qemu import acquisition_bridge_evidence, evidence


CAPTURE = """NXRS_TIMING_CLOCK frequency_hz=20000000 before=10 after=200000
NXRS_NOTE_BEGIN schema=1 source=nuttx-noteram-ascii clock=test-counter cpus=1 mode=0 notes_lost=0
worker-4 [0] 1.100000000: tracing_mark_write: B|4|work
worker-4 [0] 1.100000050: sched_switch: prev_pid=0 ==> next_pid=4
worker-4 [0] 1.100000100: irq_handler_entry: irq=15
worker-4 [0] 1.100000200: irq_handler_exit: irq=15
worker-4 [0] 1.100000300: tracing_mark_write: E|4|work
NXRS_NOTE_END schema=1 mode=0 notes_lost=0 complete=1
NXRS_TIMING_WITNESS PASS rounds=4
"""


class TimingEvidenceChecks(unittest.TestCase):
    def test_monotonic_native_framing_is_admitted(self):
        result = evidence(CAPTURE.encode())
        self.assertTrue(result["monotonic_records"])
        self.assertEqual(result["last_record_ns"] - result["first_record_ns"], 300)
        self.assertEqual(result["lost_records"], 0)

    def test_clock_probe_does_not_hide_backward_irq_timestamps(self):
        capture = CAPTURE.replace("1.100000100", "1.100000025")
        with self.assertRaisesRegex(RuntimeError, "ran backward"):
            evidence(capture.encode())

    def test_constant_note_timestamps_are_not_timing_evidence(self):
        capture = CAPTURE
        for suffix in ("050", "100", "200", "300"):
            capture = capture.replace("1.100000" + suffix, "1.100000000")
        with self.assertRaisesRegex(RuntimeError, "did not advance"):
            evidence(capture.encode())

    def test_overflow_rejects_complete_dump_and_pass_marker(self):
        capture = CAPTURE.replace("mode=0 notes_lost=0", "mode=2 notes_lost=1")
        with self.assertRaisesRegex(RuntimeError, "no-overwrite/no-overflow"):
            evidence(capture.encode())

    def test_truncated_dump_rejects_pass_marker(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            evidence(CAPTURE.replace("complete=1", "complete=0").encode())

    def test_malformed_note_inside_frame_is_rejected(self):
        capture = CAPTURE.replace("worker-4 [0] 1.100000200", "truncated record")
        with self.assertRaisesRegex(RuntimeError, "Malformed"):
            evidence(capture.encode())


class AcquisitionBridgeChecks(unittest.TestCase):
    @staticmethod
    def capture():
        rows = ["NXRS_NOTE_BEGIN schema=1"]
        for round_ in range(4):
            base = 100 + round_ * 20
            for offset, edge, name in [(0, "I", "wake"), (1, "B", "drain"), (9, "E", "drain")]:
                rows.append(f"worker-4 [0] 1.{base+offset:09d}: tracing_mark_write: {edge}|4|imu/qemu-fixture/{name}")
        rows.extend(["NXRS_NOTE_END schema=1", "NXRS_ACQUISITION_BRIDGE rounds=4 disabled_probes=2"])
        return "\n".join(rows) + "\n"

    def test_four_observed_scopes_and_wakes_are_required(self):
        for newline in ["\n", "\r\n"]:
            with self.subTest(newline=newline):
                result = acquisition_bridge_evidence(self.capture().replace("\n", newline).encode())
                self.assertEqual(result["drain_scopes"], 4)
                self.assertEqual(result["wake_instants"], 4)
                self.assertTrue(result["disabled_markers_absent"])

    def test_disabled_bridge_emission_is_rejected(self):
        for label in ["bridge/disabled-before-start", "bridge/disabled-after-stop"]:
            text = self.capture().replace("NXRS_NOTE_END", f"worker-4 [0] 1.000000200: tracing_mark_write: I|4|{label}\nNXRS_NOTE_END")
            with self.subTest(label=label), self.assertRaisesRegex(RuntimeError, "tracing was disabled"):
                acquisition_bridge_evidence(text.encode())

    def test_missing_wake_or_wrong_task_does_not_qualify_bridge(self):
        original = self.capture()
        missing = original.replace("worker-4 [0] 1.000000100: tracing_mark_write: I|4|imu/qemu-fixture/wake\n", "")
        with self.assertRaisesRegex(RuntimeError, "four wake/begin/end"):
            acquisition_bridge_evidence(missing.encode())
        mismatch = original.replace("B|4|", "B|5|", 1)
        with self.assertRaisesRegex(RuntimeError, "task or source-clock"):
            acquisition_bridge_evidence(mismatch.encode())

    def test_execution_receipt_is_required_in_addition_to_absent_disabled_notes(self):
        with self.assertRaisesRegex(RuntimeError, "execution receipt"):
            acquisition_bridge_evidence(self.capture().replace("disabled_probes=2", "disabled_probes=0").encode())


if __name__ == "__main__":
    unittest.main()
