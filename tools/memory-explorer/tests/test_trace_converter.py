"""Independent event/clock/loss witnesses for the NuttX trace converter."""

from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "nuttx-trace-to-perfetto.py"
SPEC = importlib.util.spec_from_file_location("nuttx_trace_to_perfetto", SCRIPT)
CONVERTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONVERTER)


def row(ns, body, cpu=0, task="worker", pid=7):
    return f"{task}-{pid} [{cpu}] {ns // 1_000_000_000}.{ns % 1_000_000_000:09}: {body}"


def switch(before, after, name="worker"):
    return (f"sched_switch: prev_comm=prev prev_pid={before} prev_prio=100 prev_state=R "
            f"==> next_comm={name} next_pid={after} next_prio=100")


def framed(rows, *, lost=0, mode=0, complete=1):
    return [f"NXRS_NOTE_BEGIN schema=1 source=nuttx-noteram-ascii clock=perf_gettime-converted cpus=1 mode={mode} notes_lost={lost}",
            *rows, f"NXRS_NOTE_END schema=1 mode={mode} notes_lost={lost} complete={complete}"]


def intervals(trace, category=None):
    return [event for event in trace["traceEvents"] if event["ph"] == "X"
            and (category is None or category in event["cat"].split(","))]


def call_request(call_id, operation="malloc", *, heap="0xa", size=32,
                 caller="0x101", old="0x0", alignment=0):
    return (f"heap_request: {operation} heap: {heap} id:{call_id} size:{size} "
            f"caller: {caller} old: {old} align:{alignment}")


def call_result(call_id, operation="malloc", *, heap="0xa", result="0x4000", outcome="success"):
    return f"heap_result: {operation} heap: {heap} id:{call_id} result: {result} outcome:{outcome}"


def call_framed(rows, **options):
    capture = framed(rows, **options)
    capture[0] += " recording_id=identified-allocator-test-run"
    capture[-1] += " recording_id=identified-allocator-test-run"
    return capture


class TraceConversionTests(unittest.TestCase):
    def test_cpu_and_task_residency_have_exact_observed_switch_endpoints(self):
        trace = CONVERTER.convert([
            row(1_000_000_001, switch(0, 7)),
            row(1_000_000_012, switch(7, 0, "idle")),
            row(1_000_000_099, "sched_waking: comm=worker pid=7 target_cpu=0"),
        ])
        spans = intervals(trace)
        self.assertEqual(len(spans), 2)  # CPU and per-task representations of the same residency.
        self.assertTrue(all(event["ts"] == 0 and event["dur"] == Decimal("0.011") for event in spans))
        self.assertTrue(all("IRQ" in event["args"]["accounting"] for event in spans))
        metadata = trace["otherData"]["nxrs"]
        self.assertIsNone(metadata["clock"])
        self.assertIsNone(metadata["source"])
        self.assertIsNone(metadata["lost_records"])
        self.assertEqual(metadata["incomplete_intervals"], 3)  # Two residency edges and an undispatched wake.
        # The trailing idle residency is not extended to an unrelated wake record.
        self.assertFalse(any(event["args"].get("task_id") == 0 for event in spans))

    def test_nested_irqs_use_cpu_stack_not_independent_irq_pairing(self):
        trace = CONVERTER.convert([
            row(1000, "irq_handler_entry: irq=17 name=outer"),
            row(1010, "irq_handler_entry: irq=8 name=nested"),
            row(1015, "irq_handler_exit: irq=8 ret=handled"),
            row(1020, "irq_handler_exit: irq=17 ret=handled"),
        ])
        spans = intervals(trace, "irq")
        self.assertEqual([event["dur"] for event in spans], [Decimal("0.020"), Decimal("0.005")])
        self.assertEqual([event["args"]["nesting_depth"] for event in spans], [0, 1])
        self.assertEqual(len({event["tid"] for event in spans}), 1)

    def test_crossed_irq_exits_and_unclosed_entries_have_no_invented_duration(self):
        trace = CONVERTER.convert([
            row(100, "irq_handler_entry: irq=17 name=outer"),
            row(110, "irq_handler_entry: irq=8 name=nested"),
            row(120, "irq_handler_exit: irq=17 ret=handled"),
            row(130, "irq_handler_exit: irq=8 ret=handled"),
            row(140, "irq_handler_entry: irq=9 name=never-ended"),
        ])
        self.assertEqual(intervals(trace), [])
        markers = [event for event in trace["traceEvents"] if event["ph"] == "i"]
        self.assertTrue(all(event["args"]["duration_unknown"] for event in markers))
        self.assertFalse(trace["otherData"]["nxrs"]["complete"])

    def test_application_scopes_instants_and_counters(self):
        trace = CONVERTER.convert([
            row(10, "tracing_mark_write: B|7|frame"),
            row(20, "tracing_mark_write: B|7|i2c.service"),
            row(30, "tracing_mark_write: E|7|i2c.service"),
            row(40, "tracing_mark_write: I|7|ready"),
            row(45, "tracing_mark_write: C|7|bytes|9007199254740993"),
            row(50, "tracing_mark_write: E|7|frame"),
        ])
        self.assertEqual([event["name"] for event in intervals(trace, "app")], ["frame", "i2c.service"])
        counters = [event for event in trace["traceEvents"] if event["ph"] == "C"]
        encoded = CONVERTER.encode_trace(trace)
        reparsed = json.loads(encoded, parse_float=Decimal)
        self.assertEqual(counters[0]["args"]["value"], Decimal("9007199254740993"))
        self.assertTrue(any(event.get("name") == "ready" and event["ph"] == "i" for event in reparsed["traceEvents"]))

    def test_large_absolute_origin_and_duration_retain_individual_nanoseconds(self):
        origin = (1 << 53) + 1
        duration = (1 << 53) + 3
        trace = CONVERTER.convert([
            row(origin, "irq_handler_entry: irq=1 name=long"),
            row(origin + duration, "irq_handler_exit: irq=1 ret=handled"),
        ])
        encoded = CONVERTER.encode_trace(trace)
        reparsed = json.loads(encoded, parse_float=Decimal)
        span = intervals(reparsed)[0]
        self.assertEqual(span["ts"], 0)
        self.assertEqual(span["dur"] * 1000, duration)
        self.assertEqual(reparsed["otherData"]["nxrs"]["origin_ns"], origin)

    def test_counter_exponents_remain_compact_and_exact_in_json(self):
        trace = CONVERTER.convert([
            row(1, "tracing_mark_write: C|7|small|1e-1000000000"),
        ])
        encoded = CONVERTER.encode_trace(trace)
        self.assertLess(len(encoded), 4096)
        reparsed = json.loads(encoded, parse_float=Decimal)
        counter = next(event for event in reparsed["traceEvents"] if event["ph"] == "C")
        self.assertEqual(counter["args"]["value"], Decimal("1e-1000000000"))
        self.assertNotEqual(counter["args"]["value"], 0)

    def test_global_boolean_loss_disables_ambiguous_duration_reconstruction(self):
        rows = [row(10, switch(0, 7)), row(20, switch(7, 0)),
                row(30, "irq_handler_entry: irq=1 name=timer"),
                row(40, "irq_handler_exit: irq=1 ret=handled")]
        trace = CONVERTER.convert(framed(rows, lost=1, mode=2))
        self.assertEqual(intervals(trace), [])
        metadata = trace["otherData"]["nxrs"]
        self.assertTrue(metadata["loss_detected"])
        self.assertIsNone(metadata["lost_records"])  # Boolean overflow is not an exact count.
        self.assertFalse(metadata["complete"])
        self.assertTrue(any("disabled" in warning for warning in metadata["warnings"]))

    def test_explicit_loss_boundary_resets_pairs_and_allows_later_complete_pairs(self):
        trace = CONVERTER.convert([
            row(10, "irq_handler_entry: irq=1 name=before-gap"),
            "LOST_EVENTS count=4",
            row(20, "irq_handler_exit: irq=1 ret=handled"),
            row(30, "irq_handler_entry: irq=1 name=after-gap"),
            row(40, "irq_handler_exit: irq=1 ret=handled"),
        ])
        spans = intervals(trace)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["name"], "IRQ 1 after-gap")
        self.assertEqual(spans[0]["ts"], Decimal("0.020"))
        self.assertEqual(trace["otherData"]["nxrs"]["lost_records"], 4)

    def test_framing_carries_explicit_clock_source_loss_and_build_identity(self):
        rows = framed([
            row(10, "irq_handler_entry: irq=1 name=timer"),
            row(20, "irq_handler_exit: irq=1 ret=handled"),
        ])
        rows[0] += " clock_bits=32 frequency_hz=20000000 clock_epoch=fixture-invocation"
        trace = CONVERTER.convert(rows, source="declared qemu witness", elf_sha256="A" * 64)
        metadata = trace["otherData"]["nxrs"]
        self.assertEqual(metadata["clock"], "perf_gettime-converted")
        self.assertEqual(metadata["source"], "declared qemu witness")
        self.assertEqual(metadata["elf_sha256"], "a" * 64)
        self.assertEqual(metadata["lost_records"], 0)
        self.assertFalse(metadata["loss_detected"])
        self.assertTrue(metadata["complete"])
        self.assertTrue(any("clock_bits=32" in warning and "clock_epoch=fixture-invocation" in warning
                            for warning in metadata["warnings"]))

    def test_scheduler_discontinuity_does_not_bridge_unknown_transitions(self):
        trace = CONVERTER.convert([row(10, switch(0, 7)), row(20, switch(8, 0))])
        self.assertEqual(intervals(trace), [])
        self.assertTrue(any("continuity" in warning for warning in trace["otherData"]["nxrs"]["warnings"]))

    def test_checkpoint_recording_identity_requires_both_native_frame_endpoints(self):
        recording = "a204bb6097f14793b352dd4c22cf0eda"
        rows = framed([row(1_234_567_891, "tracing_mark_write: I|7|memory/checkpoint/1")])
        rows[0] += f" recording_id={recording} capture_mode=stopped-checkpoints"
        rows[-1] += f" recording_id={recording}"
        trace = CONVERTER.convert(rows, elf_sha256="a" * 64)
        metadata = trace["otherData"]["nxrs"]
        self.assertEqual(metadata["recording_id"], recording)
        self.assertEqual(metadata["origin_ns"], 1_234_567_891)
        marker = next(event for event in trace["traceEvents"] if event["ph"] == "i")
        self.assertEqual((marker["name"], marker["ts"]), ("memory/checkpoint/1", Decimal(0)))
        self.assertTrue(any("debugger stops" in warning for warning in metadata["warnings"]))
        for first, last in [(recording, None), (None, recording), (recording, "other-run")]:
            with self.subTest(first=first, last=last):
                malformed = framed([row(20, "tracing_mark_write: I|7|memory/checkpoint/1")])
                if first is not None:
                    malformed[0] += f" recording_id={first}"
                if last is not None:
                    malformed[-1] += f" recording_id={last}"
                with self.assertRaisesRegex(ValueError, "recording_id"):
                    CONVERTER.convert(malformed)

    def test_invalid_recording_identity_is_rejected_and_old_captures_stay_unbound(self):
        for recording in ["x" * 129, "non-ascii-\u00e9", "control-\x7f"]:
            with self.subTest(recording=recording):
                rows = framed([row(20, "tracing_mark_write: I|7|memory/checkpoint/1")])
                rows[0] += f" recording_id={recording}"
                rows[-1] += f" recording_id={recording}"
                with self.assertRaisesRegex(ValueError, "recording_id"):
                    CONVERTER.convert(rows)
        for rows in [[row(20, "tracing_mark_write: I|7|ready")],
                     framed([row(20, "tracing_mark_write: I|7|memory/checkpoint/1")])]:
            self.assertIsNone(CONVERTER.convert(rows)["otherData"]["nxrs"]["recording_id"])

    def test_diagnostics_heap_records_share_native_scheduler_coordinates(self):
        # This is the diagnostic-build branch's native request/counter format.
        trace = CONVERTER.convert([
            row(2_000_000_000, "sched_switch: next_comm=tracker next_pid=7 next_prio=100"),
            row(2_000_010_000, "irq_handler_entry: irq=5"),
            row(2_000_011_000, "irq_handler_exit: irq=5"),
            row(2_000_012_000, "heap_request: malloc heap: 0xa size:8 caller: 0xb"),
            row(2_000_013_000, "tracing_mark_write: C|7|Heap Usage|200|malloc: heap: 0xa size:8, address: 0xc"),
            row(2_000_020_000, "sched_switch: next_comm=idle next_pid=0 next_prio=0"),
        ])
        requests = [event for event in trace["traceEvents"] if event.get("cat") == "heap,request"]
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0]["ph"], requests[0]["ts"], requests[0]["name"]), ("i", 12, "malloc"))
        self.assertEqual(requests[0]["args"], {"heap": "0xa", "requested_bytes": 8, "caller": "0xb",
                                               "task": "worker", "task_id": 7, "cpu": 0, "operation": "malloc",
                                               "outcome": "unknown; allocator entry only"})
        usage = [event for event in trace["traceEvents"] if event["ph"] == "C"]
        self.assertEqual((usage[0]["name"], usage[0]["ts"], usage[0]["args"]), ("Heap 0xa", 13, {"used_bytes": 200}))
        cpu = intervals(trace, "cpu")
        self.assertEqual(cpu[0]["dur"], 20)
        self.assertFalse(cpu[0]["args"]["previous_task_verified"])
        self.assertEqual(intervals(trace, "irq")[0]["dur"], 1)
        self.assertTrue(any("including failures" in warning for warning in trace["otherData"]["nxrs"]["warnings"]))

    def test_failed_request_without_success_record_does_not_invent_a_lifetime(self):
        trace = CONVERTER.convert([
            "[0] 2.1: heap_request: memalign heap: 0xa size:2000000 caller: 0xb",
        ])
        self.assertEqual(intervals(trace), [])
        events = [event for event in trace["traceEvents"] if event["ph"] != "M"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["args"], {"heap": "0xa", "requested_bytes": 2000000, "caller": "0xb",
                                            "cpu": 0, "operation": "memalign", "outcome": "unknown; allocator entry only"})
        self.assertEqual(events[0]["name"], "memalign")
        self.assertNotIn("dur", events[0])

    def test_heap_usage_accepts_native_negative_marker_context(self):
        trace = CONVERTER.convert([
            row(100, "tracing_mark_write: C|-1|Heap Usage|9007199254740993|free: heap: 0xa size:8, address: 0xc"),
        ])
        usage = [event for event in trace["traceEvents"] if event["ph"] == "C"]
        self.assertEqual(usage[0]["args"]["used_bytes"], 9007199254740993)
        self.assertEqual(intervals(trace), [])

    def test_cross_cpu_append_order_preserves_native_times(self):
        trace = CONVERTER.convert([
            row(2_000_001_000, "irq_handler_entry: irq=1", cpu=0),
            row(2_000_000_000, "irq_handler_entry: irq=2", cpu=1),
            row(2_000_002_000, "irq_handler_exit: irq=1", cpu=0),
            row(2_000_001_000, "irq_handler_exit: irq=2", cpu=1),
        ])
        spans = intervals(trace, "irq")
        self.assertEqual([(event["name"], event["ts"], event["dur"]) for event in spans],
                         [("IRQ 2", 0, 1), ("IRQ 1", 1, 1)])
        self.assertEqual(trace["otherData"]["nxrs"]["origin_ns"], 2_000_000_000)

    def test_earlier_heap_append_normalizes_to_earliest_native_timestamp_exactly(self):
        origin = (1 << 53) + 1
        trace = CONVERTER.convert([
            row(origin + 20, "irq_handler_entry: irq=1"),
            row(origin, "heap_request: realloc heap: 0xa size:17 caller: 0xb"),
            row(origin + 30, "irq_handler_exit: irq=1"),
        ])
        self.assertEqual(trace["otherData"]["nxrs"]["origin_ns"], origin)
        span = intervals(trace, "irq")[0]
        self.assertEqual((span["ts"], span["dur"]), (Decimal("0.020"), Decimal("0.010")))
        requests = [event for event in trace["traceEvents"] if event.get("cat") == "heap,request"]
        self.assertEqual(requests[0]["ts"], 0)

    def test_global_loss_keeps_observed_heap_and_counter_semantics_without_pairing(self):
        trace = CONVERTER.convert(framed([
            row(10, "tracing_mark_write: B|7|scope"),
            row(20, "heap_request: malloc heap: 0xa size:8 caller: 0xb"),
            row(30, "tracing_mark_write: C|-1|Heap Usage|200|malloc: heap: 0xa size:8, address: 0xc"),
            row(40, "tracing_mark_write: E|7|scope"),
        ], lost=1, mode=2))
        self.assertEqual(intervals(trace), [])
        self.assertTrue(any(event.get("cat") == "heap,request" for event in trace["traceEvents"]))
        self.assertTrue(any(event.get("cat") == "heap,counter" for event in trace["traceEvents"]))
        self.assertTrue(trace["otherData"]["nxrs"]["loss_detected"])

    def test_invalid_rows_clock_conflicts_and_overflows_are_rejected(self):
        invalid = [
            [row(20, switch(0, 7)), row(10, switch(7, 0))],
            [row(20, "irq_handler_entry: irq=1"), row(10, "irq_handler_exit: irq=1")],
            [row(20, "tracing_mark_write: B|7|scope"), row(10, "tracing_mark_write: E|7|scope")],
            [row(10, "sched_switch: malformed")],
            [row(10, "irq_handler_entry: missing=1")],
            [row(10, "heap_request: malloc heap: 0xa size:8")],
            [row(10, f"heap_request: malloc heap: 0xa size:{CONVERTER.U64_MAX + 1} caller: 0xb")],
            [row(10, "tracing_mark_write: C|7|counter|NaN")],
            [row(CONVERTER.U64_MAX + 1, "irq_handler_entry: irq=1")],
            ["worker-7 [0] 1.1234567890: irq_handler_entry: irq=1"],
            ["LOST_EVENTS count=0", row(1, "irq_handler_entry: irq=1")],
            framed(["not a note row"]),
            ["console without notes"],
        ]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                CONVERTER.convert(rows)
        valid = framed([row(10, "irq_handler_entry: irq=1")])
        with self.assertRaisesRegex(ValueError, "clock conflicts"):
            CONVERTER.convert(valid, clock="different-clock")
        with self.assertRaisesRegex(ValueError, "zero-loss claim"):
            CONVERTER.convert(framed([row(10, "irq_handler_entry: irq=1")], lost=1), notes_lost="0")

    def test_cli_writes_standard_json_without_changing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            capture, output = Path(directory) / "trace.log", Path(directory) / "trace.json"
            original = "\n".join([row(10, "irq_handler_entry: irq=1 name=timer"), row(11, "irq_handler_exit: irq=1 ret=handled")])
            capture.write_text(original, encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), str(capture), str(output), "--clock", "explicit", "--notes-lost", "0"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            trace = json.loads(output.read_text(), parse_float=Decimal)
            self.assertEqual(intervals(trace)[0]["dur"], Decimal("0.001"))
            self.assertEqual(capture.read_text(), original)
            invalid = subprocess.run([sys.executable, str(SCRIPT), str(capture), str(capture)], capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)


class HeapCallConversionTests(unittest.TestCase):
    def test_exact_returned_pointer_failure_and_large_call_identity(self):
        identity = (1 << 53) + 9
        trace = CONVERTER.convert(call_framed([
            row(1_000_000_003, call_request(identity)),
            row(1_000_000_017, call_result(identity)),
            row(1_000_000_020, call_request(identity + 1, "realloc", size=4096, old="0x4000")),
            row(1_000_000_049, call_result(identity + 1, "realloc", result="0x0", outcome="failure")),
        ]))
        calls = intervals(json.loads(CONVERTER.encode_trace(trace), parse_float=Decimal), "heap")
        self.assertEqual([(item["ts"], item["dur"]) for item in calls],
                         [(Decimal(0), Decimal("0.014")), (Decimal("0.017"), Decimal("0.029"))])
        success, failure = (item["args"] for item in calls)
        self.assertEqual(success["request_id"], str(identity))
        self.assertEqual(success["result_address"], "0x4000")
        self.assertEqual((success["outcome"], success["failed"]), ("succeeded", False))
        self.assertEqual((failure["outcome"], failure["failed"]), ("failed", True))
        self.assertEqual((failure["old_address"], failure["requested_bytes"]), ("0x4000", 4096))
        self.assertNotIn("failure_reason", failure)  # A null result does not identify the cause.
        self.assertNotIn("allocation_generation", success)
        self.assertEqual(trace["otherData"]["nxrs"]["heap_call_counts"],
                         dict(requests=2, results=2, paired=2, failed=1, incomplete_requests=0, orphan_results=0))

    def test_nested_backing_calls_remain_distinct_and_keep_input_attribution(self):
        trace = CONVERTER.convert(call_framed([
            row(10, call_request(8, "memalign", size=17, alignment=64, caller="0x101")),
            row(20, call_request(9, size=128, caller="0x205")),
            row(30, call_result(9, result="0x4020")),
            row(40, call_result(8, "memalign", result="0x4040")),
        ]))
        outer, inner = intervals(trace, "heap")
        self.assertEqual((outer["name"], inner["name"]), ("memalign", "malloc"))
        self.assertEqual(outer["tid"], inner["tid"])
        self.assertEqual([event["args"]["requested_bytes"] for event in (outer, inner)], [17, 128])
        self.assertEqual([event["args"]["caller"] for event in (outer, inner)], ["0x101", "0x205"])
        self.assertEqual(outer["args"]["alignment"], 64)
        self.assertEqual(inner["args"]["result_address"], "0x4020")
        self.assertTrue(all("internal backing" in event["args"]["call_scope"] for event in (outer, inner)))

    def test_crossing_calls_on_preempted_tasks_use_separate_tracks(self):
        trace = CONVERTER.convert(call_framed([
            row(10, call_request(11), pid=7),
            row(20, call_request(12, heap="0xb"), pid=8),
            row(30, call_result(11), pid=7),
            row(40, call_result(12, heap="0xb"), pid=8),
        ]))
        calls = intervals(trace, "heap")
        self.assertEqual(len({call["tid"] for call in calls}), 2)
        self.assertEqual([call["args"]["task_id"] for call in calls], [7, 8])
        self.assertTrue(all(call["args"]["cpu"] == 0 for call in calls))

    def test_task_migration_and_cross_cpu_append_order_keep_exact_call_endpoints(self):
        trace = CONVERTER.convert(call_framed([
            row(20, call_request(12), cpu=1, pid=8),
            row(10, call_request(11), cpu=0, pid=7),
            row(30, call_result(11), cpu=1, pid=7),
            row(40, call_result(12), cpu=0, pid=8),
        ]))
        calls = intervals(trace, "heap")
        self.assertEqual([call["args"]["request_id"] for call in calls], ["11", "12"])
        self.assertEqual((calls[0]["ts"], calls[0]["dur"]), (0, Decimal("0.020")))
        self.assertEqual((calls[0]["args"]["cpu"], calls[0]["args"]["result_cpu"]), (0, 1))

    def test_zero_size_uses_explicit_native_outcome_without_inventing_release(self):
        for operation, old, alignment in [("malloc", "0x0", 0), ("realloc", "0x4000", 0),
                                          ("memalign", "0x0", 64)]:
            with self.subTest(operation=operation):
                trace = CONVERTER.convert(call_framed([
                    row(10, call_request(1, operation, size=0, old=old, alignment=alignment)),
                    row(20, call_result(1, operation)),
                ]))
                args = intervals(trace, "heap")[0]["args"]
                self.assertEqual((args["requested_bytes"], args["outcome"], args["failed"]), (0, "succeeded", False))
                self.assertNotIn("released", args)

    def test_unmatched_endpoints_do_not_fabricate_outcomes_or_request_counts(self):
        trace = CONVERTER.convert(call_framed([
            row(10, call_result(7, result="(nil)", outcome="failure")),
            row(20, call_request(8, "realloc", old="0x4000")),
        ]))
        self.assertEqual(intervals(trace, "heap"), [])
        points = [event for event in trace["traceEvents"] if event["ph"] == "i"]
        self.assertEqual(len(points), 2)
        orphan, pending = (point["args"] for point in points)
        self.assertFalse(orphan["request_observed"])
        self.assertEqual(orphan["observed_outcome"], "failed")
        self.assertNotIn("requested_bytes", orphan)
        self.assertTrue(pending["request_observed"])
        self.assertEqual(pending["old_address"], "0x4000")
        for args in (orphan, pending):
            self.assertTrue(args["duration_unknown"])
            self.assertNotIn("outcome", args)
            self.assertNotIn("failed", args)
            self.assertIn("incomplete_reason", args)
        self.assertFalse(trace["otherData"]["nxrs"]["complete"])

    def test_located_loss_breaks_pending_pair_and_later_calls_recover(self):
        trace = CONVERTER.convert(call_framed([
            row(10, call_request(1)),
            "LOST_EVENTS count=2",
            row(20, call_result(1)),
            row(30, call_request(2)),
            row(40, call_result(2)),
        ]))
        self.assertEqual([event["args"]["request_id"] for event in intervals(trace, "heap")], ["2"])
        counts = trace["otherData"]["nxrs"]["heap_call_counts"]
        self.assertEqual((counts["paired"], counts["incomplete_requests"], counts["orphan_results"]), (1, 1, 1))

    def test_unlocated_loss_preserves_only_independent_endpoint_observations(self):
        trace = CONVERTER.convert(call_framed([
            row(10, call_request(1)), row(20, call_result(1, result="0x0", outcome="failure")),
        ], lost=1, mode=2))
        self.assertEqual(intervals(trace, "heap"), [])
        observations = [event["args"] for event in trace["traceEvents"] if event["ph"] == "i"]
        self.assertTrue(all("outcome" not in args and "failed" not in args for args in observations))
        self.assertEqual(trace["otherData"]["nxrs"]["heap_call_counts"]["failed"], 0)

    def test_bad_identity_reuse_order_and_result_contradictions_are_rejected(self):
        invalid = [
            [row(10, call_request(1)), row(20, call_request(1))],
            [row(10, call_result(1)), row(20, call_result(1))],
            [row(10, call_result(1)), row(20, call_request(1))],
            [row(10, call_request(1)), row(20, call_result(1, heap="0xb"))],
            [row(10, call_request(1)), row(20, call_result(1, "realloc"))],
            [row(10, call_request(1)), row(20, call_result(1), pid=8)],
            [row(20, call_request(1)), row(10, call_result(1))],
            [row(10, call_request(1)), row(20, call_result(1, result="0x0"))],
            [row(10, call_request(1)), row(20, call_result(1, outcome="failure"))],
            [row(10, call_request(1)), row(20, call_result(1)), row(30, call_request(1))],
        ]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                CONVERTER.convert(call_framed(rows))

    def test_invalid_width_zero_id_pointer_and_native_arguments_are_rejected(self):
        invalid = [call_request(0), call_request(1 << 64), call_request(1, size=1 << 64),
                   call_request(1, heap="0x0"), call_request(1, heap="0x10000000000000000"),
                   call_request(1, caller="missing"), call_request(1, old="0x4000"),
                   call_request(1, alignment=64), call_request(1, "memalign", alignment=1 << 64),
                   call_result(0), call_result(1, result="0x10000000000000000"),
                   call_result(1, outcome="released")]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                CONVERTER.convert(call_framed([row(10, payload)]))

    def test_missing_task_identity_uses_independent_tracks(self):
        trace = CONVERTER.convert(call_framed([
            f"[0] 1.000000010: {call_request(1)}", f"[0] 1.000000020: {call_request(2)}",
            f"[0] 1.000000030: {call_result(1)}", f"[0] 1.000000040: {call_result(2)}",
        ]))
        self.assertEqual(len({event["tid"] for event in intervals(trace, "heap")}), 2)

    def test_pending_calls_are_bounded_and_completed_calls_release_pending_capacity(self):
        with patch.object(CONVERTER._HeapCalls, "MAX_PENDING", 2):
            with self.assertRaisesRegex(ValueError, "pending allocator-call bound"):
                CONVERTER.convert(call_framed([row(index * 10, call_request(index)) for index in (1, 2, 3)]))
            trace = CONVERTER.convert(call_framed([row(10, call_request(1)), row(20, call_request(2)),
                                                   row(30, call_result(2)), row(40, call_request(3)),
                                                   row(50, call_result(3)), row(60, call_result(1))]))
            self.assertEqual(len(intervals(trace, "heap")), 3)

    def test_unidentified_capture_cannot_pair_across_a_reset(self):
        endpoints = [row(1_000_000_010, call_request(1)), row(2_000_000_040, call_result(1))]
        for rows in [endpoints, framed(endpoints),
                     [endpoints[0], "NuttShell (NSH) restarted", endpoints[1]]]:
            with self.subTest(rows=rows):
                trace = CONVERTER.convert(rows)
                self.assertEqual(intervals(trace, "heap"), [])
                points = [event for event in trace["traceEvents"] if event["ph"] == "i"]
                self.assertEqual(len(points), 2)
                self.assertTrue(all("reset continuity" in event["args"]["incomplete_reason"] for event in points))
                self.assertTrue(all("failed" not in event["args"] for event in points))
        identified = call_framed(endpoints)
        identified[-1] = identified[-1].replace("identified-allocator-test-run", "different-boot")
        with self.assertRaisesRegex(ValueError, "recording_id"):
            CONVERTER.convert(identified)

    def test_crossing_completed_calls_fail_but_a_missing_inner_result_stays_unknown(self):
        with self.assertRaisesRegex(ValueError, "Crossing native allocator calls"):
            CONVERTER.convert(call_framed([
                row(10, call_request(1)), row(20, call_request(2)),
                row(30, call_result(1)), row(40, call_result(2)),
            ]))
        trace = CONVERTER.convert(call_framed([
            row(10, call_request(1)), row(20, call_request(2)), row(30, call_result(1)),
        ]))
        self.assertEqual([event["args"]["request_id"] for event in intervals(trace, "heap")], ["1"])
        unknown = [event for event in trace["traceEvents"] if event["ph"] == "i"]
        self.assertEqual(unknown[0]["args"]["request_id"], "2")
        self.assertNotIn("outcome", unknown[0]["args"])


if __name__ == "__main__":
    unittest.main()
