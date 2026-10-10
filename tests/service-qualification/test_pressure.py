import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

with patch.object(sys, "path", sys.path.copy()):
    from pressure import BATCH_FIELDS, END_FIELDS, STEPS, check_profile, parse_pressure, public_report
from test_publish import report as footprint_fixture


def profile(phase, index):
    events = 2048 if phase == "load" else 100
    row = dict(phase=phase, index=index, source_ok=events, forward_ok=events * 19,
               source_full=5 if phase == "load" else 0, forward_full=20 if phase == "load" else 0,
               control_full=0, data_full=25 if phase == "load" else 0, terminal=events,
               gates=32 if phase == "load" else 0, skipped=events if phase == "load" else 0,
               cancelled=0, sems=2, stop_sent=20, stop_full=0, joined=20, closed=60,
               unlinked=60, heap_before_drain=20000, deferred_reclaimed=0,
               heap=20000, elapsed_ms=900, status=0)
    if phase == "cancel":
        row.update(source_ok=17, forward_ok=100, source_full=1, forward_full=3, data_full=4,
                   terminal=0, gates=1, skipped=17, cancelled=1, elapsed_ms=30, status=1,
                   heap_before_drain=24328, deferred_reclaimed=4328)
    return row


def batch():
    return dict(repetitions=3, calls=10, expected_cancellations=3, verified_events=6544,
                heap_before=20000, heap_after=20000, status=0,
                profiles=[profile(phase, index) for phase, index in STEPS])


def line(kind, row, fields):
    return kind + " " + " ".join(f"{key}={row[key]}" for key in fields)


def transcript():
    output = ["nsh> sq_c pressure"]
    for row in batch()["profiles"]:
        phase = row["phase"]
        output.append(line("SQ_PRESSURE_BEGIN", row, ("phase", "index")))
        output.append("SQ_MEMORY before=10000 full=130000 delta=120000 metadata=1600 payload=5440 stacks=81920")
        if phase != "cancel":
            n = 2048 if phase == "load" else 100
            output.append(f"SQ_RESULT services=20 queues=60 events={n} received={n} errors=0 mean_cycles=240 max_cycles=480 misses_1ms=0 source=messages")
        output.append(f"SQ_DONE status={row['status']} heap_after=20000")
        output.append(line("SQ_PRESSURE_END", row, END_FIELDS))
        if phase == "warmup":
            output.append("SQ_PRESSURE_BASELINE heap=20000")
    output.append(line("SQ_PRESSURE_BATCH", batch(), BATCH_FIELDS))
    output.append("nsh> ")
    return "\n".join(output)


def report():
    item = footprint_fixture()
    item.update(blocks=2, diagnostic_capture="pressure", harness_sha256={"tests/service-qualification/pressure.py": "a" * 64},
                runs=[dict(block=block, language=language, **batch()) for block in range(2)
                      for language in (("c", "rust") if block == 0 else ("rust", "c"))])
    for build in item["builds"].values():
        build.update(diagnostic_pressure=True, psram_enabled=False)
    return item


class PressureTests(unittest.TestCase):
    def test_complete_batch_counts_only_verified_non_cancelled_delivery(self):
        self.assertEqual(parse_pressure(transcript()), batch())
        self.assertEqual(parse_pressure("\x1b[32m" + transcript() + "\x1b[0m"), batch())

    def test_incomplete_duplicate_reordered_or_unsaturated_transcripts_fail(self):
        original = transcript()
        for text in (original.rsplit("SQ_PRESSURE_BATCH", 1)[0], original + "\nSQ_DONE status=0 heap_after=20000",
                     original.replace("source_full=5", "source_full=0", 1),
                     original.replace("forward_full=20", "forward_full=0", 1),
                     original.replace("SQ_PRESSURE_BEGIN phase=load index=0", "SQ_PRESSURE_BEGIN phase=load index=1", 1),
                     original.replace("received=2048", "received=2047", 1),
                     original.replace("stacks=81920", "stacks=0", 1),
                     original.replace("closed=60", "closed=59", 1),
                     original.replace("cancelled=1", "cancelled=0", 1),
                     original.replace("source_ok=17", "source_ok=-1", 1),
                     original.replace("SQ_DONE status=1", "SQ_DONE status=0", 1)):
            with self.assertRaises(ValueError):
                parse_pressure(text)

    def test_profile_guard_protects_pressure_delivery_cancellation_and_resources(self):
        for phase in ("load", "cancel", "recovery"):
            original = profile(phase, 0)
            for field in ("source_ok", "sems", "stop_full", "joined", "closed", "unlinked", "heap", "status",
                          "heap_before_drain", "deferred_reclaimed"):
                row = dict(original)
                row[field] += 1
                with self.subTest(phase=phase, field=field), self.assertRaises(ValueError):
                    check_profile(row, 20000)
        for field, value in (("gates", 0), ("skipped", 0), ("source_full", 0), ("elapsed_ms", 10000),
                             ("stop_sent", 0), ("data_full", -1), ("joined", True)):
            row = profile("load", 0)
            row[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_profile(row, 20000)

    def test_public_evidence_is_private_free_and_not_a_performance_claim(self):
        exported = public_report(report())
        self.assertEqual((exported["calls"], exported["expected_cancellations"], exported["verified_events"]), (40, 12, 26176))
        self.assertEqual((exported["services"], exported["queues"], exported["event_bytes"]), (20, 60, 16))
        self.assertFalse(exported["performance_claim"])
        self.assertFalse(exported["arbitrary_overload_qualified"])
        for private in ("/private", "backup_sha256", "transcript", "rustflags"):
            self.assertNotIn(private, json.dumps(exported))

    def test_bad_or_incomplete_public_matrix_and_changed_build_inputs_fail(self):
        for mutate in (lambda r: r.update(restoration_verified=False), lambda r: r["runs"].pop(),
                       lambda r: r["runs"].reverse(), lambda r: r.update(diagnostic_capture="faults"),
                       lambda r: r["runs"][0].update(heap_after=20001),
                       lambda r: r["runs"][0]["profiles"][1].update(source_full=0),
                       lambda r: r["builds"]["rust"].update(thread_stack=8192),
                       lambda r: r["builds"]["rust"].update(diagnostic_faults=True),
                       lambda r: r["builds"]["rust"].update(psram_enabled=True),
                       lambda r: r["builds"]["rust"].update(source_sha256={"/private/probe.c": "secret"}),
                       lambda r: r.update(harness_sha256={"../private.py": "secret"})):
            item = report()
            mutate(item)
            with self.assertRaises(ValueError):
                public_report(item)


if __name__ == "__main__":
    unittest.main()
