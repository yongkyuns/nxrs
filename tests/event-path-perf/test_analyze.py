"""Corruption controls for P2 evidence; specimens are explicitly synthetic."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "event_path_analysis", Path(__file__).with_name("analyze.py"))
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def encode(rows):
    return "synthetic functional specimen; not target execution evidence\n" + "\n".join(
        analysis.PREFIX + json.dumps(row) for row in rows) + "\n"


def specimen(case="ready-std-try", *, native=False, payload="bytes16", count=3, warmup=1):
    family, transport, admission, receive_api, arms, providers = analysis.CASES[case]
    control = family in analysis.CONTROL_FAMILIES
    payload = "none" if control else payload
    capacity = 0 if control else 8 if payload == "bytes16" else 16
    common = {"schema": analysis.SCHEMA, "run_id": "synthetic-run"}
    metadata = {
        **common, "record": "experiment", "case": case, "family": family,
        "transport": transport, "admission": admission, "receive_api": receive_api,
        "payload": payload, "payload_bytes": 0 if control else 16 if payload == "bytes16" else 48,
        "payload_alignment": 0 if control else 4 if payload == "bytes16" else 8,
        "capacity": capacity, "provider_count": providers, "selector_arms": arms,
        "stop_capacity": 1 if arms == 3 else 0, "important_capacity": capacity if arms == 3 else 0,
        "active_arm": "ordinary", "samples": count, "warmup": warmup,
        "runtime_kind": "nuttx-unqualified" if native else "host-functional-only",
        "build_id": "a" * 64 if native else "unrecorded-host",
        "clock_hz": 240_000_000 if native else 1_000_000_000,
        "clock_source": 5 if native else 4, "counter_bits": 32,
        "counter_native_bits": 32 if native else 64, "monotonic_resolution_ns": 1,
        "clock_source_request": "native-counter" if native else "baseline",
        "crossbeam_version": "0.5.17", "witness_hooks": False, "worker_stack": 16384,
        "blocked_state_name": "wait_sem",
        "release_schedule": "finite-closed-loop-or-unscheduled-fanin", "counts_include_warmup": True,
    }
    threaded = family in analysis.THREAD_FAMILIES
    sender_tid, receiver_tid = 101, 102 if threaded else 101
    sender_priority = 120 if native else 0
    receiver_priority = (180 if family in analysis.WAIT_FAMILIES | {"finite-fanin"} else 120) if native else 0
    scheduling = {
        **common, "record": "scheduling", "sender_tid": sender_tid, "receiver_tid": receiver_tid,
        "sender_policy": 1 if native else 0, "sender_priority": sender_priority,
        "receiver_policy": 1 if native else 0, "receiver_priority": receiver_priority,
        "single_core_verified": native, "peer_ready_verified": family == "fixed-peer",
    }
    provider_rows = []
    if family == "finite-fanin":
        for provider in range(providers):
            provider_rows.append({**common, "record": "provider", "provider": provider,
                                  "tid": sender_tid if provider == 0 else 200 + provider,
                                  "policy": 1 if native else 0, "priority": sender_priority})
    samples = []
    total_checksum = retained_checksum = admitted = rejected = park_returns = 0
    for provider in range(providers):
        for sequence in range(1, count + warmup + 1):
            accepted = family != "full-rejection" and not (family == "finite-fanin" and sequence % 2 == 0)
            if accepted:
                admitted += 1
                checksum = analysis.payload_checksum(payload, sequence, provider)
            else:
                rejected += 1
                checksum = (sum(analysis.payload_checksum(payload, slot, 1) for slot in range(capacity))
                            if family == "full-rejection" else 0)
            total_checksum = (total_checksum + checksum) & analysis.U64
            returns = 1 if family == "park" else 0
            park_returns += returns
            if sequence <= warmup:
                continue
            retained_checksum = (retained_checksum + checksum) & analysis.U64
            start = 100_000 + sequence * 1000 + provider * 10_000
            admission_return = start + 100
            if family == "fixed-peer":
                receive = start + 50
            else:
                receive = start + 180 if accepted else 0
            samples.append({
                **common, "record": "sample", "index": len(samples), "sequence": sequence,
                "provider": provider, "start_ticks": start, "admission_return_ticks": admission_return,
                "receive_start_ticks": start + 120 if family == "ready" else None,
                "receive_return_ticks": receive, "elapsed_upper_ns": 2000,
                "blocked_verified": native and family in analysis.WAIT_FAMILIES,
                "peer_tid": receiver_tid if control else 0, "park_returns": returns,
                "accepted": accepted, "payload_valid": True,
            })
    result = {
        **common, "record": "result", "success": True, "sample_count": len(samples),
        "attempted": (count + warmup) * providers, "admitted": admitted, "rejected": rejected,
        "handled": admitted, "checksum": total_checksum, "retained_checksum": retained_checksum,
        "park_returns": park_returns, "threads_joined": True,
        "terminal_quiescence_verified": native and threaded, "structural_valid": True,
        "timing_qualified": False, "priority_progress_qualified": False,
        "physical_execution_verified": False, "clock_qualified": False, "actual_switch_count": None,
    }
    return [metadata, *provider_rows, scheduling, *samples, result]


def first(rows, kind):
    return next(row for row in rows if row["record"] == kind)


class ValidSpecimens(unittest.TestCase):
    def test_every_named_case_has_a_complete_synthetic_specimen(self):
        for case in analysis.CASES:
            with self.subTest(case=case):
                rows = specimen(case, native=case == "fixed-peer")
                result = analysis.analyze(encode(rows))
                self.assertTrue(result["structural_valid"])
                self.assertFalse(result["timing_qualified"])
                self.assertFalse(result["priority_progress_qualified"])
                self.assertNotIn("p50_ns", result["raw_ticks"]["admission_call"])
                self.assertEqual(result["raw_ticks"]["admission_call"]["p50_ticks"], 100)

    def test_target_block_selected_notification_fanin_and_normalized_payload(self):
        for case in ("blocked-messaging-send", "blocked-select-three", "semaphore", "park",
                     "fanin-crossbeam-4", "ready-messaging-try", "full-crossbeam"):
            with self.subTest(case=case):
                result = analysis.analyze(encode(specimen(case, native=True, payload="imu-sample")))
                self.assertFalse(result["clock_qualified"])
                self.assertEqual(result["outcome"], "nuttx-unqualified")

    def test_valid_counter_wrap_preserves_unsigned_differences(self):
        rows = specimen(count=1, warmup=0)
        sample = first(rows, "sample")
        sample.update(start_ticks=analysis.U32 - 100, admission_return_ticks=analysis.U32,
                      receive_start_ticks=19, receive_return_ticks=79)
        result = analysis.analyze(encode(rows))
        self.assertEqual(result["raw_ticks"]["ready_receive_call"]["p50_ticks"], 60)

    def test_full_rejections_and_fanin_rejections_keep_all_raw_attempts(self):
        full = analysis.analyze(encode(specimen("full-std")))
        self.assertEqual(full["retained_rejected"], 3)
        self.assertIsNone(full["raw_ticks"]["admission_to_receive_return"])
        fanin = analysis.analyze(encode(specimen("fanin-messaging-4", warmup=2)))
        self.assertEqual(len(fanin["samples"]), 12)
        self.assertEqual(fanin["retained_admitted"], 8)
        self.assertEqual(fanin["retained_rejected"], 4)
        self.assertFalse(fanin["total_checksum_reconstructed"])


class CorruptionControls(unittest.TestCase):
    def reject_mutation(self, case, kind, key, value, *, native=False):
        rows = specimen(case, native=native)
        first(rows, kind)[key] = value
        with self.assertRaises(ValueError):
            analysis.analyze(encode(rows))

    def test_named_operations_topology_and_layout_cannot_be_relabelled(self):
        for key, value in (("case", "context-switch"), ("family", "blocked"),
                           ("transport", "benchmark-only-queue"), ("admission", "send"),
                           ("receive_api", "recv-timeout-10ms"), ("capacity", 0),
                           ("selector_arms", 3), ("important_capacity", 8),
                           ("provider_count", 4), ("payload_bytes", 20),
                           ("payload_alignment", 8), ("samples", 0), ("samples", 4097),
                           ("warmup", 1025), ("crossbeam_version", "0.5.16"),
                           ("counts_include_warmup", False), ("witness_hooks", True)):
            with self.subTest(key=key, value=value):
                self.reject_mutation("ready-std-try", "experiment", key, value)

    def test_missing_rejected_raw_attempt_and_duplicate_provider_sequence_fail(self):
        rows = specimen("fanin-std-4")
        rejected = next(row for row in rows if row["record"] == "sample" and not row["accepted"])
        rows.remove(rejected)
        with self.assertRaisesRegex(ValueError, "retained samples"):
            analysis.analyze(encode(rows))
        rows = specimen("fanin-std-4")
        samples = [row for row in rows if row["record"] == "sample"]
        samples[1]["sequence"] = samples[0]["sequence"]
        with self.assertRaisesRegex(ValueError, "sequence"):
            analysis.analyze(encode(rows))

    def test_payload_counts_checksums_and_original_rejection_validation_fail_closed(self):
        for case, kind, key, value in (
                ("ready-std-try", "sample", "payload_valid", False),
                ("blocked-std-try", "result", "handled", 3),
                ("full-messaging", "sample", "accepted", True),
                ("full-messaging", "sample", "receive_return_ticks", 1),
                ("full-messaging", "result", "retained_checksum", 1),
                ("ready-crossbeam-try", "result", "checksum", 1),
                ("fanin-crossbeam-4", "result", "attempted", 20),
                ("fanin-crossbeam-4", "result", "retained_checksum", 1)):
            with self.subTest(case=case, key=key):
                self.reject_mutation(case, kind, key, value)

    def test_raw_counter_bounds_reversed_order_and_coarse_guard_are_checked(self):
        rows = specimen()
        start = first(rows, "sample")["start_ticks"]
        for key, value in (("start_ticks", -1), ("admission_return_ticks", 1 << 32),
                           ("admission_return_ticks", start - 1),
                           ("receive_start_ticks", start + 50),
                           ("elapsed_upper_ns", 100), ("elapsed_upper_ns", 1 << 31),
                           ("elapsed_upper_ns", True)):
            with self.subTest(key=key):
                self.reject_mutation("ready-std-try", "sample", key, value)
        for key, value in (("clock_source", 5), ("clock_source_request", "native-counter"),
                           ("clock_hz", 240_000_000), ("counter_bits", 64)):
            with self.subTest(key=key):
                self.reject_mutation("ready-std-try", "experiment", key, value)

    def test_native_wait_schedule_peer_and_terminal_proofs_are_required(self):
        for case, kind, key, value in (
                ("blocked-select-three", "experiment", "blocked_state_name", "ready"),
                ("blocked-select-three", "sample", "blocked_verified", False),
                ("blocked-std-send", "scheduling", "receiver_priority", 120),
                ("blocked-std-send", "result", "terminal_quiescence_verified", False),
                ("park", "sample", "park_returns", 0),
                ("park", "sample", "peer_tid", 101),
                ("fixed-peer", "scheduling", "peer_ready_verified", False),
                ("fixed-peer", "scheduling", "receiver_priority", 180),
                ("fanin-std-4", "provider", "tid", 102)):
            with self.subTest(case=case, key=key):
                self.reject_mutation(case, kind, key, value, native=True)

    def test_actual_fanin_storage_limit_is_not_the_single_transfer_limit(self):
        for key, value in (("samples", 257), ("warmup", 65)):
            with self.subTest(key=key):
                self.reject_mutation("fanin-std-4", "experiment", key, value)

    def test_notification_publication_cannot_be_relabelled_as_pure_native_call(self):
        self.reject_mutation("semaphore", "experiment", "admission", "sem-post")
        self.reject_mutation("fixed-peer", "experiment", "admission", "yield-now", native=True)

    def test_host_cannot_fabricate_target_blocking_or_quiescence(self):
        self.reject_mutation("blocked-std-try", "sample", "blocked_verified", True)
        self.reject_mutation("park", "result", "terminal_quiescence_verified", True)
        with self.assertRaisesRegex(ValueError, "host cannot establish"):
            analysis.analyze(encode(specimen("fixed-peer")))

    def test_qualification_promotion_and_invented_switch_counts_are_rejected(self):
        for key in ("timing_qualified", "priority_progress_qualified", "clock_qualified",
                    "physical_execution_verified", "extra_physical_timing_qualified"):
            with self.subTest(key=key):
                self.reject_mutation("ready-std-try", "result", key, True)
        self.reject_mutation("ready-std-try", "result", "actual_switch_count", 2)
        self.reject_mutation("ready-std-try", "result", "threads_joined", False)

    def test_record_order_duplicates_mixed_runs_and_failed_trailers_are_refused(self):
        valid = specimen()
        variants = [valid[1:], valid[:-1], valid + [valid[-1]],
                    [valid[0], valid[2], valid[1], *valid[3:]], valid + specimen()]
        for rows in variants:
            with self.assertRaises(ValueError):
                analysis.analyze(encode(rows))
        mixed = copy.deepcopy(valid)
        mixed[2]["run_id"] = "different-run"
        with self.assertRaisesRegex(ValueError, "mixed run"):
            analysis.analyze(encode(mixed))
        for suffix in ('EVENT_PATH_PERF {"schema":"event-path-perf-v1","record":"fatal"}\n',
                       "panicked at fixture\n", "truncated EVENT_PATH_PERF {}\n"):
            with self.assertRaises(ValueError):
                analysis.analyze(encode(valid) + suffix)

    def test_duplicate_json_fields_nan_and_boolean_integer_are_rejected(self):
        encoded = encode(specimen())
        duplicate = encoded.replace('"sample_count": 3', '"sample_count": 3, "sample_count": 3')
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            analysis.analyze(duplicate)
        with self.assertRaises(ValueError):
            analysis.analyze(encoded.replace('"clock_hz": 1000000000', '"clock_hz": NaN'))
        self.reject_mutation("ready-std-try", "sample", "index", False)


if __name__ == "__main__":
    unittest.main()
