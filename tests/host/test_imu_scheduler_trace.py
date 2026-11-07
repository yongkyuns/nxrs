"""Focused checks for bounded NuttX IMU scheduler-note captures."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from imu_scheduler_trace import MAX_CAPTURE_BYTES, analyze


BEGIN = (
    "NXRS_NOTE_BEGIN schema=1 source=nuttx-noteram-ascii "
    "clock=perf_gettime-converted cpus=1 irq=17 worker=imu-acquire "
    "mode=0 notes_lost=0 window_ms=200"
)
END = "NXRS_NOTE_END schema=1 mode=0 notes_lost=0 complete=1"
BEGIN_V2 = BEGIN.replace("schema=1", "schema=2").replace(
    "irq=17 worker=imu-acquire", "irq=17 worker=imu-acquire i2c=1"
)
END_V2 = END.replace("schema=1", "schema=2")
SWITCH = (
    "sched_switch: prev_comm=nsh prev_pid=1 prev_prio=100 prev_state=S "
    "==> next_comm=imu-acquire next_pid=5 next_prio=100"
)


def row(task: str, pid: int, when: str, body: str) -> str:
    return f"{task}-{pid} [0] {when}: {body}"


def sample(start: str = "12.000000000", *, pid: int = 5) -> tuple[str, ...]:
    seconds, nanos = start.split(".")
    start_ns = int(seconds) * 1_000_000_000 + int(nanos)
    wake_ns, exit_ns = start_ns + 20_000, start_ns + 50_000

    def stamp(ns: int) -> str:
        return f"{ns // 1_000_000_000}.{ns % 1_000_000_000:09}"

    return (
        row("nsh", 1, stamp(start_ns), "irq_handler_entry: irq=17 name=gpio_handler"),
        row("imu-acquire", pid, stamp(wake_ns),
            f"sched_waking: comm=imu-acquire pid={pid} target_cpu=0"),
        row("nsh", 1, stamp(exit_ns), "irq_handler_exit: irq=17 ret=handled"),
        row("nsh", 1, stamp(exit_ns), SWITCH.replace("next_pid=5", f"next_pid={pid}")),
    )


def capture(*rows: str, begin: str = BEGIN, end: str = END) -> str:
    return "\n".join((begin, *rows, end))


def scope_rows(start: str = "12.000060000", *, pid: int = 5) -> tuple[str, ...]:
    start_ns = int(start.split(".")[0]) * 1_000_000_000 + int(start.split(".")[1])

    def mark(offset: int, edge: str, label: str) -> str:
        ns = start_ns + offset
        when = f"{ns // 1_000_000_000}.{ns % 1_000_000_000:09}"
        return row("imu-acquire", pid, when,
                   f"tracing_mark_write: {edge}|{pid}|nxrs.i2c.{label}")

    return (
        mark(0, "B", "char_lock_wait"), mark(10_000, "E", "char_lock_wait"),
        mark(20_000, "B", "esp32s3_lock_wait"), mark(30_000, "E", "esp32s3_lock_wait"),
        mark(40_000, "B", "esp32s3_service"), mark(70_000, "E", "esp32s3_service"),
    )


class SchedulerTraceTests(unittest.TestCase):
    def test_requires_adjacent_same_time_switch_and_single_wake(self):
        result = analyze(capture(*sample(),
            row("nsh", 1, "12.100000000", "irq_handler_entry: irq=17 name=gpio_handler")))
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["latency_ns"]["max"], 50_000)
        self.assertEqual(result["counts"]["trailing_censored_irq_entries"], 1)
        self.assertFalse(result["scope"]["physical_sensor_edge_included"])
        self.assertFalse(result["scope"]["first_worker_instruction_proven"])

        nonadjacent = sample()[:3] + (row("nsh", 1, "12.000051000", SWITCH),)
        result = analyze(capture(*nonadjacent))
        self.assertEqual(result["status"], "invalid")
        self.assertIn("missing_post_irq_worker_switch", result["invalid_reasons"])
        self.assertEqual(result["latency_ns"]["samples"], 0)

    def test_duplicate_wakes_and_coalesced_irq_are_rejected(self):
        rows = list(sample())
        rows.insert(2, row("imu-acquire", 5, "12.000030000",
                           "sched_waking: comm=imu-acquire pid=5 target_cpu=0"))
        result = analyze(capture(*rows))
        self.assertIn("duplicate_worker_wake", result["invalid_reasons"])

        coalesced = (
            *sample("1.000000000")[:3],
            row("nsh", 1, "1.000030000", "irq_handler_entry: irq=17 name=gpio_handler"),
            *sample("1.000040000")[1:],
        )
        result = analyze(capture(*coalesced))
        self.assertIn("ambiguous_gpio_irq_coalescing", result["invalid_reasons"])

    def test_overflow_nonmonotonic_and_malformed_rows_fail(self):
        begin = BEGIN.replace("mode=0 notes_lost=0", "mode=2 notes_lost=1")
        end = END.replace("mode=0 notes_lost=0", "mode=2 notes_lost=1")
        result = analyze(capture(*sample(), begin=begin, end=end))
        self.assertIn("note_buffer_overflow", result["invalid_reasons"])

        backwards = sample() + (row("nsh", 1, "0.000000001",
                                    "sched_waking: comm=nsh pid=1 target_cpu=0"),)
        result = analyze(capture(*backwards))
        self.assertIn("nonmonotonic_converted_time", result["invalid_reasons"])
        with self.assertRaises(ValueError):
            analyze(capture("not a NuttX note row"))
        with self.assertRaises(ValueError):
            analyze("x" * (MAX_CAPTURE_BYTES + 1))

    def test_cli_emits_json_and_nonzero_for_invalid_capture(self):
        script = Path(__file__).with_name("imu_scheduler_trace.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text(capture(*sample()), encoding="utf-8")
            valid = subprocess.run([sys.executable, str(script), str(path)],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(json.loads(valid.stdout)["status"], "valid")

            path.write_text("bad\n", encoding="utf-8")
            invalid = subprocess.run([sys.executable, str(script), str(path)],
                                     capture_output=True, text=True, check=False)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(json.loads(invalid.stdout)["status"], "invalid")

    def test_v2_wired_reports_i2c_scopes_and_irq_entry_coverage(self):
        rows = (*sample(), *scope_rows(),
                row("nsh", 1, "12.100000000", "irq_handler_entry: irq=17 name=gpio_handler"))
        result = analyze(capture(*rows, begin=BEGIN_V2, end=END_V2))
        self.assertEqual(result["status"], "valid", result["invalid_reasons"])
        self.assertEqual(result["i2c"]["scopes"]["nxrs.i2c.esp32s3_service"]["duration_ns"]["max"], 30_000)
        coverage = result["i2c"]["irq_to_entry"]
        char = coverage["irq_to_char_driver_entry"]
        service = coverage["irq_to_controller_service_entry"]
        self.assertEqual(char["covered_entries"], 1)
        self.assertEqual(char["coverage_percent"], 100)
        self.assertEqual(char["latency_ns"]["max"], 60_000)
        self.assertEqual(service["covered_entries"], 1)
        self.assertEqual(service["coverage_percent"], 100)
        self.assertEqual(service["latency_ns"]["max"], 100_000)
        self.assertEqual(char["metric"], "gpio_irq_dispatch_entry_to_char_driver_lock_wait_entry")
        self.assertEqual(service["metric"], "gpio_irq_dispatch_entry_to_controller_service_entry")
        self.assertFalse(char["physical_sensor_edge_included"])
        self.assertFalse(char["i2c_wire_time_included"])
        self.assertFalse(service["i2c_wire_time_included"])

        # A service interval from before this IRQ cannot supply its missing coverage.
        earlier = (*scope_rows("11.999900000"), *sample(), *scope_rows()[:2])
        result = analyze(capture(*earlier, begin=BEGIN_V2, end=END_V2))
        self.assertEqual(result["status"], "invalid")
        self.assertIn("missing_entry_coverage:irq_to_controller_service_entry",
                      result["invalid_reasons"])

    def test_v2_periodic_i2c_works_without_irq_and_counts_capture_edges(self):
        begin = BEGIN_V2.replace("irq=17", "irq=none")
        rows = (
            row("imu-acquire", 5, "12.000050000",
                "tracing_mark_write: E|5|nxrs.i2c.char_lock_wait"),
            *scope_rows("12.000060000"),
            row("imu-acquire", 5, "12.000140000",
                "tracing_mark_write: B|5|nxrs.i2c.esp32s3_service"),
        )
        result = analyze(capture(*rows, begin=begin, end=END_V2))
        self.assertEqual(result["status"], "valid", result["invalid_reasons"])
        char = result["i2c"]["scopes"]["nxrs.i2c.char_lock_wait"]["counts"]
        self.assertEqual(char["leading_censored_ends"], 1)
        service = result["i2c"]["scopes"]["nxrs.i2c.esp32s3_service"]["counts"]
        self.assertEqual(service["trailing_censored_begins"], 1)
        self.assertIsNone(result["i2c"]["irq_to_entry"])

    def test_v2_ambiguous_malformed_and_missing_i2c_evidence_fail(self):
        with self.assertRaises(ValueError):
            analyze(capture(begin=BEGIN_V2.replace("irq=17", "irq=none").replace(
                "i2c=1", "i2c=0"), end=END_V2))

        marks = list(scope_rows())
        marks.insert(1, marks[0])
        result = analyze(capture(*marks, begin=BEGIN_V2.replace("irq=17", "irq=none"),
                                 end=END_V2))
        self.assertIn("ambiguous_i2c_scope", result["invalid_reasons"])

        with self.assertRaises(ValueError):
            analyze(capture(
                row("imu-acquire", 5, "12.000000000",
                    "tracing_mark_write: B|bad|nxrs.i2c.char_lock_wait"),
                begin=BEGIN_V2.replace("irq=17", "irq=none"), end=END_V2,
            ))
        result = analyze(capture(begin=BEGIN_V2.replace("irq=17", "irq=none"), end=END_V2))
        self.assertTrue(any(reason.startswith("missing_i2c_evidence:")
                            for reason in result["invalid_reasons"]))

        wrong_pid = list(scope_rows())
        wrong_pid[0] = wrong_pid[0].replace("B|5|", "B|6|")
        result = analyze(capture(*wrong_pid, begin=BEGIN_V2.replace("irq=17", "irq=none"),
                                 end=END_V2))
        self.assertIn("i2c_note_worker_pid_mismatch", result["invalid_reasons"])

        unexpected_irq = analyze(capture(
            row("nsh", 1, "12.000000000", "irq_handler_entry: irq=17 name=gpio_handler"),
            *scope_rows(), begin=BEGIN_V2.replace("irq=17", "irq=none"), end=END_V2,
        ))
        self.assertIn("unexpected_irq_records", unexpected_irq["invalid_reasons"])


if __name__ == "__main__":
    unittest.main()
