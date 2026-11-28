"""Negative controls for the evidence gate; these are not target evidence."""
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("time_sync_oracle_tested", HERE / "oracle.py")
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


def observation(name, request, elapsed):
    correction = ((request > 0) - (request < 0)) * min(abs(request) * 1000, elapsed // 2000)
    return dict(kind="observation", name=name, request_us=request, reference_ns=elapsed,
                monotonic_ns=elapsed, wall_ns=elapsed + correction, uncertainty_ns=220_000,
                reads=1_000_000, max_read_span_ns=10_000, max_pair_bound_ns=230_000)


def status(name, state, *, age=1_000_000, pending=0):
    return dict(kind="controller", name=name, state=state, initialized=True, source=0,
                age_ns=age, bound_ns=abs(pending) * 1000 + 350_000,
                pending_known=True, pending_us=pending)


def records(variant="controller"):
    rows = [dict(kind="begin", schema=1, variant=variant, tick_us=100),
            observation("baseline", 0, 250_000_000)]
    if variant == "controller":
        rows.append(dict(kind="hal_admission", slew_rate_ppm=500, exclusive=True, pre_epoch=True))
        for name, request in (("positive", 2000), ("negative", -2000)):
            rows.append(observation(name, request, 1_250_000_000))
            pending = 1375 if request > 0 else -1375
            rows.append(dict(kind="hal_pending", name=name, request_us=request,
                             elapsed_ns=1_250_000_000, pending_us=pending,
                             uncertainty_us=2, cancelled_us=pending))
        for request in (2_500_123, -2_500_123):
            rows.append(dict(kind="hal_range", request_us=request, pending_us=request,
                             rejected_unchanged=True))
        rows += [dict(kind="hal_lifecycle", reopened=True, drop_preserved=True,
                      set_cleared=True, reference_elapsed_ns=3_000_000_000),
                 status("seed", "Checking"), status("seed_checked", "Synchronized"),
                 status("controller_positive", "Adjusting", pending=2000),
                 observation("controller_positive", 2000, 4_500_000_000),
                 status("positive_settled", "Synchronized", age=4_500_000_000),
                 status("before_replacement", "Adjusting", age=1_000_000_000, pending=1500),
                 status("controller_negative", "Adjusting", pending=-2000),
                 observation("controller_negative", -2000, 4_500_000_000),
                 status("negative_settled", "Synchronized", age=4_500_000_000),
                 dict(kind="rejected", name="reordered", unchanged=True),
                 status("holdover", "Holdover", age=6_500_000_000),
                 status("refreshed", "Synchronized"), status("resumed", "Synchronized"),
                 dict(kind="lifecycle", old_epoch_rejected=True, controller_bytes=512,
                      reference_elapsed_ns=16_000_000_000)]
    rows.append(dict(kind="complete", schema=1, variant=variant,
                     reference_elapsed_ns=17_000_000_000 if variant == "controller" else 300_000_000))
    return rows


def transcript(rows):
    return "\n".join(oracle.PREFIX + json.dumps(row) for row in rows)


class OracleTests(unittest.TestCase):
    def test_complete_inventories(self):
        for variant in ("baseline", "controller"):
            self.assertTrue(oracle.analyze(transcript(records(variant)), variant)["qualified"])

    def test_incomplete_duplicate_or_wrong_variant(self):
        rows = records()
        for changed in (rows[:-1], rows + [rows[-1]], rows[1:]):
            with self.assertRaises(ValueError):
                oracle.analyze(transcript(changed), "controller")
        with self.assertRaises(ValueError):
            oracle.analyze(transcript(rows), "baseline")

    def test_wrong_measurement_and_quality_controls(self):
        changes = [
            (1, "monotonic_ns", 900_000_000),
            (1, "max_pair_bound_ns", 500_000),
            (1, "wall_ns", 260_000_000),
            (1, "reads", 1),
            (2, "exclusive", False),
            (4, "pending_us", 2000),
            (4, "uncertainty_us", 1000),
            (7, "rejected_unchanged", False),
            (9, "drop_preserved", False),
            (11, "bound_ns", 2_000_000),
            (11, "pending_us", 100),
            (11, "pending_known", False),
            (11, "source", 1),
            (12, "state", "Synchronized"),
            (12, "pending_us", -2000),
            (12, "bound_ns", 100_000),
            (14, "age_ns", 6_000_000_000),
            (15, "pending_us", 0),
            (16, "pending_us", -3500),
            (20, "age_ns", 12_000_000_000),
            (23, "old_epoch_rejected", False),
            (23, "controller_bytes", 1_000_000),
            (24, "reference_elapsed_ns", 60_000_000_000),
        ]
        for index, key, value in changes:
            with self.subTest(index=index, key=key, value=value):
                rows = records()
                rows[index][key] = value
                with self.assertRaises(ValueError):
                    oracle.analyze(transcript(rows), "controller")

    def test_types_and_extra_fields(self):
        for key, value in (("tick_us", True), ("schema", "1"), ("unrecorded", 1)):
            rows = records()
            rows[0][key] = value
            with self.assertRaises(ValueError):
                oracle.analyze(transcript(rows), "controller")
        rows = records()
        del rows[12]["pending_known"]
        with self.assertRaises(ValueError):
            oracle.analyze(transcript(rows), "controller")

    def test_panics_are_not_hidden_by_completion(self):
        for marker in ("PANIC", "panic:", "panicked at", "Assertion failed"):
            with self.assertRaises(ValueError):
                oracle.analyze(transcript(records()) + "\n" + marker, "controller")


if __name__ == "__main__":
    unittest.main()
