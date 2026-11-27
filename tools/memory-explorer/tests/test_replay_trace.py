import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from replay_trace import trace_view, write_trace


class ReplayTimingTests(unittest.TestCase):
    def journal(self):
        # Above double's integer precision: adjacent nanoseconds must survive.
        origin = 2**53 + 1
        return {"recording_id": "test-run", "clock": "test-clock",
                "build": {"elf_sha256": "a" * 64}, "heap": {"id": "one", "epoch": "1"},
                "initial": {"sequence": 0, "time_ns": origin},
                "records": [{"kind": "transaction", "sequence": 1, "start_ns": origin + 1,
                             "commit_ns": origin + 2, "label": "test mutation"},
                            {"kind": "checkpoint", "sequence": 1, "time_ns": origin + 3}],
                "closing": {"sequence": 1, "time_ns": origin + 4}}

    def test_exact_nanoseconds_and_clock_origin_survive_json(self):
        journal = self.journal()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timing.json"
            write_trace(journal, path)
            trace = json.loads(path.read_text(), parse_float=Decimal)
        self.assertEqual(trace["otherData"]["nxrs"]["origin_ns"], 2**53 + 1)
        event = next(event for event in trace["traceEvents"] if event["ph"] == "X")
        self.assertEqual(event["ts"], Decimal("0.001"))
        self.assertEqual(event["dur"], Decimal("0.001"))
        self.assertEqual(event["args"]["sequence"], 1)
        self.assertIn("debugger", event["args"]["measurement"])

    def test_loss_and_unclosed_recording_are_not_marked_complete(self):
        journal = self.journal()
        journal["records"].append({"kind": "loss", "time_ns": journal["closing"]["time_ns"], "reason": "injected loss"})
        metadata = trace_view(journal)["otherData"]["nxrs"]
        self.assertFalse(metadata["complete"])
        self.assertEqual(metadata["lost_records"], "detected")
        journal["records"].pop()
        journal["closing"] = None
        self.assertFalse(trace_view(journal)["otherData"]["nxrs"]["complete"])

    def test_backward_window_or_time_outside_source_epoch_is_rejected(self):
        for field in ["start_ns", "commit_ns"]:
            journal = self.journal()
            journal["records"][0][field] = 0
            with self.assertRaises(ValueError):
                trace_view(journal)

    def test_existing_trace_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timing.json"
            write_trace(self.journal(), path)
            with self.assertRaises(FileExistsError):
                write_trace(self.journal(), path)


if __name__ == "__main__":
    unittest.main()
