"""Synthetic negative controls for the native std primitive evidence contract."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("std_transfer_analysis_tests", HERE / "mutex_analyze.py")
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)

# Deliberately independent of analyzer.EXPECTED: these are the agreed wire
# outcomes and ownership balances, not values copied from the implementation.
CASES = {
    "mutex-admit-held": ("busy", (0, 0, 0, 0, 1)),
    "mutex-admit-released": ("accepted", (0, 1, 1, 0, 0)),
    "mutex-take-held": ("busy", (1, 0, 0, 1, 1)),
    "mutex-take-released": ("taken", (1, 0, 1, 0, 0)),
    "condvar-wait-publish": ("taken", (0, 1, 1, 0, 0)),
    "condvar-publish-before-wait": ("taken", (0, 1, 1, 0, 0)),
    "condvar-close-wait": ("closed", (0, 0, 0, 0, 0)),
}


def fixture(mode="mutex-admit-held"):
    """Construct synthetic evidence only; this does not execute target code."""
    common = {"schema": "std-transfer-progress-v1", "run_id": "synthetic-std-transfer-test"}
    mutex = mode.startswith("mutex-")
    held = mutex and mode.endswith("-held")
    waiting = mode in {"condvar-wait-publish", "condvar-close-wait"}
    metadata = dict(
        common, record="metadata", mode=mode, build_mode="std-mutex-suite",
        build_id="b" * 64, hook_stage=0, runtime="nuttx", transport="std-mutex-condvar",
        capacity=1, payload_bytes=16, provider_count=1, arm_count=1, selection_bias="none",
        witness_api="try_lock" if mutex else "condvar", single_core_tcb_witness=True,
        prewarmed=True, prewarm_before_workers=True,
    )
    for role, priority in (("caller", 180), ("holder", 120), ("observer", 220), ("watchdog", 240)):
        metadata.update({role + "_policy": 1, role + "_priority": priority})
    outcome, counts = CASES[mode]
    result = dict(
        common, record="result", mode=mode, outcome=outcome, success=True, diagnostic_valid=True,
        completed_without_repair=True, primitive_contract_satisfied=True,
        priority_progress_qualified=False, production_transport_qualified=False,
        timing_qualified=False, recovery_applied=False, try_lock_calls=1 if mutex else 0,
        original_payload_retained=mode == "mutex-admit-held",
        protected_payload_retained=mode == "mutex-take-held", payload_valid=True,
        guard_held_at_attempt=held, guard_held_at_return=held,
        condvar_wait_calls=1 if waiting else 0, initial_condvar_wait_verified=waiting,
        initial_wait_state="wait_sem" if waiting else "unavailable",
        initial_wait_priority=180 if waiting else -1, caller_start_gate_verified=True,
        holder_start_gate_verified=True, worker_priorities_verified=True,
        caller_done=True, holder_done=True, joins_complete=True, watchdog_complete=True,
    )
    result.update(zip(("initial_value_count", "accepted", "transferred", "final_value_count", "busy_count"), counts))
    result.update(dict.fromkeys(analyzer.ORDER_FIELDS, 0))
    sequence = ["prewarm_order", "caller_start_order", "holder_start_order"]
    if held:
        sequence += ["holder_guard_order", "caller_attempt_order", "caller_return_order",
                     "holder_release_order"]
    elif mutex:
        sequence += ["holder_guard_order", "holder_release_order", "caller_attempt_order",
                     "caller_return_order"]
    elif waiting:
        sequence += ["caller_attempt_order", "wait_verified_order", "holder_guard_order",
                     "close_order" if mode == "condvar-close-wait" else "publish_order",
                     "holder_release_order", "caller_return_order"]
    else:
        sequence += ["holder_guard_order", "publish_order", "holder_release_order",
                     "caller_attempt_order", "caller_return_order"]
    result.update((field, index) for index, field in enumerate(sequence, 1))
    return metadata, result


class StdTransferEvidenceTests(unittest.TestCase):
    def reject_result(self, mode, **changes):
        metadata, result = fixture(mode)
        result.update(changes)
        with self.assertRaises(ValueError):
            analyzer.analyze(metadata, result)

    def test_all_seven_cases_keep_their_distinct_ownership_outcomes(self):
        self.assertEqual(set(analyzer.MODES), set(CASES))
        for mode in analyzer.MODES:
            metadata, result = fixture(mode)
            before = copy.deepcopy((metadata, result))
            with self.subTest(mode=mode):
                summary = analyzer.analyze(metadata, result)
                self.assertTrue(summary["fixture_valid"])
                self.assertTrue(summary["completed_without_repair"])
                self.assertTrue(summary["progress_contract_satisfied"])
                self.assertTrue(summary["primitive_contract_satisfied"])
                self.assertEqual(summary["outcome"], CASES[mode][0])
                self.assertEqual(summary["scope"], "std-transfer-primitives")
                self.assertEqual(summary["runtime_kind"], "unbound")
                self.assertFalse(summary["build_bound"])
                for field in ("priority_progress_qualified", "production_transport_qualified",
                              "timing_qualified", "physical_timing_qualified",
                              "physical_execution_verified", "hard_realtime_bound"):
                    self.assertFalse(summary[field])
                self.assertEqual((metadata, result), before)

    def test_busy_completes_the_attempt_without_claiming_admission_or_delivery(self):
        for mode in ("mutex-admit-held", "mutex-take-held"):
            summary = analyzer.analyze(*fixture(mode))
            self.assertEqual(summary["outcome"], "busy")
            self.assertEqual(summary["result"]["accepted"], 0)
            self.assertEqual(summary["result"]["transferred"], 0)
            self.assertTrue(summary["completed_without_repair"])

    def test_every_required_metadata_and_result_field_is_required(self):
        for mode in analyzer.MODES:
            metadata, result = fixture(mode)
            for row_index, row in enumerate((metadata, result)):
                for field in row:
                    broken = copy.deepcopy((metadata, result))
                    del broken[row_index][field]
                    with self.subTest(mode=mode, row=row_index, field=field), self.assertRaises(ValueError):
                        analyzer.analyze(*broken)

    def test_wrong_records_or_run_identity_are_refused(self):
        for index, changes in (
            (0, {"schema": "channel-progress-v1"}), (1, {"schema": "channel-progress-v1"}),
            (0, {"record": "result"}), (1, {"record": "metadata"}),
            (0, {"run_id": " "}), (1, {"run_id": "different-run"}),
            (1, {"mode": "mutex-take-held"}),
        ):
            rows = fixture()
            rows[index].update(changes)
            with self.subTest(index=index, changes=changes), self.assertRaises(ValueError):
                analyzer.analyze(*rows)
        for rows in (([], {}), ({}, []), (None, {})):
            with self.assertRaises(ValueError):
                analyzer.analyze(*rows)

    def test_native_metadata_cannot_be_relabelled_or_inferred(self):
        for field, value in (
            ("mode", "std-mutex-suite"), ("mode", []), ("build_mode", "clean"),
            ("hook_stage", 1), ("hook_stage", True), ("runtime", "linux-functional"),
            ("transport", "crossbeam-channel"), ("capacity", 8), ("payload_bytes", 4),
            ("provider_count", 4), ("arm_count", 3), ("selection_bias", "stop-important-ordinary"),
            ("witness_api", "try_recv"), ("build_id", "unrecorded-host"),
            ("single_core_tcb_witness", False), ("prewarmed", False),
            ("prewarm_before_workers", False),
        ):
            metadata, result = fixture()
            metadata[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                analyzer.analyze(metadata, result)

    def test_each_worker_and_supervisor_needs_actual_fifo_priority(self):
        for role in analyzer.SCHEDULE:
            for suffix, value in (("_policy", 0), ("_policy", 2), ("_policy", True),
                                  ("_priority", 100), ("_priority", True)):
                metadata, result = fixture()
                metadata[role + suffix] = value
                with self.subTest(role=role, suffix=suffix, value=value), self.assertRaises(ValueError):
                    analyzer.analyze(metadata, result)

    def test_success_label_cannot_replace_completion_watchdog_or_no_repair(self):
        for mode in analyzer.MODES:
            for field in ("success", "diagnostic_valid", "completed_without_repair",
                          "primitive_contract_satisfied", "payload_valid", "caller_start_gate_verified",
                          "holder_start_gate_verified", "worker_priorities_verified", "caller_done",
                          "holder_done", "joins_complete", "watchdog_complete"):
                with self.subTest(mode=mode, field=field):
                    self.reject_result(mode, **{field: False})
            self.reject_result(mode, recovery_applied=True)

    def test_booleans_cannot_masquerade_as_integer_observations(self):
        for field in (*analyzer.COUNTERS, *analyzer.ORDER_FIELDS, "try_lock_calls",
                      "condvar_wait_calls", "initial_wait_priority"):
            with self.subTest(field=field):
                self.reject_result("condvar-wait-publish", **{field: True})
        for field in ("success", "payload_valid", "guard_held_at_attempt", "recovery_applied"):
            self.reject_result("mutex-admit-held", **{field: 1})

    def test_ownership_counts_are_case_specific_and_never_inferred_from_busy(self):
        for mode in analyzer.MODES:
            _, valid = fixture(mode)
            for field in analyzer.COUNTERS:
                for value in (1 - valid[field], -1, 2):
                    with self.subTest(mode=mode, field=field, value=value):
                        self.reject_result(mode, **{field: value})
        self.reject_result("mutex-admit-held", accepted=1, transferred=1, busy_count=0, outcome="accepted")
        self.reject_result("mutex-take-held", transferred=1, final_value_count=0, busy_count=0, outcome="taken")
        self.reject_result("condvar-close-wait", accepted=1, transferred=1, outcome="taken")

    def test_held_cases_preserve_the_correct_owned_value(self):
        self.reject_result("mutex-admit-held", original_payload_retained=False)
        self.reject_result("mutex-take-held", protected_payload_retained=False)
        for mode in analyzer.MODES:
            _, valid = fixture(mode)
            for field in ("original_payload_retained", "protected_payload_retained"):
                with self.subTest(mode=mode, field=field):
                    self.reject_result(mode, **{field: not valid[field]})

    def test_try_lock_must_be_one_attempt_and_guard_must_still_be_held_on_return(self):
        for mode in analyzer.MODES:
            mutex = mode.startswith("mutex-")
            for calls in ((0, 2) if mutex else (1, 2)):
                self.reject_result(mode, try_lock_calls=calls)
            _, valid = fixture(mode)
            for field in ("guard_held_at_attempt", "guard_held_at_return"):
                self.reject_result(mode, **{field: not valid[field]})

    def test_wait_cases_need_native_blocking_evidence_at_the_original_priority(self):
        for mode in ("condvar-wait-publish", "condvar-close-wait"):
            for field, value in (("condvar_wait_calls", 0), ("initial_condvar_wait_verified", False),
                                 ("initial_wait_state", "ready"), ("initial_wait_state", "unavailable"),
                                 ("initial_wait_priority", -1), ("initial_wait_priority", 200),
                                 ("wait_verified_order", 0)):
                with self.subTest(mode=mode, field=field):
                    self.reject_result(mode, **{field: value})

    def test_spurious_condition_wakes_still_require_a_verified_final_predicate(self):
        for mode in ("condvar-wait-publish", "condvar-close-wait"):
            metadata, result = fixture(mode)
            result["condvar_wait_calls"] = 3
            self.assertTrue(analyzer.analyze(metadata, result)["fixture_valid"])
            result["caller_done"] = False
            with self.assertRaises(ValueError):
                analyzer.analyze(metadata, result)

    def test_before_wait_publication_and_mutex_cases_cannot_claim_a_condition_wait(self):
        for mode in analyzer.MODES:
            if mode in ("condvar-wait-publish", "condvar-close-wait"):
                continue
            for field, value in (("condvar_wait_calls", 1), ("initial_condvar_wait_verified", True),
                                 ("initial_wait_state", "wait_sem"), ("initial_wait_priority", 180),
                                 ("wait_verified_order", 100)):
                self.reject_result(mode, **{field: value})

    def test_prewarming_is_before_both_workers_and_operations_follow_setup(self):
        for mode in analyzer.MODES:
            for field, value in (("prewarm_order", 0), ("prewarm_order", 100),
                                 ("caller_start_order", 100), ("holder_start_order", 100),
                                 ("caller_attempt_order", 0), ("holder_guard_order", 0)):
                with self.subTest(mode=mode, field=field):
                    self.reject_result(mode, **{field: value})

    def test_reordered_critical_events_are_rejected_even_with_all_success_flags(self):
        pairs = {
            "mutex-admit-held": (("holder_guard_order", "caller_attempt_order"),
                                 ("caller_return_order", "holder_release_order")),
            "mutex-take-held": (("caller_attempt_order", "caller_return_order"),
                                ("caller_return_order", "holder_release_order")),
            "mutex-admit-released": (("holder_release_order", "caller_attempt_order"),),
            "mutex-take-released": (("holder_release_order", "caller_attempt_order"),),
            "condvar-wait-publish": (("wait_verified_order", "holder_guard_order"),
                                     ("publish_order", "holder_release_order"),
                                     ("holder_release_order", "caller_return_order")),
            "condvar-close-wait": (("wait_verified_order", "close_order"),
                                   ("close_order", "holder_release_order")),
            "condvar-publish-before-wait": (("publish_order", "caller_attempt_order"),
                                            ("holder_release_order", "caller_attempt_order")),
        }
        for mode, field_pairs in pairs.items():
            for first, second in field_pairs:
                metadata, result = fixture(mode)
                result[first], result[second] = result[second], result[first]
                with self.subTest(mode=mode, first=first, second=second), self.assertRaises(ValueError):
                    analyzer.analyze(metadata, result)

    def test_event_ids_are_distinct_bounded_integers_with_no_fabricated_transitions(self):
        for mode in analyzer.MODES:
            self.reject_result(mode, caller_return_order=4)
            self.reject_result(mode, caller_return_order=2**32)
            self.reject_result(mode, holder_release_order=-1)
            if mode.startswith("mutex-"):
                for field in ("wait_verified_order", "publish_order", "close_order"):
                    self.reject_result(mode, **{field: 100})
        self.reject_result("condvar-wait-publish", close_order=100)
        self.reject_result("condvar-close-wait", publish_order=100)
        self.reject_result("condvar-publish-before-wait", close_order=100)

    def test_outcome_is_derived_from_case_and_observations(self):
        for mode in analyzer.MODES:
            for outcome in {"busy", "accepted", "taken", "closed", "completed-without-repair"}:
                if outcome != CASES[mode][0]:
                    with self.subTest(mode=mode, outcome=outcome):
                        self.reject_result(mode, outcome=outcome)

    def test_target_cannot_self_certify_broader_qualification(self):
        for row_index in (0, 1):
            for field in analyzer.CLAIMS:
                rows = fixture("condvar-wait-publish")
                rows[row_index][field] = True
                with self.subTest(row=row_index, field=field), self.assertRaises(ValueError):
                    analyzer.analyze(*rows)


class StdPrimitiveSourceTests(unittest.TestCase):
    def test_api_dispatch_pthread_and_initialization_paths_are_all_required(self):
        required = {
            "std/src/sync/poison/mutex.rs", "std/src/sync/poison/condvar.rs",
            "std/src/sys/sync/mutex/mod.rs", "std/src/sys/sync/mutex/pthread.rs",
            "std/src/sys/pal/unix/sync/mutex.rs", "std/src/sys/sync/condvar/mod.rs",
            "std/src/sys/sync/condvar/pthread.rs", "std/src/sys/pal/unix/sync/condvar.rs",
            "std/src/sys/sync/once_box.rs",
        }
        for expected, sdk in ((analyzer.STD_SOURCE_SHA256, "nightly-2025-09-15"),
                              (analyzer.ESP_STD_SOURCE_SHA256, "esp-1.90.0.0")):
            self.assertEqual(set(expected), required)
            self.assertTrue(all(analyzer.HEX256.fullmatch(value) for value in expected.values()))
            complete_library = dict(expected, unrelated_file="0" * 64)
            analyzer.validate_std_sources(complete_library, sdk)

    def test_each_missing_or_changed_source_is_rejected_for_both_sdks(self):
        for expected, sdk in ((analyzer.STD_SOURCE_SHA256, "nightly-2025-09-15"),
                              (analyzer.ESP_STD_SOURCE_SHA256, "esp-1.90.0.0")):
            for path in expected:
                for change in ("missing", "different", "wrong-type"):
                    library = dict(expected)
                    if change == "missing":
                        del library[path]
                    else:
                        library[path] = "0" * 64 if change == "different" else True
                    with self.subTest(sdk=sdk, path=path, change=change), self.assertRaises(ValueError):
                        analyzer.validate_std_sources(library, sdk)

    def test_other_sdk_or_mixed_dispatch_is_not_an_audited_source_cohort(self):
        for expected, other, sdk in (
            (analyzer.STD_SOURCE_SHA256, analyzer.ESP_STD_SOURCE_SHA256, "nightly-2025-09-15"),
            (analyzer.ESP_STD_SOURCE_SHA256, analyzer.STD_SOURCE_SHA256, "esp-1.90.0.0"),
        ):
            with self.assertRaises(ValueError):
                analyzer.validate_std_sources(other, sdk)
            for path in expected:
                if expected[path] == other[path]:
                    continue
                mixed = dict(expected)
                mixed[path] = other[path]
                with self.subTest(sdk=sdk, path=path), self.assertRaises(ValueError):
                    analyzer.validate_std_sources(mixed, sdk)

    def test_unknown_sdk_and_invalid_ledger_are_refused(self):
        for sdk in ("1.90.0", "nightly", "", None, [], True):
            with self.subTest(sdk=sdk), self.assertRaises(ValueError):
                analyzer.validate_std_sources(analyzer.STD_SOURCE_SHA256, sdk)
        for library in (None, [], "not-a-ledger"):
            with self.assertRaises(ValueError):
                analyzer.validate_std_sources(library, "nightly-2025-09-15")


if __name__ == "__main__":
    unittest.main()
