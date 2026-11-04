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


def specimen(case="ready-std-try", *, native=False, payload="bytes16", count=None, warmup=1):
    family, transport, admission, receive_api, arms, providers = analysis.CASES[case]
    normal, saturation = case.endswith("-normal"), case.endswith("-saturation")
    timer_case = case == "timer-provider-messaging"
    scheduled = normal or saturation
    count = (9 if saturation else 3) if count is None else count
    epoch = 8_000_000 if scheduled else None
    period = 1_000_000 if normal else 0
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
        "release_schedule": ("native-timeout-after-owner-wait" if timer_case
                             else "finite-absolute-1ms" if normal else "finite-held-owner-burst"
                             if saturation else "finite-closed-loop-or-unscheduled-fanin"),
        "release_period_ns": period,
        "timer_delay_ns": 50_000_000 if timer_case else 0,
        "warmup_scope": "per-provider-prefix" if family == "finite-fanin" else "sequential-prefix",
        "counts_include_warmup": True,
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
    owner_release = 100_000 + (count + warmup) * 1000 + providers * 10_000 if saturation else None
    total_checksum = retained_checksum = admitted = rejected = park_returns = 0
    for provider in range(providers):
        for sequence in range(1, count + warmup + 1):
            if saturation:
                accepted = provider * (count + warmup) + sequence <= capacity
            else:
                accepted = (family != "full-rejection"
                            and not (family == "finite-fanin" and not normal and sequence % 2 == 0))
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
            elif saturation:
                receive = owner_release + sequence * 100 if accepted else 0
            else:
                receive = start + 180 if accepted else 0
            planned = epoch + (sequence - 1) * period if scheduled else None
            timer = None
            if timer_case:
                plan = 1_000_000_000 + sequence * 100_000_000
                timer = {
                    "plan_ns": plan, "deadline_ns": plan + 50_000_000,
                    "provider_resume_ns": plan + 50_000_100,
                    "provider_resume_ticks": start - 20,
                    "provider_wait_observed_ns": plan + 1_000_000 if native else 0,
                    "owner_wait_observed_ns": plan - 100 if native else 0,
                    "provider_wait_verified": native, "expired": True,
                    "handler_start_ticks": receive + 10, "handler_end_ticks": receive + 30,
                }
            samples.append({
                **common, "record": "sample", "index": len(samples), "sequence": sequence,
                "provider": provider, "start_ticks": start, "admission_return_ticks": admission_return,
                "receive_start_ticks": start + 120 if family == "ready" else None,
                "receive_return_ticks": receive,
                "elapsed_upper_ns": ((count + warmup) * 1_000_000 if normal
                                     else 1_000_000 if saturation else 2000),
                "planned_release_ns": planned,
                "release_observed_ns": planned + sequence * 3 + provider * 10 if scheduled else None,
                "blocked_verified": native and family in analysis.WAIT_FAMILIES,
                "peer_tid": receiver_tid if control else 0, "park_returns": returns,
                "accepted": accepted, "payload_valid": True,
                "timer": timer,
            })
    result = {
        **common, "record": "result", "success": True, "sample_count": len(samples),
        "attempted": (count + warmup) * providers, "admitted": admitted, "rejected": rejected,
        "handled": admitted, "checksum": total_checksum, "retained_checksum": retained_checksum,
        "park_returns": park_returns, "threads_joined": True,
        "terminal_quiescence_verified": native and threaded, "structural_valid": True,
        "timing_qualified": False, "priority_progress_qualified": False,
        "physical_execution_verified": False, "clock_qualified": False, "actual_switch_count": None,
        "release_epoch_ns": epoch, "owner_release_ticks": owner_release,
        "owner_hold_verified": native and saturation,
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

    def test_scheduled_normal_and_saturation_preserve_all_attempts_and_lateness(self):
        for providers in (1, 4):
            for mode in ("normal", "saturation"):
                for native in (False, True):
                    with self.subTest(providers=providers, mode=mode, native=native):
                        case = f"fanin-messaging-{providers}-{mode}"
                        result = analysis.analyze(encode(specimen(
                            case, native=native, payload="imu-sample", count=18, warmup=2)))
                        lateness = result["release_lateness"]
                        self.assertFalse(lateness["timing_qualified"])
                        self.assertEqual(lateness["observed_ns"]["count"], 18 * providers)
                        self.assertEqual(lateness["observed_ns"]["min_ns"], 9)
                        self.assertEqual(lateness["observed_ns"]["max_ns"], 60 + 10 * (providers - 1))
                        self.assertFalse(result["timing_qualified"])
                        self.assertFalse(result["priority_progress_qualified"])
                        self.assertEqual(result["result"]["admitted"],
                                         16 if mode == "saturation" else 20 * providers)

    def test_original_captures_remain_unscheduled_when_new_fields_are_absent(self):
        for case in analysis.CASES:
            if case.endswith(("-normal", "-saturation")) or case == "timer-provider-messaging":
                continue
            with self.subTest(case=case):
                rows = specimen(case, native=case == "fixed-peer")
                for row in rows:
                    for key in ("release_period_ns", "warmup_scope", "release_epoch_ns",
                                "planned_release_ns", "release_observed_ns",
                                "owner_release_ticks", "owner_hold_verified", "timer_delay_ns", "timer"):
                        row.pop(key, None)
                result = analysis.analyze(encode(rows))
                self.assertIsNone(result["release_lateness"])

    def test_saturation_owner_release_order_survives_a_valid_counter_wrap(self):
        rows = specimen("fanin-messaging-1-saturation", native=True)
        offset = analysis.U32 - 115_000
        for row in rows:
            if row["record"] == "sample":
                for key in ("start_ticks", "admission_return_ticks", "receive_return_ticks"):
                    if key != "receive_return_ticks" or row["accepted"]:
                        row[key] = (row[key] + offset) & analysis.U32
        result = first(rows, "result")
        result["owner_release_ticks"] = (result["owner_release_ticks"] + offset) & analysis.U32
        self.assertTrue(analysis.analyze(encode(rows))["structural_valid"])

    def test_timer_pipeline_separates_deadline_lateness_from_raw_stage_ticks(self):
        for native in (False, True):
            for payload in ("bytes16", "imu-sample"):
                with self.subTest(native=native, payload=payload):
                    result = analysis.analyze(encode(specimen(
                        "timer-provider-messaging", native=native, payload=payload)))
                    self.assertFalse(result["timer_deadline_lateness"]["timing_qualified"])
                    self.assertEqual(result["timer_deadline_lateness"]["observed_ns"]["p50_ns"], 100)
                    for name, expected in (("provider_to_admission_start", 20),
                                           ("provider_to_handler_end", 230),
                                           ("receive_to_handler_start", 10), ("handler_duration", 20)):
                        self.assertEqual(result["raw_ticks"][name]["p50_ticks"], expected)
                    self.assertFalse(result["timing_qualified"])
                    self.assertFalse(result["priority_progress_qualified"])
                    self.assertTrue(result["total_checksum_reconstructed"])

    def test_timer_accepts_owner_preemption_and_consistent_raw_counter_wrap(self):
        rows = specimen("timer-provider-messaging", native=True, count=1, warmup=0)
        sample = first(rows, "sample")
        timer = sample["timer"]
        sample["admission_return_ticks"] = timer["handler_end_ticks"] + 50
        offset = analysis.U32 - sample["start_ticks"] - 5
        for key in ("start_ticks", "admission_return_ticks", "receive_return_ticks"):
            sample[key] = (sample[key] + offset) & analysis.U32
        for key in ("provider_resume_ticks", "handler_start_ticks", "handler_end_ticks"):
            timer[key] = (timer[key] + offset) & analysis.U32
        result = analysis.analyze(encode(rows))
        self.assertEqual(result["raw_ticks"]["provider_to_handler_end"]["p50_ticks"], 230)
        self.assertEqual(result["raw_ticks"]["admission_call"]["p50_ticks"], 260)

    def test_pre_timer_capture_fields_remain_optional_for_all_previous_cases(self):
        for case in analysis.CASES:
            if case == "timer-provider-messaging":
                continue
            with self.subTest(case=case):
                rows = specimen(case, native=case == "fixed-peer")
                for row in rows:
                    row.pop("timer", None)
                    row.pop("timer_delay_ns", None)
                result = analysis.analyze(encode(rows))
                self.assertIsNone(result["timer_deadline_lateness"])


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

    def test_new_fanin_modes_require_their_exact_release_and_warmup_contract(self):
        for mode in ("normal", "saturation"):
            case = f"fanin-messaging-4-{mode}"
            for key, value in (("release_schedule", "finite-closed-loop-or-unscheduled-fanin"),
                               ("release_period_ns", 1), ("release_period_ns", True),
                               ("warmup_scope", "sequential-prefix")):
                with self.subTest(case=case, key=key):
                    self.reject_mutation(case, "experiment", key, value)
            for kind, key in (("experiment", "release_period_ns"), ("experiment", "warmup_scope"),
                              ("result", "release_epoch_ns"), ("result", "owner_release_ticks"),
                              ("result", "owner_hold_verified"), ("sample", "planned_release_ns"),
                              ("sample", "release_observed_ns")):
                with self.subTest(case=case, missing=key):
                    rows = specimen(case)
                    del first(rows, kind)[key]
                    with self.assertRaises(ValueError):
                        analysis.analyze(encode(rows))

    def test_release_markers_refuse_early_drift_reordering_and_fabricated_windows(self):
        for mode in ("normal", "saturation"):
            case = f"fanin-messaging-4-{mode}"
            rows = specimen(case)
            sample = first(rows, "sample")
            for key, value in (("planned_release_ns", sample["planned_release_ns"] + 1),
                               ("release_observed_ns", sample["planned_release_ns"] - 1),
                               ("release_observed_ns", analysis.U64),
                               ("release_observed_ns", True)):
                with self.subTest(case=case, key=key, value=value):
                    self.reject_mutation(case, "sample", key, value)
            samples = [row for row in rows if row["record"] == "sample"]
            samples[0]["release_observed_ns"] = samples[1]["release_observed_ns"] + 1
            with self.assertRaisesRegex(ValueError, "decreases within a provider"):
                analysis.analyze(encode(rows))
        rows = specimen("fanin-messaging-1-normal")
        first(rows, "result")["release_epoch_ns"] = analysis.U64
        with self.assertRaisesRegex(ValueError, "release schedule overflows u64"):
            analysis.analyze(encode(rows))
        self.reject_mutation("fanin-messaging-1-normal", "result", "release_epoch_ns", 1 << 64)
        self.reject_mutation("fanin-messaging-1-normal", "experiment", "release_period_ns", 1 << 64)

    def test_saturation_requires_capacity_admissions_and_a_full_rejection_suffix(self):
        case = "fanin-messaging-1-saturation"
        rows = specimen(case, count=8, warmup=0)
        with self.assertRaisesRegex(ValueError, "saturation must fill"):
            analysis.analyze(encode(rows))
        rows = specimen(case)
        result = first(rows, "result")
        result["admitted"] -= 1
        result["handled"] -= 1
        result["rejected"] += 1
        with self.assertRaisesRegex(ValueError, "saturation must fill"):
            analysis.analyze(encode(rows))
        rows = specimen(case)
        samples = [row for row in rows if row["record"] == "sample"]
        accepted = next(row for row in samples if row["accepted"])
        rejected = next(row for row in samples if not row["accepted"])
        accepted["accepted"], rejected["accepted"] = False, True
        accepted["receive_return_ticks"], rejected["receive_return_ticks"] = (
            0, first(rows, "result")["owner_release_ticks"] + 100)
        with self.assertRaisesRegex(ValueError, "after a full rejection"):
            analysis.analyze(encode(rows))

    def test_saturation_owner_hold_proof_and_raw_release_order_are_required(self):
        case = "fanin-messaging-1-saturation"
        self.reject_mutation(case, "result", "owner_hold_verified", False, native=True)
        self.reject_mutation(case, "result", "owner_hold_verified", True)
        self.reject_mutation("fanin-messaging-1-normal", "result", "owner_hold_verified", True, native=True)
        self.reject_mutation(case, "result", "owner_release_ticks", None)
        self.reject_mutation(case, "result", "owner_release_ticks", True)
        rows = specimen(case)
        sample, result = first(rows, "sample"), first(rows, "result")
        for marker in (sample["admission_return_ticks"] - 1, sample["receive_return_ticks"] + 1,
                       (sample["start_ticks"] + (1 << 31)) & analysis.U32):
            with self.subTest(marker=marker):
                self.reject_mutation(case, "result", "owner_release_ticks", marker)
        result["owner_release_ticks"] = sample["admission_return_ticks"]
        with self.assertRaisesRegex(ValueError, "owner.*release"):
            analysis.analyze(encode(rows))

    def test_original_cases_cannot_gain_scheduled_claims_from_new_fields(self):
        for kind, key, value in (("experiment", "release_period_ns", 1_000_000),
                                 ("experiment", "warmup_scope", "sequential-prefix"),
                                 ("result", "release_epoch_ns", 1),
                                 ("result", "owner_release_ticks", 1),
                                 ("result", "owner_hold_verified", True),
                                 ("sample", "planned_release_ns", 1),
                                 ("sample", "release_observed_ns", 1)):
            with self.subTest(key=key):
                self.reject_mutation("fanin-messaging-1", kind, key, value)

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


class TimerCorruptionControls(unittest.TestCase):
    def reject_timer_mutation(self, key, value, *, native=False):
        rows = specimen("timer-provider-messaging", native=native)
        first(rows, "sample")["timer"][key] = value
        with self.assertRaises(ValueError):
            analysis.analyze(encode(rows))

    def test_timer_metadata_limits_and_required_fields_are_exact(self):
        for key, value in (("samples", 129), ("warmup", 33), ("timer_delay_ns", 49_000_000),
                           ("timer_delay_ns", True), ("release_period_ns", 1),
                           ("release_schedule", "finite-absolute-1ms"),
                           ("warmup_scope", "per-provider-prefix")):
            with self.subTest(key=key):
                rows = specimen("timer-provider-messaging")
                rows[0][key] = value
                with self.assertRaises(ValueError):
                    analysis.analyze(encode(rows))
        for kind, key in (("experiment", "timer_delay_ns"), ("experiment", "release_period_ns"),
                          ("experiment", "warmup_scope"), ("sample", "timer"),
                          ("sample", "planned_release_ns"), ("sample", "release_observed_ns"),
                          ("result", "release_epoch_ns"), ("result", "owner_release_ticks"),
                          ("result", "owner_hold_verified")):
            with self.subTest(missing=key):
                rows = specimen("timer-provider-messaging")
                del first(rows, kind)[key]
                with self.assertRaises(ValueError):
                    analysis.analyze(encode(rows))

    def test_timer_deadline_expiry_and_provider_window_fail_closed(self):
        timer = first(specimen("timer-provider-messaging"), "sample")["timer"]
        for key, value in (("expired", False), ("deadline_ns", timer["deadline_ns"] - 1),
                           ("plan_ns", analysis.U64), ("plan_ns", 1 << 64),
                           ("provider_resume_ns", timer["deadline_ns"] - 1),
                           ("provider_resume_ns", timer["deadline_ns"] + 1999),
                           ("provider_resume_ns", True)):
            with self.subTest(key=key, value=value):
                self.reject_timer_mutation(key, value)

    def test_timer_wait_observations_and_host_refusal_are_required(self):
        timer = first(specimen("timer-provider-messaging", native=True), "sample")["timer"]
        for key, value in (("provider_wait_verified", False),
                           ("owner_wait_observed_ns", timer["plan_ns"] + 1),
                           ("provider_wait_observed_ns", timer["plan_ns"] - 1),
                           ("provider_wait_observed_ns", timer["deadline_ns"])):
            with self.subTest(native=True, key=key):
                self.reject_timer_mutation(key, value, native=True)
        for key, value in (("provider_wait_verified", True),
                           ("provider_wait_observed_ns", 1), ("owner_wait_observed_ns", 1)):
            with self.subTest(native=False, key=key):
                self.reject_timer_mutation(key, value)
        rows = specimen("timer-provider-messaging", native=True)
        first(rows, "sample")["blocked_verified"] = False
        with self.assertRaisesRegex(ValueError, "blocked-wait proof"):
            analysis.analyze(encode(rows))

    def test_timer_raw_stage_order_and_wrap_bounds_are_checked(self):
        sample = first(specimen("timer-provider-messaging"), "sample")
        for key, value in (("provider_resume_ticks", sample["start_ticks"] + 1),
                           ("handler_start_ticks", sample["receive_return_ticks"] - 1),
                           ("handler_end_ticks", sample["timer"]["handler_start_ticks"] - 1),
                           ("handler_end_ticks", sample["start_ticks"] + analysis.HALF),
                           ("handler_end_ticks", 1 << 32), ("handler_start_ticks", True)):
            with self.subTest(key=key, value=value):
                self.reject_timer_mutation(key, value)
        rows = specimen("timer-provider-messaging")
        first(rows, "sample")["admission_return_ticks"] = sample["start_ticks"] - 1
        with self.assertRaises(ValueError):
            analysis.analyze(encode(rows))

    def test_timer_object_omissions_extra_claims_and_old_case_relabelling_fail(self):
        valid = specimen("timer-provider-messaging")
        for key in first(valid, "sample")["timer"]:
            with self.subTest(missing=key):
                rows = copy.deepcopy(valid)
                del first(rows, "sample")["timer"][key]
                with self.assertRaisesRegex(ValueError, "malformed timer evidence"):
                    analysis.analyze(encode(rows))
        self.reject_timer_mutation("irq_timestamp_ticks", 1)
        self.reject_timer_mutation("timing_qualified", True)
        rows = specimen("blocked-messaging-try")
        first(rows, "sample")["timer"] = first(valid, "sample")["timer"]
        with self.assertRaisesRegex(ValueError, "non-timer case"):
            analysis.analyze(encode(rows))
        rows = specimen("blocked-messaging-try")
        rows[0]["timer_delay_ns"] = 50_000_000
        with self.assertRaisesRegex(ValueError, "unexpected timer delay"):
            analysis.analyze(encode(rows))


if __name__ == "__main__":
    unittest.main()
