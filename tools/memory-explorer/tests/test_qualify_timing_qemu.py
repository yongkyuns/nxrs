"""Timing evidence admission checks; real emulator observations are separate."""

import unittest

from qualify_timing_qemu import evidence


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


if __name__ == "__main__":
    unittest.main()
