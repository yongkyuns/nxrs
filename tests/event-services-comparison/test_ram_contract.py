"""Reject missing, contradictory, or fabricated stack measurement evidence."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("ram_contract_tests", Path(__file__).with_name("ram_contract.py"))
ram = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ram)


def worker(index=0, **changes):
    row = dict(scope="worker", id=index, requested=2048, reserved=2016,
               used=1536, available=1, capture="before_thread_return")
    row.update(changes)
    return "ES_STACK " + " ".join(f"{key}={value}" for key, value in row.items())


class StackEvidenceTests(unittest.TestCase):
    def test_requested_and_usable_capacity_are_distinct(self):
        values = ram.stack_rows(worker(), "nuttx-rust", workers=1,
                                configuration={"worker_stack_bytes": 2048}, require_observed=True)
        self.assertEqual(values[0]["reserved"], 2016)
        self.assertEqual(ram.stack_summary(values)["minimum_observed_remaining_bytes"], 480)
        self.assertFalse(ram.stack_summary(values)["worst_case_bound"])

    def test_missing_and_duplicate_workers_are_rejected(self):
        for text in (worker(), worker() + "\n" + worker(), worker(20)):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ram.stack_rows(text, "nuttx-c", workers=2)

    def test_unavailable_is_not_zero_stack_use(self):
        text = worker(available=0, used=0, capture="unavailable")
        values = ram.stack_rows(text, "nuttx-c", workers=1)
        self.assertIsNone(ram.stack_summary(values)["minimum_observed_remaining_bytes"])
        with self.assertRaisesRegex(ValueError, "unavailable"):
            ram.stack_rows(text, "nuttx-c", workers=1, require_observed=True)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            ram.stack_rows(worker(available=0), "nuttx-c", workers=1)

    def test_contradictory_measurements_and_build_identity_fail(self):
        for fields in ({"used": 3000}, {"used": -1}, {"available": 2},
                       {"guard_ok": 0}, {"capture": ""}, {"used": 0}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ram.stack_rows(worker(**fields), "nuttx-c", workers=1)
        with self.assertRaisesRegex(ValueError, "frozen"):
            ram.stack_rows(worker(), "nuttx-c", workers=1,
                           configuration={"worker_stack_bytes": 4096})

    def test_duplicate_fields_and_extra_primary_scope_fail(self):
        with self.assertRaisesRegex(ValueError, "duplicate field"):
            ram.stack_rows(worker() + " used=2", "nuttx-c", workers=1)
        text = worker(scope="shared", reserved=8192, requested=8192,
                      capture="post-main-cumulative", guard_ok=1, painted_bytes=7040,
                      unpainted_bytes=1024, protected_bytes=128, guard_bytes=64, sp_gap_bytes=256)
        values = ram.stack_rows(text, "embassy", configuration={"shared_stack_bytes": 8192})
        self.assertEqual(values[0]["capture"], "post-main-cumulative")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            ram.stack_rows(text + "\n" + worker(), "embassy")

    def test_shared_coverage_cannot_be_missing_or_claim_disabled_measurement(self):
        incomplete = worker(scope="shared", requested=8192, reserved=8192,
                            capture="post-main-cumulative")
        with self.assertRaises(ValueError):
            ram.stack_rows(incomplete, "embassy")
        with self.assertRaisesRegex(ValueError, "coverage"):
            ram.stack_rows(worker(capture="disabled"), "nuttx-c", workers=1)
        # An untouched IRQ stack is possible; unavailable and zero remain distinct.
        irq = worker(scope="irq", used=0, capture="report_snapshot")
        self.assertEqual(ram.stack_rows(irq, "nuttx-c", workers=0)[0]["available"], 1)


def allocation(profile, platform="nuttx-c", queues=60):
    count = int(profile.rsplit("-", 1)[1]) if profile.startswith("alloc-threads-") else 0
    stages = (["baseline", "threads", "threads-joined", "cleanup"] if profile.startswith("alloc-threads-")
              else ["baseline", "queues-empty", "queues-full", "queues-drained", "cleanup"]
              if profile == "alloc-queues" else ["baseline"])
    static = platform == "zephyr-c"
    lines = []
    for stage in stages:
        live = count if stage == "threads" else 0
        active = queues if stage.startswith("queues-") else 0
        row = dict(stage=stage, heap_used=1000 + live * 2400 + (37760 if stage == "queues-full" else 0),
                   available=1, requested_workers=count, live_workers=live,
                   worker_stack_requested=live * 2048,
                   worker_stack_reserved=20 * 2080 if static else 0,
                   worker_stack_reservation_available=int(static),
                   queue_payload_capacity=30720 if static or active else 0,
                   static_storage=40000 if static else 5000, diagnostics=244,
                   static_scope="adapter", active_queues=active, storage="static" if static else "dynamic")
        lines.append("ES_ALLOC " + " ".join(f"{key}={value}" for key, value in row.items()))
    lines.extend(worker(i) for i in range(count))
    lines.extend([f"ES_ALLOC_RESULT platform={platform} profile={profile} requested_workers={count} errors=0",
                  "ES_PASS", "ES_COMMAND_EXIT status=0"])
    return "\n".join(lines)


class AllocationEvidenceTests(unittest.TestCase):
    def test_all_controls_keep_static_reservations_and_runtime_deltas_distinct(self):
        for platform in ("nuttx-c", "nuttx-rust", "zephyr-c"):
            for queues in (20, 60):
                for profile in ram.ALLOC_PROFILES:
                    with self.subTest(platform=platform, queues=queues, profile=profile):
                        result = ram.allocation_output(allocation(profile, platform, queues), profile, platform,
                            {"worker_stack_bytes": 2048, "queues": queues})
                        if profile != "alloc-baseline":
                            self.assertEqual(result["live_heap_delta_from_baseline"]["cleanup"], 0)
                        if profile == "alloc-queues":
                            self.assertEqual(result["live_heap_delta_from_baseline"]["queues-full"], 37760)
                        if platform == "zephyr-c":
                            self.assertEqual(result["phases"][0]["worker_stack_reserved"], 41600)
                            self.assertEqual(result["phases"][0]["queue_payload_capacity"], 30720)

    def test_missing_lifecycle_and_false_reservations_are_rejected(self):
        original = allocation("alloc-threads-1")
        bad = [original.replace("stage=cleanup", "stage=threads"),
               original.replace("worker_stack_reserved=0", "worker_stack_reserved=2048"),
               original.replace("live_workers=1", "live_workers=0"),
               original.replace("ES_COMMAND_EXIT status=0", "ES_COMMAND_EXIT status=1"),
               original.replace(worker(), "")]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                ram.allocation_output(text, "alloc-threads-1", "nuttx-c",
                                      {"worker_stack_bytes": 2048, "queues": 60})

    def test_unavailable_heap_is_not_a_zero_delta(self):
        text = allocation("alloc-baseline").replace("heap_used=1000 available=1", "heap_used=0 available=0")
        result = ram.allocation_output(text, "alloc-baseline", "nuttx-c",
                                      {"worker_stack_bytes": 2048, "queues": 60})
        self.assertIsNone(result["live_heap_delta_from_baseline"]["baseline"])


if __name__ == "__main__":
    unittest.main()
