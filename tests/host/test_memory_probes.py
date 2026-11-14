"""Validate real memory-test transcripts, or run parser negative controls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest

ACQUISITION_PHASES = ("construction", "first-wait", "stop-join", "session-drop")
ACQUISITION_CASES = tuple(
    case
    for workers in ("one-worker", "two-workers")
    for case in (workers, *(f"{workers}-repeat-{cycle}" for cycle in range(1, 9)))
)

EXPECTED = {
    "std-demo": {
        **{case: {"setup", "first-use", "steady", "reclaimed"}
           for case in ("vec-bounded", "hashmap", "string")},
        "vec-dynamic": {"growth", "reclaimed"},
        "vec-reserved": {"setup", "reserved-fill", "growth", "reclaimed"},
        "fixed-array": {"steady"},
    },
    "ao-stress": {
        "caller-runtime": {"first-use", "reuse"},
        "owned-buffer": {"setup", "first-use", "steady", "teardown", "reclaimed"},
        **{case + "-lifecycle": {"first-run", "warmed-cycles"}
           for case in ("steady", "burst", "slow-consumer", "cpu-load")},
    },
    "acquisition": {
        case: {*ACQUISITION_PHASES, "reclaimed"} for case in ACQUISITION_CASES
    },
}
COUNTS = {"alloc", "zeroed", "realloc", "dealloc", "failed"}
SIZES = {"live_before", "live_after", "blocks_before", "blocks_after", "peak_process_requested_bytes"}
KEYS = COUNTS | SIZES | {"app", "case", "phase", "valid"}


def validate(text: str, app: str) -> list[dict]:
    if app not in EXPECTED:
        raise ValueError("unknown app")
    lines = text.replace("\r", "").splitlines()
    if "panicked" in text or "test result: FAILED" in text:
        raise ValueError("test failed")
    rows = []
    seen = set()
    for line in lines:
        if not line.startswith("MEMORY_RESULT "):
            continue
        row = json.loads(line.removeprefix("MEMORY_RESULT "))
        if (not isinstance(row, dict) or set(row) != KEYS
                or row["app"] != app or row["valid"] is not True
                or not isinstance(row["case"], str)
                or not isinstance(row["phase"], str)):
            raise ValueError("invalid report schema/app/validity")
        for key in COUNTS | SIZES:
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError("invalid unsigned counter")
        key = (row["case"], row["phase"])
        if key in seen:
            raise ValueError("duplicate phase")
        seen.add(key)
        if row["failed"] or row["peak_process_requested_bytes"] < max(row["live_before"], row["live_after"]):
            raise ValueError("failed allocation or impossible lifetime peak")
        if (row["blocks_after"] - row["blocks_before"]
                != row["alloc"] + row["zeroed"] - row["dealloc"]):
            raise ValueError("allocation calls do not balance live blocks")
        zero_window = row["phase"] in {"steady", "reserved-fill"} or (
            app == "std-demo" and row["phase"] == "first-use") or (
            app == "acquisition" and row["phase"] == "first-wait")
        reclaimed = row["phase"] in {"reclaimed", "warmed-cycles", "reuse"} or zero_window
        if zero_window and any(row[k] for k in COUNTS):
            raise ValueError("allocator activity in zero-call phase")
        if reclaimed and (row["live_after"] != row["live_before"] or row["blocks_after"] != row["blocks_before"]):
            raise ValueError("retained allocation at reclamation boundary")
        if row["phase"] == "growth" and row["realloc"] == 0:
            raise ValueError("missing observed Vec growth")
        rows.append(row)
    expected = {(case, phase) for case, phases in EXPECTED[app].items() for phase in phases}
    if seen != expected:
        raise ValueError(f"incomplete/unexpected phases: missing={expected - seen}, extra={seen - expected}")
    if app == "acquisition":
        validate_acquisition(rows)
    if lines.count(f"MEMORY_PASS app={app} cases={len(EXPECTED[app])}") != 1:
        raise ValueError("missing or duplicate completion")
    if lines.count(f"MEMORY_CONTROL app={app} kind=allocation-and-retention rejected=true") != 1:
        raise ValueError("missing observer negative control")
    return rows


def validate_acquisition(rows: list[dict]) -> None:
    """Check ownership boundaries without adopting observed byte counts as budgets."""
    records = {(row["case"], row["phase"]): row for row in rows}
    initial = records[ACQUISITION_CASES[0], "construction"]
    previous_peak = 0
    for case in ACQUISITION_CASES:
        phases = [records[case, phase] for phase in ACQUISITION_PHASES]
        construction, _, joined, dropped = phases
        reclaimed = records[case, "reclaimed"]
        if construction["peak_process_requested_bytes"] < previous_peak:
            raise ValueError("decreasing process peak across acquisition lifecycles")
        for previous, following in zip(phases, phases[1:]):
            if any(previous[f"{kind}_after"] != following[f"{kind}_before"]
                   for kind in ("live", "blocks")):
                raise ValueError("disconnected acquisition phase endpoints")
            if (following["peak_process_requested_bytes"]
                    < previous["peak_process_requested_bytes"]):
                raise ValueError("decreasing process peak across acquisition phases")
        if (construction["alloc"] + construction["zeroed"] == 0
                or any(construction[f"{kind}_after"] <= construction[f"{kind}_before"]
                       for kind in ("live", "blocks"))):
            raise ValueError("missing observed acquisition construction allocation")
        if any(joined[f"{kind}_after"] <= construction[f"{kind}_before"]
               for kind in ("live", "blocks")):
            raise ValueError("join was incorrectly treated as full session reclamation")
        if (any(dropped[key] for key in ("alloc", "zeroed", "realloc"))
                or dropped["dealloc"] == 0):
            raise ValueError("session drop must release retained allocations without allocating")
        if any(dropped[f"{kind}_after"] != construction[f"{kind}_before"]
               for kind in ("live", "blocks")):
            raise ValueError("session drop did not restore the acquisition baseline")
        if any(reclaimed[key] != sum(phase[key] for phase in phases) for key in COUNTS):
            raise ValueError("reclaimed summary does not cover all acquisition phases")
        if (any(reclaimed[f"{kind}_before"] != construction[f"{kind}_before"]
                or reclaimed[f"{kind}_after"] != dropped[f"{kind}_after"]
                for kind in ("live", "blocks"))
                or reclaimed["peak_process_requested_bytes"]
                != dropped["peak_process_requested_bytes"]):
            raise ValueError("reclaimed summary uses different acquisition snapshots")
        if any(construction[f"{kind}_before"] != initial[f"{kind}_before"]
               for kind in ("live", "blocks")):
            raise ValueError("acquisition moved its fixed baseline")
        previous_peak = dropped["peak_process_requested_bytes"]


def synthetic_acquisition_rows() -> list[dict]:
    """Invented balanced counters for checker tests, never measurement evidence."""
    rows = []
    for case in ACQUISITION_CASES:
        workers = 2 if case.startswith("two-workers") else 1
        baseline_live, baseline_blocks = 100, 2
        peak = baseline_live + 256 * workers
        values = (
            ("construction", 4 * workers, 0,
             baseline_live, peak, baseline_blocks, baseline_blocks + 4 * workers),
            ("first-wait", 0, 0,
             peak, peak, baseline_blocks + 4 * workers, baseline_blocks + 4 * workers),
            ("stop-join", 0, 2 * workers,
             peak, baseline_live + 64 * workers,
             baseline_blocks + 4 * workers, baseline_blocks + 2 * workers),
            ("session-drop", 0, 2 * workers,
             baseline_live + 64 * workers, baseline_live,
             baseline_blocks + 2 * workers, baseline_blocks),
            ("reclaimed", 4 * workers, 4 * workers,
             baseline_live, baseline_live, baseline_blocks, baseline_blocks),
        )
        for phase, alloc, dealloc, live_before, live_after, blocks_before, blocks_after in values:
            row = dict.fromkeys(COUNTS | SIZES, 0)
            row.update(app="acquisition", case=case, phase=phase, valid=True,
                       alloc=alloc, dealloc=dealloc, live_before=live_before,
                       live_after=live_after, blocks_before=blocks_before,
                       blocks_after=blocks_after, peak_process_requested_bytes=peak)
            rows.append(row)
    return rows


def synthetic(app: str) -> str:
    """Only a parser test fixture, not benchmark/measurement evidence."""
    lines = [f"MEMORY_CONTROL app={app} kind=allocation-and-retention rejected=true"]
    if app == "acquisition":
        for row in synthetic_acquisition_rows():
            lines.append("MEMORY_RESULT " + json.dumps(row))
    else:
        for case, phases in EXPECTED[app].items():
            for phase in sorted(phases):
                row = dict.fromkeys(COUNTS | SIZES, 0)
                row.update(app=app, case=case, phase=phase, valid=True)
                if phase == "growth":
                    row["realloc"] = 1
                lines.append("MEMORY_RESULT " + json.dumps(row))
    lines.append(f"MEMORY_PASS app={app} cases={len(EXPECTED[app])}")
    return "\n".join(lines)


class MemoryReportTests(unittest.TestCase):
    def test_complete_schema(self):
        self.assertEqual(len(validate(synthetic("std-demo"), "std-demo")), 19)
        self.assertEqual(len(validate(synthetic("ao-stress"), "ao-stress")), 15)
        self.assertEqual(len(validate(synthetic("acquisition"), "acquisition")), 90)

    @staticmethod
    def acquisition_transcript(rows: list[dict]) -> str:
        return "\n".join([
            "MEMORY_CONTROL app=acquisition kind=allocation-and-retention rejected=true",
            *("MEMORY_RESULT " + json.dumps(row) for row in rows),
            "MEMORY_PASS app=acquisition cases=18",
        ])

    def test_success_marker_is_not_evidence(self):
        with self.assertRaises(ValueError):
            validate("MEMORY_PASS app=std-demo cases=6", "std-demo")

    def test_missing_duplicate_and_wrong_app(self):
        text = synthetic("std-demo")
        result = next(line for line in text.splitlines() if line.startswith("MEMORY_RESULT "))
        for changed in [text.replace(result, ""), text + "\n" + result,
                        text.replace('"app": "std-demo"', '"app": "ao-stress"')]:
            with self.subTest(changed=changed[:60]), self.assertRaises(ValueError):
                validate(changed, "std-demo")

    def test_activity_leak_invalid_and_bad_numeric_values(self):
        text = synthetic("std-demo")
        original = next(line for line in text.splitlines()
                        if '"phase": "steady"' in line)
        for key, value in [("alloc", 1), ("live_after", 1), ("blocks_after", 1),
                           ("valid", False), ("dealloc", -1), ("zeroed", True)]:
            row = json.loads(original.removeprefix("MEMORY_RESULT "))
            row[key] = value
            changed = text.replace(original, "MEMORY_RESULT " + json.dumps(row))
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(changed, "std-demo")

    def test_caller_runtime_reuse_cannot_hide_continuing_retention(self):
        text = synthetic("ao-stress")
        original = next(line for line in text.splitlines() if '"phase": "reuse"' in line)
        row = json.loads(original.removeprefix("MEMORY_RESULT "))
        row.update(live_after=48, blocks_after=1, peak_process_requested_bytes=48)
        with self.assertRaises(ValueError):
            validate(text.replace(original, "MEMORY_RESULT " + json.dumps(row)), "ao-stress")

    def test_missing_control_and_failure_after_success(self):
        for app in ("ao-stress", "acquisition"):
            text = synthetic(app)
            for changed in ["\n".join(text.splitlines()[1:]), text + "\nthread panicked"]:
                with self.subTest(app=app), self.assertRaises(ValueError):
                    validate(changed, app)

    def test_acquisition_requires_each_boundary_and_repeat(self):
        rows = synthetic_acquisition_rows()
        for changed in [
            [row for row in rows if not (row["case"] == "one-worker"
                                        and row["phase"] == "session-drop")],
            [row for row in rows if row["case"] != "two-workers-repeat-8"],
            [*rows, rows[0]],
        ]:
            with self.subTest(records=len(changed)), self.assertRaises(ValueError):
                validate(self.acquisition_transcript(changed), "acquisition")

    def test_acquisition_rejects_zero_work_transcript(self):
        rows = synthetic_acquisition_rows()
        for row in rows:
            row.update(dict.fromkeys(COUNTS | SIZES, 0))
        with self.assertRaisesRegex(ValueError, "construction allocation"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_first_wait_must_not_allocate(self):
        rows = synthetic_acquisition_rows()
        first_wait = next(row for row in rows if row["phase"] == "first-wait")
        first_wait.update(alloc=1, dealloc=1)
        with self.assertRaisesRegex(ValueError, "zero-call phase"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_rejects_disconnected_endpoints(self):
        for kind in ("live", "blocks"):
            rows = synthetic_acquisition_rows()
            joined = next(row for row in rows if row["phase"] == "stop-join")
            joined[f"{kind}_before"] -= 1
            joined[f"{kind}_after"] -= 1
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "disconnected"):
                validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_join_is_not_full_session_reclamation(self):
        rows = synthetic_acquisition_rows()
        joined = next(row for row in rows if row["phase"] == "stop-join")
        dropped = next(row for row in rows if row["phase"] == "session-drop")
        joined.update(dealloc=4, live_after=100, blocks_after=2)
        dropped.update(dealloc=0, live_before=100, blocks_before=2)
        with self.assertRaisesRegex(ValueError, "join was incorrectly treated"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_drop_cannot_allocate(self):
        rows = synthetic_acquisition_rows()
        dropped = next(row for row in rows if row["phase"] == "session-drop")
        reclaimed = next(row for row in rows if row["phase"] == "reclaimed")
        for row in (dropped, reclaimed):
            row["alloc"] += 1
            row["dealloc"] += 1
        with self.assertRaisesRegex(ValueError, "without allocating"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_drop_must_release_bytes_and_blocks(self):
        for kind in ("live", "blocks"):
            rows = synthetic_acquisition_rows()
            for row in rows:
                if row["case"] == "one-worker" and row["phase"] in {"session-drop", "reclaimed"}:
                    row[f"{kind}_after"] += 1
                    if kind == "blocks":
                        row["dealloc"] -= 1
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "retained allocation"):
                validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_summary_cannot_omit_phase_activity(self):
        rows = synthetic_acquisition_rows()
        reclaimed = next(row for row in rows if row["phase"] == "reclaimed")
        reclaimed["alloc"] -= 1
        reclaimed["dealloc"] -= 1
        with self.assertRaisesRegex(ValueError, "does not cover all"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_summary_cannot_use_different_snapshots(self):
        rows = synthetic_acquisition_rows()
        reclaimed = next(row for row in rows if row["phase"] == "reclaimed")
        reclaimed["live_before"] += 1
        reclaimed["live_after"] += 1
        with self.assertRaisesRegex(ValueError, "different acquisition snapshots"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_repetition_cannot_move_its_baseline(self):
        rows = synthetic_acquisition_rows()
        for row in rows:
            if row["case"] == "one-worker-repeat-8":
                row["live_before"] += 7
                row["live_after"] += 7
                row["blocks_before"] += 1
                row["blocks_after"] += 1
                row["peak_process_requested_bytes"] += 7
        with self.assertRaisesRegex(ValueError, "moved its fixed baseline"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_cardinality_cannot_move_the_process_baseline(self):
        rows = synthetic_acquisition_rows()
        for row in rows:
            if row["case"].startswith("two-workers"):
                row["live_before"] += 7
                row["live_after"] += 7
                row["blocks_before"] += 1
                row["blocks_after"] += 1
                row["peak_process_requested_bytes"] += 7
        with self.assertRaisesRegex(ValueError, "moved its fixed baseline"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_calls_must_balance_live_blocks(self):
        rows = synthetic_acquisition_rows()
        rows[0]["alloc"] += 1
        with self.assertRaisesRegex(ValueError, "do not balance live blocks"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_process_peak_cannot_decrease_between_phases(self):
        rows = synthetic_acquisition_rows()
        rows[0]["peak_process_requested_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "decreasing process peak"):
            validate(self.acquisition_transcript(rows), "acquisition")

    def test_acquisition_process_peak_cannot_decrease_between_lifecycles(self):
        rows = synthetic_acquisition_rows()
        for row in rows:
            # Both values remain above every live-byte snapshot. Only the
            # process-lifetime monotonicity between cycles is violated.
            row["peak_process_requested_bytes"] += (
                1 if row["case"] == "one-worker-repeat-4" else 2)
        with self.assertRaisesRegex(ValueError, "across acquisition lifecycles"):
            validate(self.acquisition_transcript(rows), "acquisition")


if __name__ == "__main__":
    if "--app" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--app", required=True, choices=EXPECTED)
        parser.add_argument("--log", required=True, type=Path)
        args = parser.parse_args()
        rows = validate(args.log.read_text(), args.app)
        print(f"MEMORY_TRANSCRIPT PASS app={args.app} records={len(rows)}")
    else:
        unittest.main()
