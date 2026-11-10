"""Reject partial evidence and host/native or capacity relabelling."""
import copy
import json
import unittest

from report import parse


def baseline():
    case = dict(schema=1, domain="host-functional", case="baseline", transport="none",
                owners=0, workers=0, stack_request_per_worker=0, stack_request_total=0,
                queues=0, slots_per_queue=0, event_bytes=64, payload_capacity_bytes=0,
                native_heap_available=False, native_stack_available=False, workers_joined=0,
                valid=True, payload_valid=True, released_rust_bytes_delta=0, released_rust_blocks_delta=0)
    sample = dict(rust_live=64, rust_blocks=1, rust_peak_process=100,
                  alloc=2, zeroed=0, realloc=0, dealloc=1,
                  native_heap_used=None, native_heap_free=None, native_heap_arena=None,
                  main_stack_usable=None, main_stack_observed=None)
    return [case, [dict(sample, phase="baseline"), dict(sample, phase="released")], [],
            dict(stack_usable=None, stack_observed=None)]


def encode(data, complete=True):
    case, samples, workers, stack = data
    lines = ["RAM_CASE " + json.dumps(case)]
    lines += ["RAM_SAMPLE " + json.dumps(sample) for sample in samples]
    lines += ["RAM_WORKER " + json.dumps(worker) for worker in workers]
    lines += ["RAM_REPORT_STACK " + json.dumps(stack)]
    if complete:
        lines += ["RAM_END valid=true"]
    return "\n".join(lines)


class ReportTests(unittest.TestCase):
    def test_host_baseline_is_explicitly_not_native_ram(self):
        result = parse(encode(baseline()), "host-functional")
        self.assertFalse(result["accounting"]["linked_sections_included"])
        self.assertIsNone(result["deltas"][0]["native_heap_used_from_baseline"])

    def test_missing_completion_and_duplicate_phase_are_rejected(self):
        with self.assertRaises(ValueError):
            parse(encode(baseline(), complete=False), "host-functional")
        data = baseline()
        data[1].insert(1, copy.deepcopy(data[1][0]))
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")

    def test_host_bytes_cannot_be_relabelled_as_native_observations(self):
        data = baseline()
        data[1][0]["native_heap_used"] = 100
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")
        with self.assertRaises(ValueError):
            parse(encode(baseline()), "nuttx-target")

    def test_target_records_require_real_stack_fields(self):
        data = baseline()
        data[0].update(domain="nuttx-target", native_heap_available=True, native_stack_available=True)
        with self.assertRaises(ValueError):
            parse(encode(data), "nuttx-target")
        for sample in data[1]:
            sample.update(native_heap_used=1000, native_heap_free=9000, native_heap_arena=10000,
                          main_stack_usable=4096, main_stack_observed=1200)
        data[3].update(stack_usable=4096, stack_observed=1600)
        self.assertEqual(parse(encode(data), "nuttx-target")["reporting_stack"]["stack_observed"], 1600)

    def test_changed_baseline_and_counter_rollback_are_rejected(self):
        data = baseline()
        data[0].update(owners=1)
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")
        data = baseline()
        data[1][-1]["alloc"] = 1
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")

    def test_unreclaimed_baseline_is_rejected_even_when_delta_matches(self):
        data = baseline()
        data[1][-1]["rust_live"] += 8
        data[0]["released_rust_bytes_delta"] = 8
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")

    def test_records_after_completion_are_rejected(self):
        text = encode(baseline()) + "\nRAM_SAMPLE " + json.dumps(baseline()[1][0])
        with self.assertRaises(ValueError):
            parse(text, "host-functional")


if __name__ == "__main__":
    unittest.main()
