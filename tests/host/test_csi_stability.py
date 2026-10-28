"""Offline failure gates for existing vendor/recorded stability workloads."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("stability", ROOT / "tests/csi-radio/presence/stability.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def ready(case=S.Case.VENDOR):
    t = S.Trial(case, 30)
    for ms in range(0, 30000, 1000):
        t.observe(dict(ms=ms, training=0, rf_freshness="not_exported"))
    t.observe(dict(kind="numerical_probe", windows=100, bad_input=0, bad_output=0, kernel_errors=0))
    if case is S.Case.RECORDED:
        t.observe(dict(kind="lab_recorder_status", losses=0, rf_stack=1024, gain_stack=1024, compute_stack=1024))
        t.observe(dict(kind="model_owner_status", received=20, applied=20, pending=0))
    t.observe("PRESENCE_END ms=30000 reports=30 state=unknown resources=retained_until_reset")
    return t


def recording():
    return dict(footer=dict(losses=0), feature_events=20, native_outputs=20)


class StabilityTests(unittest.TestCase):
    def test_existing_modes_preserve_scope_and_do_not_promote_survival_to_presence(self):
        self.assertEqual(S.Case.VENDOR.options(), {})
        self.assertEqual(S.Case.RECORDED.options()["motion_case"], "temporal-motion-v1")
        for t, args in [(ready(), {}), (ready(S.Case.RECORDED), dict(recording=recording(), recorder_exit=0))]:
            result = t.finish(**args)
            self.assertFalse(result["presence_qualified"])
            self.assertFalse(result["cause_proven"])
            self.assertEqual(result["callback_retirement"], "whole_chip_reset_required")

    def test_missing_or_early_end_report_mismatch_and_no_native_work_fail(self):
        for field, value in [("end", None), ("end", (29999, 30)), ("end", (32001, 30)),
                             ("end", (30000, 31)), ("samples", 0), ("last_ms", 27999),
                             ("probe", None), ("probe", dict(windows=0))]:
            t = ready()
            setattr(t, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                t.finish()

    def test_truncated_recording_loss_unjoined_owner_and_missing_stack_fail(self):
        for change in ["footer", "loss", "process", "owner", "stack", "feature"]:
            t, r, code = ready(S.Case.RECORDED), recording(), 0
            if change == "footer": r["footer"] = None
            if change == "loss": r["footer"]["losses"] = 1
            if change == "process": code = 1
            if change == "owner": t.owner["pending"] = 1
            if change == "stack": t.stack["compute_stack"] = None
            if change == "feature": r["feature_events"] = 21
            with self.subTest(change=change), self.assertRaises(ValueError):
                t.finish(r, code)
        with self.assertRaises(ValueError):
            ready().finish(recording(), 0)

    def test_live_faults_training_reversal_and_recorder_loss_fail(self):
        cases = [dict(training=1, ms=1, rf_freshness="not_exported"),
                 dict(kind="numerical_probe", windows=1, bad_input=0, bad_output=1, kernel_errors=0),
                 dict(kind="numeric_failure"), dict(kind="lab_recorder_status", losses=1)]
        for event in cases:
            with self.subTest(event=event), self.assertRaises(ValueError):
                S.Trial(S.Case.RECORDED, 30).observe(event)
        t = S.Trial(S.Case.VENDOR, 30)
        t.observe(dict(ms=100, training=0, rf_freshness="not_exported"))
        with self.assertRaises(ValueError):
            t.observe(dict(ms=99, training=0, rf_freshness="not_exported"))
        t.observe(dict(kind="numerical_probe", windows=20, bad_input=0, bad_output=0, kernel_errors=0))
        with self.assertRaises(ValueError):
            t.observe(dict(kind="numerical_probe", windows=19, bad_input=0, bad_output=0, kernel_errors=0))

    def test_repeated_reports_cannot_hide_a_stalled_compute_counter(self):
        t = S.Trial(S.Case.VENDOR, 30)
        probe = dict(kind="numerical_probe", windows=1, bad_input=0, bad_output=0, kernel_errors=0)
        t.observe(dict(ms=0, training=0, rf_freshness="not_exported"))
        t.observe(probe)
        t.observe(dict(ms=2001, training=0, rf_freshness="not_exported"))
        with self.assertRaisesRegex(ValueError, "stalled"):
            t.observe(probe)
        t = S.Trial(S.Case.VENDOR, 30)
        t.observe(dict(ms=10000, training=0, rf_freshness="not_exported"))
        with self.assertRaisesRegex(ValueError, "stalled"):
            t.observe(dict(probe, windows=0))

    def test_ambiguous_control_duration_and_post_end_data_fail(self):
        for case, seconds in [("vendor_unrecorded", 30), (S.Case.VENDOR, True),
                              (S.Case.VENDOR, 29), (S.Case.VENDOR, 121)]:
            with self.assertRaises(ValueError): S.Trial(case, seconds)
        for event in ["PRESENCE_END ms=30000 reports=30 state=clear resources=retained_until_reset",
                      "PRESENCE_TRAIN_START status=0"]:
            with self.assertRaises(ValueError): S.Trial(S.Case.VENDOR, 30).observe(event)
        with self.assertRaises(ValueError): ready().observe(None)


if __name__ == "__main__":
    unittest.main()
