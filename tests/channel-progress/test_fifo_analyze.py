"""Negative controls for source ordering, native readiness and narrow scope."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("fifo_analysis_tests", HERE / "fifo_analyze.py")
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)

# Wire expectations are deliberately independent of the implementation's mode
# table. Synthetic records do not claim that target code has executed.
CASES = (
    "fifo-try-recv-reserved", "fifo-try-recv-published",
    "fifo-select-reserved", "fifo-select-published",
    "fifo-try-send-reserved", "fifo-try-send-reclaimed",
)


def fixture(mode="fifo-try-recv-reserved"):
    selected, reverse = mode.startswith("fifo-select-"), mode.startswith("fifo-try-send-")
    reserved = mode.endswith("-reserved")
    common = dict(schema="fifo-channel-progress-v1", run_id="synthetic-equal-fifo-run", mode=mode)
    trigger = (0x303 if reserved else 0x304) if reverse else (0x301 if reserved else 0x302)
    metadata = dict(
        common, record="metadata", build_id="b" * 64, build_mode="fifo-channel-suite",
        contract="equal-fifo-run-to-block-v1", hook_stage=5, runtime="nuttx",
        transport="crossbeam-channel", transport_version="0.5.17", capacity=8, payload_bytes=16,
        provider_count=1, arm_count=3 if selected else 1,
        selection_bias="stop-important-ordinary" if selected else "none",
        holder_api="try_recv" if reverse else "try_send",
        peer_api="select_biased" if selected else "try_send" if reverse else "try_recv",
        trigger_stage=trigger, commit_stage=0x304 if reverse else 0x302,
        single_core_tcb_witness=True, participant_operation_budget=1,
        drain_budget=8 if reverse else 0, select_timeout_ms=200 if selected else 0,
        interference_iteration_limit=2_000_000, interference_time_limit_ms=1000,
        async_channel_access=False, manual_selected_operations=False, selected_send=False,
        endpoints_changed_during_window=False,
    )
    for role, priority in (("holder", 180), ("peer", 180), ("observer", 220), ("watchdog", 240)):
        metadata.update({role + "_policy": 1, role + "_priority": priority})
    result = dict(
        common, record="result", outcome="completed-without-repair", success=True,
        diagnostic_valid=True, completed_without_repair=True, progress_contract_satisfied=True,
        recovery_applied=False, priority_progress_qualified=False, production_transport_qualified=False,
        failure_reason="none", cleanup_error="none",
        timing_qualified=False, hook_error=False, trigger_count=1, holder_resume_count=1,
        holder_reserved_count=1, holder_committed_count=1, peer_reserved_count=1, peer_committed_count=1,
        holder_operation_calls=1, peer_operation_calls=1, peer_retry_count=0,
        messages_sent=9 if reverse else 1, messages_received=9 if reverse else 1, payload_valid=True,
        initial_gate_waits_verified=True, holder_state_at_trigger="ready", holder_priority_at_trigger=180,
        peer_state_at_trigger="wait_sem", peer_priority_at_trigger=180,
        peer_ready_state="ready", peer_ready_priority=180,
        holder_state_at_ready="ready", holder_priority_at_ready=180,
        holder_hook_returned_at_ready=False, holder_commit_count_at_ready=0 if reserved else 1,
        observer_state_at_holder_resume="wait_sem", observer_priority_at_holder_resume=220,
        initial_select_wait_verified=selected, registered_arms=3 if selected else 0,
        select_registration_count=1 if selected else 0, select_wait_resume_count=1 if selected else 0,
        select_outcome="important" if selected else "none", select_timeout_count=0,
        interference_iterations=500 if selected else 1,
        interference_elapsed_ns=200_000_000 if selected else 0,
        holder_done=True, peer_done=True, final_native_waits_verified=True,
        joins_complete=True, watchdog_complete=True,
        select_registered_order=0, select_wait_verified_order=0, peer_wait_resume_order=0,
    )
    sequence = []
    if selected:
        sequence += ["peer_enter_order", "select_registered_order", "select_wait_verified_order"]
    sequence += ["holder_enter_order", "holder_reserve_order"]
    if not reserved:
        sequence.append("holder_commit_order")
    sequence += ["trigger_order", "observer_trigger_order", "peer_ready_order",
                 "observer_block_order", "holder_resume_order"]
    if reserved:
        sequence.append("holder_commit_order")
    sequence += ["holder_return_order", "peer_wait_resume_order" if selected else "peer_enter_order",
                 "peer_reserve_order", "peer_commit_order", "peer_return_order"]
    result.update({field: index for index, field in enumerate(sequence, 1)})
    return metadata, result


class FifoAnalysisTests(unittest.TestCase):
    def test_exact_six_cases_have_narrow_unbound_scope(self):
        for mode in CASES:
            with self.subTest(mode=mode):
                summary = analyzer.analyze(*fixture(mode))
                self.assertEqual(summary["mode"], mode)
                self.assertEqual(summary["scope"], "equal-fifo-reservation-progress")
                self.assertEqual(summary["contract"], "equal-fifo-run-to-block-v1")
                self.assertTrue(summary["fixture_valid"])
                self.assertTrue(summary["completed_without_repair"])
                self.assertTrue(summary["progress_contract_satisfied"])
                self.assertEqual(summary["runtime_kind"], "unbound")
                self.assertNotIn("candidate_contract_satisfied", summary)
                for field in ("build_bound", "physical_execution_verified", "priority_progress_qualified",
                              "production_transport_qualified", "timing_qualified",
                              "physical_timing_qualified", "hard_realtime_bound"):
                    self.assertFalse(summary[field])

    def test_post_publication_operation_return_need_not_precede_the_peer(self):
        # A post-release waker mutex can block without exposing a live slot
        # reservation. Do not invent a stronger source invariant than the audit.
        for mode in CASES:
            metadata, result = fixture(mode)
            result["holder_return_order"] = max(result[key] for key in analyzer.ORDER_FIELDS) + 1
            with self.subTest(mode=mode):
                analyzer.analyze(metadata, result)

    def test_recorded_failure_or_cleanup_error_cannot_be_overridden_by_success_flags(self):
        for field in ("failure_reason", "cleanup_error"):
            for value in ("fifo-invalid-evidence", "join-deadline", "gate-release-failed", "", None, False):
                metadata, result = fixture()
                result[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    analyzer.analyze(metadata, result)

    def test_missing_wire_fields_cannot_be_inferred_from_success(self):
        for row_index in (0, 1):
            original = fixture("fifo-select-reserved")
            for field in original[row_index]:
                rows = copy.deepcopy(original)
                del rows[row_index][field]
                with self.subTest(record=row_index, field=field), self.assertRaises(ValueError):
                    analyzer.analyze(*rows)

    def test_wrong_schema_run_case_and_build_contract_are_refused(self):
        for index, field, value in (
            (0, "schema", "channel-progress-v1"), (1, "schema", "std-transfer-progress-v1"),
            (0, "record", "result"), (1, "record", "metadata"), (1, "run_id", "different-run"),
            (0, "run_id", ""), (0, "mode", "fifo-channel-suite"),
            (1, "mode", "fifo-try-recv-published"), (0, "build_id", "unrecorded-host"),
            (0, "build_mode", "std-mutex-suite"), (0, "contract", "any-equal-priority"),
            (0, "hook_stage", 1), (0, "runtime", "linux-functional"),
        ):
            rows = fixture()
            rows[index][field] = value
            with self.subTest(record=index, field=field), self.assertRaises(ValueError):
                analyzer.analyze(*rows)

    def test_metadata_admission_is_exact_and_includes_all_roles(self):
        changes = (
            ("transport", "std"), ("transport_version", "0.5.18"), ("capacity", 0),
            ("payload_bytes", 32), ("provider_count", 4), ("arm_count", 1),
            ("selection_bias", "random"), ("holder_api", "send"), ("peer_api", "recv"),
            ("trigger_stage", 0x302), ("commit_stage", 0x304),
            ("participant_operation_budget", 8), ("drain_budget", 1), ("select_timeout_ms", 0),
            ("interference_iteration_limit", 4_000_000), ("interference_time_limit_ms", 2000),
            ("single_core_tcb_witness", False), ("async_channel_access", True),
            ("manual_selected_operations", True), ("selected_send", True),
            ("endpoints_changed_during_window", True),
        )
        for field, value in changes:
            metadata, result = fixture("fifo-select-reserved")
            metadata[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyzer.analyze(metadata, result)
        for role in ("holder", "peer", "observer", "watchdog"):
            for suffix, value in (("_policy", 2), ("_priority", 120)):
                metadata, result = fixture()
                metadata[role + suffix] = value
                with self.subTest(role=role, suffix=suffix), self.assertRaises(ValueError):
                    analyzer.analyze(metadata, result)

    def test_native_states_cannot_be_replaced_by_announcements_or_wrong_priorities(self):
        for state, priority in (
            ("holder_state_at_trigger", "holder_priority_at_trigger"),
            ("peer_state_at_trigger", "peer_priority_at_trigger"),
            ("peer_ready_state", "peer_ready_priority"),
            ("holder_state_at_ready", "holder_priority_at_ready"),
            ("observer_state_at_holder_resume", "observer_priority_at_holder_resume"),
        ):
            for field, value in ((state, "unavailable"), (priority, 120)):
                metadata, result = fixture("fifo-select-reserved")
                result[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    analyzer.analyze(metadata, result)

    def test_ready_snapshot_must_be_on_the_named_side_of_the_exact_release(self):
        for mode in CASES:
            metadata, result = fixture(mode)
            result["holder_commit_count_at_ready"] ^= 1
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                analyzer.analyze(metadata, result)
        metadata, result = fixture()
        result["holder_hook_returned_at_ready"] = True
        with self.assertRaises(ValueError):
            analyzer.analyze(metadata, result)

    def test_selected_wait_requires_setup_trigger_ready_and_exact_one_resume(self):
        for field, value in (
            ("initial_select_wait_verified", False), ("registered_arms", 2),
            ("select_registration_count", 0), ("select_registration_count", 2),
            ("select_wait_resume_count", 0), ("select_wait_resume_count", 2),
            ("peer_state_at_trigger", "ready"), ("select_outcome", "deadline"),
            ("select_outcome", "ordinary"), ("select_timeout_count", 1),
        ):
            metadata, result = fixture("fifo-select-reserved")
            result[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                analyzer.analyze(metadata, result)
        metadata, result = fixture()
        result["select_registered_order"] = 50
        with self.assertRaises(ValueError):
            analyzer.analyze(metadata, result)

    def test_all_required_events_are_nonzero_unique_and_causally_ordered(self):
        for mode in ("fifo-select-reserved", "fifo-select-published"):
            for field in analyzer.ORDER_FIELDS:
                metadata, result = fixture(mode)
                result[field] = 0
                with self.subTest(mode=mode, field=field), self.assertRaises(ValueError):
                    analyzer.analyze(metadata, result)
            for first, second in (
                ("holder_reserve_order", "holder_commit_order"),
                ("observer_trigger_order", "peer_ready_order"),
                ("peer_ready_order", "holder_resume_order"),
                ("observer_block_order", "holder_resume_order"),
                ("holder_commit_order", "peer_wait_resume_order"),
                ("holder_resume_order", "holder_return_order"),
                ("peer_reserve_order", "peer_commit_order"),
                ("select_wait_verified_order", "holder_enter_order"),
            ):
                metadata, result = fixture(mode)
                result[first], result[second] = result[second], result[first]
                with self.subTest(mode=mode, first=first, second=second), self.assertRaises(ValueError):
                    analyzer.analyze(metadata, result)
        metadata, result = fixture()
        result["peer_enter_order"] = result["holder_commit_order"]
        with self.assertRaisesRegex(ValueError, "distinct"):
            analyzer.analyze(metadata, result)

    def test_a_published_control_still_requires_original_holder_resumption(self):
        metadata, result = fixture("fifo-try-recv-published")
        result["holder_resume_order"], result["peer_enter_order"] = (
            result["peer_enter_order"], result["holder_resume_order"])
        result["holder_return_order"] = 20
        with self.assertRaisesRegex(ValueError, "holder_resume_order < peer_enter_order"):
            analyzer.analyze(metadata, result)

    def test_counts_retries_payloads_and_finite_interference_are_checked(self):
        changes = (("trigger_count", 0), ("holder_resume_count", 2),
                   ("holder_reserved_count", 2), ("holder_committed_count", 0),
                   ("peer_reserved_count", 0), ("peer_committed_count", 2),
                   ("holder_operation_calls", 2), ("peer_operation_calls", 2),
                   ("peer_retry_count", 1), ("messages_sent", 8), ("messages_received", 8),
                   ("payload_valid", False), ("interference_iterations", 0),
                   ("interference_iterations", 2_000_001), ("interference_elapsed_ns", 1_000_000_001),
                   ("outcome", "full"))
        for field, value in changes:
            metadata, result = fixture("fifo-try-send-reserved")
            result[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyzer.analyze(metadata, result)

    def test_repair_failure_or_absent_terminal_holds_never_pass(self):
        metadata, exemplar = fixture()
        for field in ("success", "diagnostic_valid", "completed_without_repair", "progress_contract_satisfied",
                      "initial_gate_waits_verified", "holder_done", "peer_done",
                      "final_native_waits_verified", "joins_complete", "watchdog_complete"):
            result = dict(exemplar, **{field: False})
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyzer.analyze(metadata, result)
        for field in ("recovery_applied", "hook_error"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyzer.analyze(metadata, dict(exemplar, **{field: True}))

    def test_types_cannot_smuggle_booleans_into_counts_or_orders(self):
        original = fixture("fifo-select-reserved")
        for index, row in enumerate(original):
            for field, value in row.items():
                if type(value) not in (int, bool):
                    continue
                rows = copy.deepcopy(original)
                rows[index][field] = 1 if type(value) is bool else True
                with self.subTest(record=index, field=field), self.assertRaises(ValueError):
                    analyzer.analyze(*rows)

    def test_runtime_cannot_self_certify_broader_or_complete_cohort_claims(self):
        for index in (0, 1):
            for field in ("physical_measurement", "physical_execution_verified", "physical_timing_qualified",
                          "priority_progress_qualified", "production_transport_qualified", "timing_qualified",
                          "hard_realtime_bound", "production_qualified", "candidate_contract_satisfied"):
                rows = fixture()
                rows[index][field] = True
                with self.subTest(record=index, field=field), self.assertRaises(ValueError):
                    analyzer.analyze(*rows)


if __name__ == "__main__":
    unittest.main()
