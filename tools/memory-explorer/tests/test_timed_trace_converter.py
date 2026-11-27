"""Timed-capture oracles: silent time, censoring, boundaries and retention loss."""

from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "timed_trace_converter", Path(__file__).resolve().parents[2] / "nuttx-trace-to-perfetto.py")
CONVERTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONVERTER)


def row(ns, body, cpu=0):
    return f"gnss-7 [{cpu}] {ns // 1_000_000_000}.{ns % 1_000_000_000:09}: {body}"


def framed(rows, **changes):
    begin = dict(schema=3, source="nuttx-noteram-ascii", clock="nuttx-perf",
                 recording_id="timed-fixture-1", cpus=1, mode=0, notes_lost=0,
                 capture_start_ns=10_000_000_000, capture_end_ns=12_000_000_000,
                 requested_duration_ns=2_000_000_000, stop_reason="duration",
                 categories="app", buffer_bytes=32768)
    end = dict(schema=3, recording_id="timed-fixture-1", mode=0, notes_lost=0, complete=1)
    for key, value in changes.items():
        if key.startswith("end_"):
            end[key[4:]] = value
        else:
            begin[key] = value
    return ["NXRS_NOTE_BEGIN " + " ".join(f"{key}={value}" for key, value in begin.items()),
            *rows,
            "NXRS_NOTE_END " + " ".join(f"{key}={value}" for key, value in end.items())]


def metadata(trace):
    return trace["otherData"]["nxrs"]


class TimedCaptureConversionTests(unittest.TestCase):
    def test_sparse_one_hz_updates_keep_two_seconds_including_quiet_edges(self):
        trace = CONVERTER.convert(framed([
            row(10_250_000_000, "tracing_mark_write: I|7|gnss/primary/fix_received"),
            row(11_250_000_000, "tracing_mark_write: I|7|gnss/primary/fix_received"),
        ]))
        info = metadata(trace)
        self.assertEqual(info["origin_ns"], 10_000_000_000)
        self.assertEqual(info["observed_start_ns"], 10_250_000_000)
        self.assertEqual(info["observed_end_ns"], 11_250_000_000)
        self.assertEqual(info["capture_window"], {
            "start_ns": 10_000_000_000, "end_ns": 12_000_000_000,
            "requested_duration_ns": 2_000_000_000, "stop_reason": "duration",
            "complete": True, "categories": ["app"],
        })
        observations = [item for item in trace["traceEvents"] if item["ph"] == "i"]
        self.assertEqual([item["ts"] for item in observations], [250_000, 1_250_000])
        # The observed occurrence rate is 2 / 2 seconds, not 2 / 1 second.
        seconds = (info["capture_window"]["end_ns"] - info["origin_ns"]) / 1_000_000_000
        self.assertEqual(len(observations) / seconds, 1)

    def test_context_notes_are_excluded_with_half_open_end(self):
        trace = CONVERTER.convert(framed([
            row(9_999_999_999, "tracing_mark_write: I|7|before"),
            row(10_000_000_000, "tracing_mark_write: I|7|first"),
            row(11_999_999_999, "tracing_mark_write: I|7|last"),
            row(12_000_000_000, "tracing_mark_write: I|7|after"),
        ]))
        observations = [item for item in trace["traceEvents"] if item["ph"] == "i"]
        self.assertEqual([item["name"] for item in observations], ["first", "last"])
        self.assertEqual(observations[-1]["ts"], Decimal("1999999.999"))
        self.assertEqual(metadata(trace)["context_note_count"], 2)
        self.assertEqual(metadata(trace)["observed_note_count"], 2)

    def test_boundary_censoring_does_not_claim_retention_loss_or_shorten_calls(self):
        trace = CONVERTER.convert(framed([
            row(9_900_000_000, "tracing_mark_write: B|7|cross_left"),
            row(10_100_000_000, "tracing_mark_write: E|7|cross_left"),
            row(10_500_000_000, "tracing_mark_write: B|7|complete"),
            row(10_502_000_000, "tracing_mark_write: E|7|complete"),
            row(11_900_000_000, "tracing_mark_write: B|7|cross_right"),
            row(12_100_000_000, "tracing_mark_write: E|7|cross_right"),
        ]))
        spans = [item for item in trace["traceEvents"] if item["ph"] == "X"]
        self.assertEqual([(item["name"], item["dur"]) for item in spans], [("complete", 2000)])
        info = metadata(trace)
        self.assertEqual(info["incomplete_intervals"], 2)
        self.assertFalse(info["complete"])
        self.assertTrue(info["capture_window"]["complete"])
        self.assertFalse(info["loss_detected"])
        self.assertEqual(info["lost_records"], 0)

    def test_an_empty_measured_window_remains_an_empty_two_second_recording(self):
        trace = CONVERTER.convert(framed([], categories="none"))
        info = metadata(trace)
        self.assertTrue(info["capture_window"]["complete"])
        self.assertEqual(info["capture_window"]["categories"], [])
        self.assertEqual(info["observed_note_count"], 0)
        self.assertIsNone(info["observed_start_ns"])
        self.assertIsNone(info["observed_end_ns"])
        self.assertTrue(all(item["ph"] == "M" for item in trace["traceEvents"]))
        with self.assertRaisesRegex(ValueError, "no timestamped"):
            CONVERTER.convert([])

    def test_overflow_keeps_requested_window_but_denies_complete_coverage(self):
        trace = CONVERTER.convert(framed([
            row(10_250_000_000, "tracing_mark_write: B|7|parse"),
            row(10_252_000_000, "tracing_mark_write: E|7|parse"),
        ], mode=2, notes_lost=1, end_mode=2, end_notes_lost=1))
        info = metadata(trace)
        self.assertFalse(info["capture_window"]["complete"])
        self.assertTrue(info["loss_detected"])
        self.assertIsNone(info["lost_records"], "boolean overflow is not a lost-record count")
        self.assertEqual(info["capture_window"]["end_ns"], 12_000_000_000)
        self.assertFalse(any(item["ph"] == "X" for item in trace["traceEvents"]))

    def test_partial_dump_and_explicit_loss_cannot_claim_full_coverage(self):
        rows = [row(10_250_000_000, "tracing_mark_write: I|7|fix")]
        for capture in (framed(rows, end_complete=0), framed([*rows, "LOST_EVENTS count=3"])):
            with self.subTest(capture=capture):
                self.assertFalse(metadata(CONVERTER.convert(capture))["capture_window"]["complete"])
        with self.assertRaisesRegex(ValueError, "completeness"):
            CONVERTER.convert(framed(rows, end_complete=0), complete=True)

    def test_interrupted_window_reports_actual_shorter_duration(self):
        trace = CONVERTER.convert(framed([], capture_end_ns=10_800_000_000,
                                        stop_reason="interrupted"))
        window = metadata(trace)["capture_window"]
        self.assertEqual(window["requested_duration_ns"], 2_000_000_000)
        self.assertEqual(window["end_ns"] - window["start_ns"], 800_000_000)
        self.assertTrue(window["complete"], "an early stop can retain its entire actual window")

    def test_large_source_epoch_preserves_single_nanoseconds(self):
        start = (1 << 53) + 1
        trace = CONVERTER.convert(framed([
            row(start + 123_456_789, "tracing_mark_write: I|7|fix"),
        ], capture_start_ns=start, capture_end_ns=start + 2_000_000_000))
        decoded = json.loads(CONVERTER.encode_trace(trace), parse_float=Decimal)
        event = next(item for item in decoded["traceEvents"] if item["ph"] == "i")
        self.assertEqual(event["ts"] * 1000, 123_456_789)
        self.assertEqual(metadata(decoded)["capture_window"]["start_ns"], start)

    def test_invalid_capture_contracts_are_rejected(self):
        for changes in (
            {"capture_end_ns": 10_000_000_000},
            {"capture_end_ns": 11_999_999_999},
            {"capture_start_ns": -1},
            {"capture_end_ns": 1 << 64},
            {"requested_duration_ns": 0},
            {"requested_duration_ns": 30_000_000_001},
            {"stop_reason": "buffer_is_probably_fine"},
            {"categories": "app,app"},
            {"categories": "all"},
            {"buffer_bytes": 0},
            {"cpus": 2},
            {"mode": 1, "end_mode": 1},
            {"recording_id": ""},
            {"clock": ""},
            {"end_recording_id": "different"},
            {"capture_mode": "stopped-checkpoints"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                CONVERTER.convert(framed([], **changes))

    def test_foreign_cpu_and_truncated_envelope_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "another CPU"):
            CONVERTER.convert(framed([row(10_500_000_000, "tracing_mark_write: I|7|fix", cpu=1)]))
        with self.assertRaisesRegex(ValueError, "ordered"):
            CONVERTER.convert(framed([])[:-1])


if __name__ == "__main__":
    unittest.main()
