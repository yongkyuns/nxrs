"""Source attribution, native heap observations and measured dispatch endpoints."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "nuttx-trace-to-perfetto.py"
SPEC = importlib.util.spec_from_file_location("trace_enrichment_converter", SCRIPT)
CONVERTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONVERTER)


def row(ns, payload, cpu=0, task="acquisition", pid=7):
    return f"{task}-{pid} [{cpu}] {ns // 1_000_000_000}.{ns % 1_000_000_000:09}: {payload}"


def switch(before, after, cpu=0, ns=100):
    return row(ns, f"sched_switch: prev_comm=previous prev_pid={before} prev_prio=100 prev_state=R "
               f"==> next_comm=next next_pid={after} next_prio=100", cpu=cpu)


def events(trace, category):
    return [event for event in trace["traceEvents"] if category in event.get("cat", "").split(",")]


class TraceEnrichmentTests(unittest.TestCase):
    def test_heap_retains_native_operations_without_inventing_lifetimes_or_request_success(self):
        trace = CONVERTER.convert([
            row(10, "heap_request: memalign heap: 0xa size:32 caller: 0xb"),
            row(20, "tracing_mark_write: C|7|Heap Usage|144|malloc: heap: 0xa size:48, address: 0x20"),
            row(30, "tracing_mark_write: C|7|Heap Usage|96|free: heap: 0xa size:48, address: 0x20"),
            row(40, "heap_request: malloc heap: 0xa size:2000000 caller: 0xc"),
        ])
        self.assertEqual([event["args"]["used_bytes"] for event in events(trace, "counter")], [144, 96])
        allocation, release = events(trace, "allocation")[0], events(trace, "free")[0]
        self.assertEqual((allocation["args"]["address"], allocation["args"]["bytes"]), ("0x20", 48))
        self.assertEqual((release["args"]["task_id"], release["args"]["cpu"]), (7, 0))
        self.assertTrue(all(event["ph"] in ("i", "C") for event in events(trace, "heap")))
        self.assertTrue(all("unknown" in event["args"]["outcome"] for event in events(trace, "request")))
        self.assertTrue(all("failed" not in event["args"] for event in events(trace, "request")))
        self.assertEqual(trace["otherData"]["nxrs"]["observed_note_count"], 4)
        self.assertEqual(trace["otherData"]["nxrs"]["event_counts"], {"heap_request": 2, "tracing_mark_write": 2})

    def test_wake_to_dispatch_uses_observed_endpoints_and_dispatch_cpu(self):
        trace = CONVERTER.convert([
            switch(3, 0, ns=10),
            row(20, "sched_waking: comm=imu pid=7 target_cpu=1"),
            switch(0, 7, cpu=1, ns=45),
            switch(7, 0, cpu=1, ns=80),
        ])
        latency = events(trace, "latency")
        self.assertEqual(len(latency), 1)
        self.assertEqual((latency[0]["ts"] * 1000, latency[0]["dur"] * 1000), (10, 25))
        self.assertEqual(latency[0]["args"]["dispatch_cpu"], 1)
        self.assertIn("not CPU cost", latency[0]["args"]["accounting"])

    def test_ambiguous_repeated_wakes_and_running_task_do_not_get_latency(self):
        for rows in [
            [switch(0, 7, ns=10), row(20, "sched_waking: comm=imu pid=7 target_cpu=0"), switch(7, 7, ns=30)],
            [row(10, "sched_waking: comm=imu pid=7 target_cpu=0"), switch(7, 7, ns=30)],
            [row(10, "sched_waking: comm=imu pid=7 target_cpu=0"),
             row(20, "sched_waking: comm=imu pid=7 target_cpu=0"),
             row(25, "sched_waking: comm=imu pid=7 target_cpu=0"), switch(0, 7, ns=30)],
        ]:
            with self.subTest(rows=rows):
                self.assertEqual(events(CONVERTER.convert(rows), "latency"), [])

    def test_loss_boundary_cannot_bridge_wake_and_dispatch(self):
        trace = CONVERTER.convert([
            row(10, "sched_waking: comm=imu pid=7 target_cpu=0"),
            "LOST_EVENTS count=2", switch(0, 7, ns=30),
            row(40, "sched_waking: comm=gnss pid=9 target_cpu=0"), switch(7, 9, ns=50),
        ])
        latency = events(trace, "latency")
        self.assertEqual(len(latency), 1)
        self.assertEqual(latency[0]["args"]["task_id"], 9)
        self.assertEqual(latency[0]["dur"] * 1000, 10)

    def test_scheduler_discontinuity_does_not_invent_dispatch_latency(self):
        trace = CONVERTER.convert([
            switch(0, 3, ns=10),
            row(20, "sched_waking: comm=imu pid=7 target_cpu=0"),
            switch(9, 7, ns=30),  # The task-3 -> task-9 switch is missing.
        ])
        self.assertEqual(events(trace, "latency"), [])
        self.assertTrue(any("discontinuity" in event["name"] for event in events(trace, "capture")))

    def test_irq_context_retains_interrupted_task_without_claiming_device_source(self):
        trace = CONVERTER.convert([
            row(10, "irq_handler_entry: irq=15 name=0x4001S", task="gnss"),
            row(20, "irq_handler_exit: irq=15 ret=handled", task="gnss"),
        ])
        irq = events(trace, "irq")[0]
        self.assertEqual(irq["args"]["handler"], "0x4001S")
        self.assertEqual(irq["args"]["interrupted_task"], "gnss")
        self.assertNotIn("source", irq["args"])
        self.assertNotIn("gnss", irq["name"])

    def test_censored_callbacks_remain_findable_by_kind_and_source(self):
        for category, entry, matched, unmatched in [
            ("irq", "irq_handler_entry: irq=15 name=0x1001S",
             "irq_handler_exit: irq=15 ret=handled", "irq_handler_exit: irq=16 ret=handled"),
            ("timer", "tracing_mark_write: I|7|wdog: enter-0x1001S 0x20",
             "tracing_mark_write: I|7|wdog: leave-0x1001S 0x20",
             "tracing_mark_write: I|7|wdog: leave-0x1001S 0x21"),
        ]:
            for rows, global_loss, expected in [
                ([row(10, entry)], None, 1),
                ([row(10, entry), row(20, unmatched)], None, 2),
                ([row(10, entry), "LOST_EVENTS count=1", row(20, matched)], None, 2),
                ([row(10, entry), row(20, matched)], "1", 2),
            ]:
                with self.subTest(category=category, rows=rows, global_loss=global_loss):
                    callbacks = events(CONVERTER.convert(rows, notes_lost=global_loss), category)
                    self.assertEqual(len(callbacks), expected)
                    self.assertTrue(all(item["ph"] == "i" and "dur" not in item for item in callbacks))
                    self.assertTrue(all(item["args"]["duration_unknown"] for item in callbacks))
                    self.assertTrue(all("unknown" in item["cat"].split(",") for item in callbacks))
                    self.assertEqual(callbacks[0]["args"]["handler"], "0x1001S")

    def test_watchdog_callback_pairs_by_cpu_nesting_handler_and_argument(self):
        trace = CONVERTER.convert([
            row(10, "tracing_mark_write: I|7|wdog: start-0x1001S 0x20"),
            row(20, "tracing_mark_write: I|7|wdog: enter-0x1001S 0x20"),
            row(25, "tracing_mark_write: I|7|wdog: enter-0x2001S 0x30"),
            row(29, "tracing_mark_write: I|7|wdog: leave-0x2001S 0x30"),
            row(40, "tracing_mark_write: I|7|wdog: leave-0x1001S 0x20"),
            row(50, "tracing_mark_write: I|7|wdog: cancel-0x1001S 0x20"),
        ])
        callbacks = [event for event in events(trace, "timer") if event["ph"] == "X"]
        self.assertEqual([event["dur"] * 1000 for event in callbacks], [20, 4])
        self.assertEqual([event["args"]["nesting_depth"] for event in callbacks], [0, 1])
        self.assertEqual(callbacks[0]["args"]["callback_arg"], "0x20")
        self.assertEqual(trace["otherData"]["nxrs"]["timer_event_counts"], {"start": 1, "enter": 2, "leave": 2, "cancel": 1})

    def test_timer_loss_and_mismatched_argument_do_not_create_intervals(self):
        for rows in [
            [row(10, "tracing_mark_write: I|7|wdog: enter-0x1001S 0x20"),
             row(20, "tracing_mark_write: I|7|wdog: leave-0x1001S 0x21")],
            [row(10, "tracing_mark_write: I|7|wdog: enter-0x1001S 0x20"), "LOST_EVENTS count=1",
             row(20, "tracing_mark_write: I|7|wdog: leave-0x1001S 0x20")],
        ]:
            trace = CONVERTER.convert(rows)
            self.assertFalse(any(event["ph"] == "X" for event in events(trace, "timer")))
            self.assertTrue(any(event["args"].get("duration_unknown") for event in events(trace, "capture")))

    def test_timer_clock_cannot_run_backward_between_closed_callbacks(self):
        with self.assertRaisesRegex(ValueError, "timestamps ran backward"):
            CONVERTER.convert([
                row(30, "tracing_mark_write: I|7|wdog: enter-0x1001S 0x20"),
                row(40, "tracing_mark_write: I|7|wdog: leave-0x1001S 0x20"),
                row(10, "tracing_mark_write: I|7|wdog: enter-0x1001S 0x20"),
                row(20, "tracing_mark_write: I|7|wdog: leave-0x1001S 0x20"),
            ])

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc") and shutil.which("nm") and shutil.which("addr2line"), "requires Linux host ELF tools")
    def test_symbolization_uses_real_exact_elf_and_native_pointer_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.c"
            elf = Path(directory) / "source.elf"
            source.write_text("int timer_callback(void) { return 4; }\nint allocate_samples(void) { return 8; }\nint main(void) { return timer_callback() + allocate_samples(); }\n")
            build = subprocess.run(["cc", "-g", "-O0", "-fno-pie", "-no-pie", str(source), "-o", str(elf)], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            symbols = {parts[2]: int(parts[0], 16) for line in subprocess.check_output(["nm", str(elf)], text=True).splitlines()
                       if len(parts := line.split()) == 3}
            handler, caller = symbols["timer_callback"], symbols["allocate_samples"]
            trace = CONVERTER.convert([
                row(10, f"irq_handler_entry: irq=15 name=0x{handler:x}S"),
                row(20, "irq_handler_exit: irq=15 ret=handled"),
                row(30, f"heap_request: malloc heap: 0xa size:8 caller: 0x{caller:x}"),
                row(40, "heap_request: realloc heap: 0xa size:8 caller: 0x1"),
                row(50, f"irq_handler_entry: irq=15 name=0x{handler:x}S"),
            ], elf_sha256=CONVERTER.elf_identity(elf))
            CONVERTER.symbolize_trace(trace, elf, addr2line=shutil.which("addr2line"))
            irq, request = events(trace, "irq")[0], events(trace, "request")[0]
            self.assertEqual(irq["args"]["handler_function"], "timer_callback")
            self.assertEqual(irq["args"]["handler_file"], str(source))
            self.assertEqual(irq["args"]["handler_line"], 1)
            self.assertEqual(irq["name"], "IRQ 15 · timer_callback")
            incomplete_irq = events(trace, "irq")[1]
            self.assertEqual(incomplete_irq["name"], irq["name"])
            self.assertEqual(incomplete_irq["args"]["handler_function"], "timer_callback")
            self.assertEqual(incomplete_irq["ph"], "i")
            self.assertIn("unknown", incomplete_irq["cat"].split(","))
            self.assertEqual(request["args"]["caller_function"], "allocate_samples")
            self.assertEqual(request["args"]["caller_line"], 2)
            self.assertNotIn("caller_function", events(trace, "request")[1]["args"])
            attribution = trace["otherData"]["nxrs"]["source_attribution"]
            self.assertEqual((attribution["caller_observations"], attribution["caller_resolved"]), (2, 1))
            trace["otherData"]["nxrs"]["elf_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "hash conflicts"):
                CONVERTER.symbolize_trace(trace, elf)

    def test_cli_rejects_output_overwriting_symbol_elf(self):
        with tempfile.TemporaryDirectory() as directory:
            capture, elf = Path(directory) / "trace.log", Path(directory) / "firmware"
            capture.write_text(row(10, "heap_request: malloc heap: 0xa size:8 caller: 0xb"))
            elf.write_bytes(b"do not overwrite")
            result = subprocess.run([sys.executable, str(SCRIPT), str(capture), str(elf), "--elf", str(elf)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(elf.read_bytes(), b"do not overwrite")


if __name__ == "__main__":
    unittest.main()
