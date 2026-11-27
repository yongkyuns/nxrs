"""Negative controls for independently linked native checkpoint evidence."""

import copy
import unittest

from qualify_lifecycle_qemu import CLOCK, bind_capture, validate_heap_lifecycle, validate_markers


RECORDING = "1234567890abcdef1234567890abcdef"
ELF = "a" * 64
CAPTURE = f"""NXRS_TIMING_CLOCK frequency_hz=20000000 before=10 after=200000
NXRS_NOTE_BEGIN schema=1 source=nuttx-noteram-ascii clock={CLOCK} clock_bits=32 clock_epoch=fixture-invocation frequency_hz=20000000 cpus=1 mode=0 notes_lost=0 recording_id={RECORDING} capture_mode=stopped-checkpoints checkpoints=4
main-4 [0] 1.100000000: tracing_mark_write: B|4|work
main-4 [0] 1.100000050: sched_switch: prev_pid=0 ==> next_pid=4
main-4 [0] 1.100000100: irq_handler_entry: irq=15
main-4 [0] 1.100000150: irq_handler_exit: irq=15
main-4 [0] 1.100000200: tracing_mark_write: I|4|memory/checkpoint/1
main-4 [0] 1.100000250: tracing_mark_write: I|4|memory/checkpoint/2
main-4 [0] 1.100000300: tracing_mark_write: I|4|memory/checkpoint/3
main-4 [0] 1.100000350: tracing_mark_write: I|4|memory/checkpoint/4
main-4 [0] 1.100000400: tracing_mark_write: E|4|work
NXRS_NOTE_END schema=1 mode=0 notes_lost=0 complete=1 recording_id={RECORDING}
NXRS_TIMING_WITNESS PASS rounds=4
"""


def stopped_report():
    return {"recording_id": RECORDING, "snapshots": [
        {"checkpoint_id": identifier, "recording_id": RECORDING,
         "metadata_unchanged": True, "timer_unchanged_while_stopped": True,
         "unbound_file": f"unbound-{identifier}.json", "raw_file": f"raw-{identifier}.json",
         "marker": {"source_time_ns": 1_100_000_150 + identifier * 50,
                    "clock_ticks": (1_100_000_150 + identifier * 50) // 50,
                    "name": f"memory/checkpoint/{identifier}", "task_id": 4, "cpu": 0}}
        for identifier in range(1, 5)]}


def heap_points():
    allocated = [{"start": start, "size": size, "kind": "allocation"}
                 for start, size in ((1000, 16432), (17432, 32816), (50248, 16432))]
    hole = copy.deepcopy(allocated)
    hole[1]["kind"] = "free"
    free = [{"start": 900, "size": 100000, "kind": "free"}]
    return [{"heaps": [{"regions": [{"chunks": layout}]}],
             "checkpoint": {"block_pointers": pointers}}
            for layout, pointers in zip((free, allocated, hole, free),
                                       ([0, 0, 0], [1048, 17480, 50296], [1048, 0, 50296], [0, 0, 0]))]


class MarkerEvidenceChecks(unittest.TestCase):
    def test_exact_native_and_uart_checkpoints_are_admitted(self):
        timing, markers = validate_markers(CAPTURE.encode(), stopped_report(), RECORDING)
        self.assertEqual(set(markers), {1, 2, 3, 4})
        self.assertEqual(markers[2]["source_time_ns"], 1_100_000_250)
        self.assertEqual(timing["lost_records"], 0)

    def test_recording_identity_must_match_at_both_frame_endpoints(self):
        for index in (0, 1):
            text = CAPTURE.split("recording_id=")
            text[index + 1] = text[index + 1].replace(RECORDING, "f" * 32, 1)
            with self.subTest(index=index), self.assertRaisesRegex(RuntimeError, "identity"):
                validate_markers("recording_id=".join(text).encode(), stopped_report(), RECORDING)

    def test_native_recording_cannot_be_relabelled(self):
        report = stopped_report()
        report["snapshots"][0]["recording_id"] = "f" * 32
        with self.assertRaisesRegex(RuntimeError, "identity or quiescence"):
            validate_markers(CAPTURE.encode(), report, RECORDING)

    def test_marker_timestamp_and_task_must_match_stopped_native_note(self):
        for field, value in (("source_time_ns", 1_100_000_201), ("task_id", 5), ("cpu", 1),
                             ("clock_ticks", 12)):
            report = stopped_report()
            report["snapshots"][0]["marker"][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                validate_markers(CAPTURE.encode(), report, RECORDING)

    def test_missing_duplicate_and_noncanonical_markers_are_rejected(self):
        line = "main-4 [0] 1.100000250: tracing_mark_write: I|4|memory/checkpoint/2\n"
        for text in (CAPTURE.replace(line, ""), CAPTURE.replace(line, line + line),
                     CAPTURE.replace("checkpoint/2", "checkpoint/02")):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                validate_markers(text.encode(), stopped_report(), RECORDING)

    def test_incomplete_native_snapshot_sequence_is_rejected(self):
        report = stopped_report()
        report["snapshots"].pop(1)
        with self.assertRaisesRegex(RuntimeError, "all four"):
            validate_markers(CAPTURE.encode(), report, RECORDING)

    def test_target_timer_motion_and_metadata_changes_reject_linking(self):
        for field in ("metadata_unchanged", "timer_unchanged_while_stopped"):
            report = stopped_report()
            report["snapshots"][1][field] = False
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "quiescence"):
                validate_markers(CAPTURE.encode(), report, RECORDING)

    def test_shared_clock_name_without_epoch_and_mode_is_insufficient(self):
        for before, after in (("clock_bits=32", "clock_bits=64"),
                              ("clock_epoch=fixture-invocation", "clock_epoch=boot"),
                              ("capture_mode=stopped-checkpoints", "capture_mode=running")):
            with self.subTest(before=before), self.assertRaises(RuntimeError):
                validate_markers(CAPTURE.replace(before, after).encode(), stopped_report(), RECORDING)


class HeapLifecycleChecks(unittest.TestCase):
    def test_addressed_allocated_hole_and_release_states_are_admitted(self):
        result = validate_heap_lifecycle(heap_points())
        self.assertTrue(result["middle_payload_observed_free_while_neighbors_live"])

    def test_ownership_flag_alone_does_not_establish_a_free_chunk(self):
        points = heap_points()
        points[2]["heaps"][0]["regions"][0]["chunks"][1]["kind"] = "allocation"
        with self.assertRaisesRegex(RuntimeError, "observed free"):
            validate_heap_lifecycle(points)

    def test_allocation_payload_must_fit_inside_real_chunk(self):
        points = heap_points()
        points[1]["heaps"][0]["regions"][0]["chunks"][1]["size"] = 128
        with self.assertRaisesRegex(RuntimeError, "contained by one allocated chunk"):
            validate_heap_lifecycle(points)

    def test_retained_neighbors_must_remain_allocated(self):
        points = heap_points()
        points[2]["heaps"][0]["regions"][0]["chunks"][2]["kind"] = "free"
        with self.assertRaisesRegex(RuntimeError, "still allocated"):
            validate_heap_lifecycle(points)

    def test_request_order_does_not_establish_address_contiguity(self):
        points = heap_points()
        points[1]["heaps"][0]["regions"][0]["chunks"][1]["start"] += 8
        with self.assertRaisesRegex(RuntimeError, "contiguous address-ordered"):
            validate_heap_lifecycle(points)


class CaptureBindingChecks(unittest.TestCase):
    def capture(self):
        return {"build": {"elf_sha256": ELF},
                "snapshots": [{"time_ns": 1_100_000_200, "spans": []}],
                "warnings": ["Snapshot coordinates are supplied manually; not sampled."],
                "capture": {"limits": ["This snapshot makes no claim about changes between capture points."]}}

    def test_bound_document_preserves_original_and_exact_time(self):
        capture = self.capture()
        result = bind_capture(capture, stopped_report()["snapshots"][0], RECORDING, ELF)
        self.assertNotIn("correlation", capture["snapshots"][0])
        self.assertEqual(result["snapshots"][0]["time_ns"], 1_100_000_200)
        self.assertEqual(result["snapshots"][0]["correlation"],
                         {"recording_id": RECORDING, "clock": CLOCK, "checkpoint_id": 1})
        self.assertIn("between capture points", result["capture"]["limits"][0])

    def test_wrong_build_or_coordinate_cannot_be_bound(self):
        for key in ("elf", "time"):
            capture = self.capture()
            if key == "elf":
                capture["build"]["elf_sha256"] = "b" * 64
            else:
                capture["snapshots"][0]["time_ns"] += 1
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                bind_capture(capture, stopped_report()["snapshots"][0], RECORDING, ELF)


if __name__ == "__main__":
    unittest.main()
