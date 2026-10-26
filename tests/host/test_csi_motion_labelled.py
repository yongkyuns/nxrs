"""Human cue, RF-boundary and fail-closed trial-controller regressions."""

import importlib.util
from pathlib import Path
import struct
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "motion_labelled", ROOT / "tests/csi-lab/motion_labelled.py"
)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def motion(now, phase="ready", state="idle", label=1, bin_id=None):
    time_us = round(now * 1_000_000)
    return dict(
        kind="recorded_motion",
        time_us=time_us,
        phase=phase,
        state=state,
        fault=0,
        label=label,
        thresholds=(1, 2, 3, 4) if phase in ("holdout", "ready") else None,
        bin=(
            None
            if bin_id is None
            else dict(
                id=bin_id, oldest_us=time_us - 200_000, newest_us=time_us, samples=7
            )
        ),
    )


class ScheduleTests(unittest.TestCase):
    def ready(self):
        s = M.Schedule(confirmed_empty=True)
        s.tick(dict(kind="recorded_label", label=1, time_us=0), 0)
        s.tick(dict(kind="rf_trace_status", faults=0, gain_ready=True), 0)
        for now in range(11):
            commands = s.tick(motion(now, "uncalibrated", "unknown"), now)
        self.assertEqual(commands, ["TRAIN_START"])
        s.tick("PRESENCE_TRAIN_START ms=1", 10)
        for now in range(11, 71):
            commands = s.tick(motion(now, "uncalibrated", "unknown"), now)
        self.assertEqual(commands, ["TRAIN_STOP"])
        s.tick("PRESENCE_TRAIN_STOP ms=1 status=0 templates=25", 70)
        s.tick(motion(70, "background", "unknown"), 70)
        for now in range(71, 103):
            s.tick(motion(now, "background", "unknown"), now)
        s.tick(motion(102, "holdout", "unknown"), 102)
        for now in range(103, 163):
            s.tick(motion(now, "holdout", "unknown"), now)
        s.tick(motion(162), 162)
        self.assertEqual(s.phase, "ready_settling")
        return s

    def bins(self, s, start, label, state="idle", count=241):
        for n in range(count):
            now = start + n / 4
            commands = s.tick(
                motion(now, label=label, state=state, bin_id=round(now * 4)), now
            )
        return commands, now

    def test_complete_sequence_uses_fresh_rf_support_and_includes_seated(self):
        s = self.ready()
        commands, now = self.bins(s, 164.25, 1)
        self.assertEqual(commands, ["LABEL_UNKNOWN"])
        self.assertEqual(s.pending_label, "walking")
        self.assertEqual(s.measurements["empty_control"]["bins"], 241)
        now += 0.25
        self.assertEqual(s.confirm("walking", now), ["LABEL_WALKING"])
        # No label ack means no walking measurements, even with fresh RF.
        s.tick(motion(now, label=0), now)
        self.assertNotIn("walking", s.counts)
        s.tick(dict(kind="recorded_label", label=2, time_us=round(now * 1e6)), now)
        # A window crossing the label boundary is excluded.
        s.tick(
            motion(now + 0.1, label=2, state="moving", bin_id=round(now * 4) + 1),
            now + 0.1,
        )
        self.assertNotIn("walking", s.counts)
        commands, now = self.bins(s, now + 0.5, 2, "moving")
        self.assertEqual(commands, ["LABEL_UNKNOWN"])
        self.assertEqual(s.pending_label, "seated")
        now += 0.25
        self.assertEqual(s.confirm("seated", now), ["LABEL_SEATED"])
        s.tick(dict(kind="recorded_label", label=3, time_us=round(now * 1e6)), now)
        commands, now = self.bins(s, now + 0.25, 3)
        self.assertEqual(commands, ["LABEL_UNKNOWN"])
        self.assertEqual(s.pending_label, "out")
        now += 0.25
        s.confirm("out", now)
        s.tick(dict(kind="recorded_label", label=1, time_us=round(now * 1e6)), now)
        for n in range(1, 41):
            t = now + n / 4
            s.tick(motion(t, label=1, state="moving", bin_id=round(t * 4)), t)
        self.assertNotIn("empty_return", s.counts)
        commands, now = self.bins(s, now + 10.25, 1)
        self.assertEqual(commands, ["END"])
        self.assertEqual(s.outcome, "labelled_sequence_complete")
        self.assertEqual(s.counts["seated"]["idle"], 241)
        self.assertEqual(s.counts["walking"]["moving"], 241)
        self.assertFalse(hasattr(s, "accuracy_qualified"))

    def test_rejection_threshold_change_and_empty_positive_stop(self):
        for change, reason in (
            (dict(phase="rejected", fault=10), "motion_rejected:10"),
            (dict(thresholds=(1, 2, 3, 5)), "thresholds_changed_after_background"),
            (dict(state="moving", bin_id=657), "independent_empty_control_failed"),
        ):
            s = self.ready()
            event = motion(164.25)
            event.update(change)
            if "bin_id" in event:
                event = motion(164.25, state="moving", bin_id=657)
            self.assertEqual(s.tick(event, 164.25), ["END"])
            self.assertEqual(s.outcome, reason)

    def test_silence_duplicates_and_unsolicited_or_late_human_labels(self):
        s = self.ready()
        with self.assertRaises(ValueError):
            s.confirm("walking", 162)
        self.assertEqual(s.tick(None, 164), ["END"])
        self.assertEqual(s.outcome, "recorded_motion_silent")
        s = self.ready()
        e = motion(164.25, bin_id=657)
        s.tick(e, 164.25)
        self.assertEqual(s.tick(e, 164.25), ["END"])
        self.assertEqual(s.outcome, "duplicate_or_reversed_bin")
        s = self.ready()
        _, now = self.bins(s, 164.25, 1)
        with self.assertRaises(ValueError):
            s.confirm("out", now)
        with self.assertRaises(ValueError):
            s.confirm("walking", now + 120)

    def test_no_training_without_recorded_initial_label_or_fresh_confirmation(self):
        with self.assertRaises(ValueError):
            M.Schedule(confirmed_empty=False)
        s = M.Schedule(confirmed_empty=True)
        s.tick(dict(kind="rf_trace_status", faults=0, gain_ready=True), 10)
        self.assertEqual(s.tick(None, 20), ["END"])
        self.assertEqual(s.outcome, "phase_deadline:settling")


class DecoderTests(unittest.TestCase):
    def line(self, kind, payload):
        w = [0x4C495343, 1, 0, 1000, 0, kind, len(payload), *payload]
        w.append(zlib.crc32(struct.pack("<" + "I" * len(w), *w)))
        return ("CSI_LAB " + "".join(f"{v:08x}" for v in w) + "\n").encode()

    def test_labels_and_checkpoint_semantics_with_crc(self):
        self.assertEqual(
            M.record_event(self.line(7, [2])),
            dict(kind="recorded_label", time_us=1000, label=2),
        )
        w = [1, 1, 0, 0, 0, 0, 1] + [0] * 25
        self.assertEqual(M.record_event(self.line(10, w))["phase"], "uncalibrated")
        for index, value in ((0, 2), (3, 1), (4, 11), (26, 1)):
            bad = w.copy()
            bad[index] = value
            with self.assertRaises(ValueError):
                M.record_event(self.line(10, bad))
        bad = bytearray(self.line(7, [2]))
        bad[8] = ord("0")
        with self.assertRaises(ValueError):
            M.record_event(bytes(bad))


if __name__ == "__main__":
    unittest.main()
