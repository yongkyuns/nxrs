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


if __name__ == "__main__":
    unittest.main()
