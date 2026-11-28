"""Negative controls for collector completeness, independent of GUI replay."""

import copy
import unittest

from build_replay_nuttx import hook_coverage
from qualify_replay_qemu import make_journal, validate_observations


RECORDING = "0123456789abcdef0123456789abcdef"
ELF = "a" * 64


def receipt_fixture():
    """Synthetic bounded no-op history; never passed off as target evidence."""
    region = {"start": 4096, "size": 1024}
    chunks = [{"start": 4096, "size": 48, "state": "reserved"},
              {"start": 4144, "size": 928, "state": "free"},
              {"start": 5072, "size": 48, "state": "reserved"}]
    events = []

    def event(kind, operation, sequence, begins, commits, safe):
        index = len(events) + 1
        item = {"kind": kind, "operation": operation, "sequence": sequence, "probe_sequence": index,
                "begin_count": begins, "commit_count": commits, "safe_count": safe,
                "time_ns": index * 100, "ticks": index * 2, "timer_ticks": index * 2 + 1,
                "timer_unchanged": True, "metadata_unchanged": True, "primask": 1, "basepri": 128,
                "xpsr": 0x01000000, "faults": 0,
                "task_id": 4, "heap_address": 4000, "region": copy.deepcopy(region),
                "chunks": copy.deepcopy(chunks), "deferred_payloads": [],
                "mutex_holder": 4 if kind in ("begin", "commit") else 0x7ffffffe}
        if kind in ("initial", "safe", "final"):
            item["independent_safe_heap"] = {"address": 4000, "deferred_payloads": [], "regions": [
                {**region, "chunks": [{"start": c["start"], "size": c["size"], "kind": c["state"]} for c in chunks]}]}
        events.append(item)

    event("initial", 0, 0, 0, 0, 0)
    for operation in range(1, 25):
        event("begin", operation, operation - 1, operation, operation - 1, operation - 1)
        event("commit", operation, operation, operation, operation, operation - 1)
        event("safe", operation, operation, operation, operation, operation)
    event("final", 24, 24, 24, 24, 24)
    raw = {"schema_version": 1, "recording_id": RECORDING, "elf_sha256": ELF,
           "target_function_calls": False, "events": events}
    console = (f"NXRS_REPLAY PASS recording_id={RECORDING} sequence=24 begins=24 commits=24 "
               f"safe_points=24 probes={len(events)} faults=0 arena_bytes=65536 owner=4\n").encode()
    return raw, console


class CollectorReceiptTests(unittest.TestCase):
    def validate(self, raw, console):
        return validate_observations(raw, console, RECORDING, ELF)

    def test_accepts_every_noop_release_and_retains_its_sequence(self):
        raw, console = receipt_fixture()
        self.assertEqual(self.validate(raw, console)["transactions"], 24)
        journal = make_journal(raw, {"elf_sha256": ELF, "nuttx_revision": "b" * 40, "config_sha256": "c" * 64})
        transactions = [record for record in journal["records"] if record["kind"] == "transaction"]
        self.assertEqual([record["sequence"] for record in transactions], list(range(1, 25)))
        self.assertTrue(all(record["expected"] == record["replacement"] for record in transactions))

    def test_drop_two_cancelling_operations_cannot_hide_behind_equal_endpoints(self):
        raw, console = receipt_fixture()
        self.assertEqual(raw["events"][0]["chunks"], raw["events"][-1]["chunks"])
        del raw["events"][1:7]
        with self.assertRaisesRegex(RuntimeError, "probe"):
            self.validate(raw, console)

    def test_duplicate_probe_is_not_timestamp_deduplicated(self):
        raw, console = receipt_fixture()
        raw["events"].insert(2, copy.deepcopy(raw["events"][1]))
        with self.assertRaisesRegex(RuntimeError, "probe"):
            self.validate(raw, console)

    def test_reordered_release_and_acquisition_fail(self):
        raw, console = receipt_fixture()
        raw["events"][1:3] = reversed(raw["events"][1:3])
        with self.assertRaises(RuntimeError):
            self.validate(raw, console)

    def test_missing_final_receipt_fails(self):
        raw, console = receipt_fixture()
        raw["events"].pop()
        with self.assertRaisesRegex(RuntimeError, "initial/final"):
            self.validate(raw, console)

    def test_success_code_without_actual_native_mutex_ownership_fails(self):
        raw, console = receipt_fixture()
        raw["events"][1]["mutex_holder"] = 0x7ffffffe
        with self.assertRaisesRegex(RuntimeError, "mutex"):
            self.validate(raw, console)

    def test_native_counter_gap_fails_even_if_geometry_is_identical(self):
        raw, console = receipt_fixture()
        raw["events"][2]["sequence"] += 1
        with self.assertRaisesRegex(RuntimeError, "sequence"):
            self.validate(raw, console)

    def test_geometry_change_outside_lock_window_fails(self):
        raw, console = receipt_fixture()
        raw["events"][4]["chunks"][1]["state"] = "allocated"
        with self.assertRaisesRegex(RuntimeError, "acquisition"):
            self.validate(raw, console)

    def test_independent_safe_point_disagreement_fails(self):
        raw, console = receipt_fixture()
        raw["events"][3]["independent_safe_heap"]["regions"][0]["chunks"][1]["kind"] = "allocation"
        with self.assertRaisesRegex(RuntimeError, "Independent raw"):
            self.validate(raw, console)

    def test_wrong_recording_and_elf_fail(self):
        for field in ("recording_id", "elf_sha256"):
            with self.subTest(field=field):
                raw, console = receipt_fixture()
                raw[field] = "0" * len(raw[field])
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    self.validate(raw, console)

    def test_clock_conversion_backward_time_and_unmasked_probes_fail(self):
        for field, value in (("time_ns", 201), ("ticks", 0), ("primask", 0), ("timer_unchanged", False),
                             ("xpsr", 0x01000011), ("faults", 1)):
            with self.subTest(field=field):
                raw, console = receipt_fixture()
                raw["events"][2][field] = value
                with self.assertRaises(RuntimeError):
                    self.validate(raw, console)

    def test_heap_region_or_owner_change_fails(self):
        for field, value in (("heap_address", 5000), ("region", {"start": 4096, "size": 2048}), ("task_id", 5)):
            with self.subTest(field=field):
                raw, console = receipt_fixture()
                raw["events"][2][field] = value
                with self.assertRaisesRegex(RuntimeError, "identity, bounds or owning task"):
                    self.validate(raw, console)

    def test_uart_footer_must_close_every_probe_and_transaction(self):
        raw, console = receipt_fixture()
        for original, replacement in ((b"probes=74", b"probes=73"), (b"commits=24", b"commits=23"),
                                      (b"faults=0", b"faults=1"), (b"PASS", b"FAIL")):
            with self.subTest(original=original):
                with self.assertRaises(RuntimeError):
                    self.validate(raw, console.replace(original, replacement))


def linked_calls():
    text = []
    helper_calls = {"mm_malloc": ["mm_addfreechunk"], "mm_delayfree": ["mm_addfreechunk"],
                    "mm_realloc": ["mm_addfreechunk", "mm_addfreechunk", "mm_shrinkchunk"],
                    "mm_memalign": ["mm_addfreechunk", "mm_shrinkchunk"]}
    for name, locks, unlocks in (("mm_malloc", 1, 1), ("mm_delayfree", 1, 2),
                                 ("mm_realloc", 1, 3), ("mm_memalign", 1, 1)):
        text.append("10000000 <" + name + ">:")
        text.extend(" 10000000: f000 f000 bl 10000100 <__wrap_mm_lock>" for _ in range(locks))
        text.extend(" 10000000: f000 f000 bl 10000200 <__wrap_mm_unlock>" for _ in range(unlocks))
        text.extend(" 10000000: f000 f000 bl 10000300 <" + target + ">" for target in helper_calls[name])
    for name in ("mm_lock", "mm_unlock"):
        text.extend(("10000100 <__wrap_" + name + ">:", " 10000100: f000 f000 bl 10000300 <" + name + ">"))
    text.extend(("10000400 <replay_probe_stop>:", "10000500 <heap_replay_main>:",
                 " 10000500: f000 f000 bl 10000300 <mm_uninitialize>",
                 "10000600 <mm_addregion>:", " 10000600: f000 f000 bl 10000300 <mm_addfreechunk>",
                 "10000700 <mm_shrinkchunk>:", " 10000700: f000 f000 bl 10000300 <mm_addfreechunk>",
                 " 10000700: f000 f000 bl 10000300 <mm_addfreechunk>",
                 "10000800 <mm_initialize_heap>:", " 10000800: f000 f000 bl 10000300 <mm_addregion>"))
    return "\n".join(text)


class FinalElfCoverageTests(unittest.TestCase):
    def test_expected_linked_calls_are_admitted(self):
        self.assertFalse(hook_coverage(linked_calls())["unwrapped_mutex_calls"])

    def test_removing_an_allocator_release_hook_fails(self):
        source = linked_calls().replace("<__wrap_mm_unlock>", "<unrelated>", 1)
        with self.assertRaisesRegex(RuntimeError, "coverage"):
            hook_coverage(source)

    def test_any_direct_real_mutex_bypass_fails(self):
        source = linked_calls() + "\n10000600 <bypass>:\n 10000600: f000 f000 bl 10000300 <mm_lock>"
        with self.assertRaisesRegex(RuntimeError, "Unwrapped"):
            hook_coverage(source)

    def test_wrapper_presence_without_real_mutex_call_fails(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one real"):
            hook_coverage(linked_calls().replace("<mm_lock>\n", "<not_a_mutex>\n"))

    def test_unadmitted_geometry_helper_caller_fails(self):
        source = linked_calls() + "\n10000900 <bypass>:\n 10000900: f000 f000 bl 10000300 <mm_shrinkchunk>"
        with self.assertRaisesRegex(RuntimeError, "geometry helper"):
            hook_coverage(source)


if __name__ == "__main__":
    unittest.main()
