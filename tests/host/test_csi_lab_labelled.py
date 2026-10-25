"""Declared recorder trial gates, independent of real time and hardware."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "csi_labelled_schedule", ROOT / "tests/csi-lab/labelled.py"
)
LAB = importlib.util.module_from_spec(spec)
spec.loader.exec_module(LAB)


def corrected(phase, state="unknown", **changes):
    return dict(
        kind="corrected_calibration",
        phase=phase,
        state=state,
        rejection="none",
        features=512,
        enter=0.4,
        exit=0.3,
        background_min=0.01,
        background_max=0.2,
        **changes
    )


class LabelledTests(unittest.TestCase):
    def prepared(self):
        s = LAB.Schedule()
        self.assertEqual(s.tick(None, 10), [])  # No gain readiness; never train yet.
        self.assertEqual(
            s.tick(dict(kind="rf_trace_status", faults=0, gain_ready=True), 11),
            ["TRAIN_START"],
        )
        s.tick("PRESENCE_TRAIN_START ms=11000", 12)
        self.assertEqual(s.tick(None, 71), [])
        self.assertEqual(s.tick(None, 72), ["TRAIN_STOP"])
        s.tick("PRESENCE_TRAIN_STOP ms=72000 status=0 wander=0.1 jitter=0.1", 73)
        self.assertEqual(
            s.tick(
                dict(
                    training=0,
                    calibrated=1,
                    ready=1,
                    templates=10,
                    rf_freshness="not_exported",
                ),
                74,
            ),
            ["CALIBRATE_BACKGROUND"],
        )
        s.tick("PRESENCE_CALIBRATION_CONTROL phase=background", 75)
        s.tick(corrected("background"), 105)
        self.assertEqual(s.tick(None, 106), ["CALIBRATE_VALIDATE"])
        s.tick("PRESENCE_CALIBRATION_CONTROL phase=holdout", 107)
        s.tick(corrected("holdout"), 166)
        self.assertEqual(s.tick(None, 167), ["CALIBRATE_ACCEPT"])
        s.tick("PRESENCE_CALIBRATION_CONTROL phase=ready", 168)
        s.tick(corrected("ready", "not_detected"), 169)
        return s

    def ready(self):
        s = self.prepared()
        for n in range(10):
            s.tick(corrected("ready", "not_detected"), 170 + n)
        self.assertEqual(s.tick(None, 199), ["LABEL_UNKNOWN"])
        self.assertEqual(s.pending_label, "walking")
        return s

    def test_full_trial_has_independent_human_handoffs_and_no_automatic_labels(self):
        s = self.ready()
        self.assertEqual(s.tick(None, 250), [])
        self.assertEqual(s.phase, "await_walking")
        self.assertEqual(s.confirm("walking", 251), ["LABEL_WALKING"])
        for n in range(10):
            s.tick(corrected("ready", "presence_hint"), 252 + n)
        self.assertEqual(s.tick(None, 311), ["LABEL_UNKNOWN"])
        self.assertEqual(s.pending_label, "out")
        self.assertEqual(s.confirm("out", 312), ["LABEL_EMPTY"])
        for n in range(10):
            s.tick(corrected("ready", "not_detected"), 313 + n)
        self.assertEqual(s.tick(None, 372), ["END"])
        self.assertEqual(s.outcome, "labelled_sequence_complete")
        self.assertEqual(s.counts["walking"]["presence_hint"], 10)
        self.assertIsNone(s.pending_label)
        self.assertEqual(s.tick(None, 590), [])

    def test_rejected_calibration_ends_recording_without_authorizing_movement(self):
        s = self.prepared()
        event = corrected("rejected")
        event["rejection"] = "holdout_exceeded"
        self.assertEqual(s.tick(event, 170), ["END"])
        self.assertEqual(s.outcome, "calibration_rejected:holdout_exceeded")
        self.assertIsNone(s.pending_label)
        with self.assertRaises(ValueError):
            s.confirm("walking", 171)

    def test_empty_control_cannot_pass_false_positive_or_unavailable(self):
        for event in (
            corrected("ready", "presence_hint"),
            dict(kind="corrected_unavailable"),
        ):
            s = self.prepared()
            for n in range(10):
                s.tick(corrected("ready", "not_detected"), 170 + n)
            s.tick(event, 180)
            self.assertEqual(s.tick(None, 199), ["END"])
            self.assertEqual(s.outcome, "empty_control_failed_or_unavailable")
            self.assertIsNone(s.pending_label)

    def test_frozen_threshold_or_envelope_change_ends_trial(self):
        for field in ("enter", "exit", "background_min", "background_max"):
            s = self.prepared()
            event = corrected("ready", "not_detected")
            event[field] += 0.01
            self.assertEqual(s.tick(event, 170), ["END"])
            self.assertEqual(s.outcome, "thresholds_changed_after_background")

    def test_loss_stack_and_native_faults_are_never_clean_room_measurements(self):
        events = [
            dict(kind="numeric_failure"),
            dict(
                kind="lab_recorder_status",
                losses=1,
                rf_stack=1000,
                gain_stack=1000,
                compute_stack=1000,
            ),
            dict(
                kind="lab_recorder_status",
                losses=0,
                rf_stack=1000,
                gain_stack=1000,
                compute_stack=511,
            ),
            dict(kind="rf_trace_status", faults=1, gain_ready=True),
        ]
        for event in events:
            s = self.ready()
            self.assertEqual(s.tick(event, 200), ["END"])
            self.assertNotEqual(s.outcome, "labelled_sequence_complete")
            self.assertIsNone(s.pending_label)

    def test_wrong_unsolicited_late_and_nonmonotonic_human_confirmations_fail(self):
        s = LAB.Schedule()
        with self.assertRaises(ValueError):
            s.confirm("walking", 1)
        s = self.ready()
        with self.assertRaises(ValueError):
            s.confirm("out", 200)
        with self.assertRaises(ValueError):
            s.confirm("walking", 319)
        s = self.ready()
        self.assertEqual(s.tick(None, 319), ["END"])
        self.assertEqual(s.outcome, "human_confirmation_timeout")
        for now in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                LAB.Schedule().tick(None, now)

    def test_acknowledgement_and_overall_deadlines_cannot_extend_indefinitely(self):
        s = LAB.Schedule()
        self.assertEqual(s.tick(None, 20), ["END"])
        self.assertEqual(s.outcome, "command_or_readiness_deadline")
        s = self.ready()
        self.assertEqual(s.tick(None, 590), ["END"])
        self.assertEqual(s.outcome, "overall_deadline")

    def test_missing_minimum_features_never_attempts_holdout_admission(self):
        s = LAB.Schedule()
        s.phase = "background"
        s.since = 0
        event = corrected("background")
        event["features"] = 127
        self.assertEqual(s.tick(event, 31), ["END"])
        self.assertEqual(s.outcome, "insufficient_calibration_features")

    def test_walking_miss_and_return_unknown_remain_measurements_not_success(self):
        s = self.ready()
        s.confirm("walking", 200)
        for n in range(10):
            s.tick(corrected("ready", "not_detected"), 201 + n)
        s.tick(None, 260)
        s.confirm("out", 261)
        for n in range(10):
            s.tick(dict(kind="corrected_unavailable"), 262 + n)
        self.assertEqual(s.tick(None, 321), ["END"])
        self.assertEqual(s.outcome, "labelled_sequence_complete")
        self.assertEqual(s.counts["walking"]["presence_hint"], 0)
        self.assertEqual(s.counts["empty_return"]["unknown"], 10)
