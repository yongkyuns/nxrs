import runpy
import unittest
from pathlib import Path


convert = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "tools/nuttx-trace-to-perfetto.py")
)["convert"]


class NuttxTraceTest(unittest.TestCase):
    def test_scheduler_interrupt_and_heap_share_the_same_clock(self):
        trace = convert([
            "process-7 [0] 2.000000000: sched_switch: next_comm=tracker next_pid=7 next_prio=100",
            "process-7 [0] 2.000010000: irq_handler_entry: irq=5",
            "process-7 [0] 2.000011000: irq_handler_exit: irq=5",
            "process-7 [0] 2.000012000: heap_request: malloc heap: 0xa size:8 caller: 0xb",
            "process-7 [0] 2.000013000: tracing_mark_write: C|7|Heap Usage|200|malloc: heap: 0xa size:8, address: 0xc",
            "process-7 [0] 2.000020000: sched_switch: next_comm=idle next_pid=0 next_prio=0",
        ])
        scheduler, irq, request, usage = (e for e in trace["traceEvents"] if e["ph"] != "M")
        self.assertEqual((scheduler["name"], scheduler["dur"]), ("tracker", 20))
        self.assertEqual((irq["name"], irq["ts"], irq["dur"]), ("IRQ 5", 10, 1))
        self.assertEqual((request["ph"], request["ts"]), ("i", 12))
        self.assertEqual(request["args"], {
            "heap": "0xa", "requested_bytes": 8, "caller": "0xb", "task": "process", "task_id": 7,
        })
        self.assertEqual((usage["name"], usage["args"]), ("Heap 0xa", {"used_bytes": 200}))

    def test_heap_request_does_not_require_a_success_note(self):
        trace = convert(["[0] 2.1: heap_request: memalign heap: 0xa size:2000000 caller: 0xb"])
        request = trace["traceEvents"][-1]
        self.assertEqual((request["name"], request["args"]["requested_bytes"]), ("memalign", 2000000))

    def test_notes_from_different_cpus_can_arrive_out_of_order(self):
        trace = convert([
            "[0] 2.000001: irq_handler_entry: irq=1",
            "[1] 2.000000: irq_handler_entry: irq=2",
            "[0] 2.000002: irq_handler_exit: irq=1",
            "[1] 2.000001: irq_handler_exit: irq=2",
        ])
        self.assertEqual([(e["name"], e["ts"]) for e in trace["traceEvents"] if e["ph"] == "X"],
                         [("IRQ 2", 0), ("IRQ 1", 1)])
