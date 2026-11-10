"""Validate the named equal-FIFO reservation-exclusion observations.

The common analyzer owns strict transcript decoding and source/build/session
binding. These checks derive the case result from native state, source-store
ordering and payload conservation. A complete candidate cohort is a separate
gate; one case cannot qualify a production transport, a physical board or time.
"""
from __future__ import annotations

import re

SCHEMA = "fifo-channel-progress-v1"
CONTRACT = "equal-fifo-run-to-block-v1"
BUILD_MODE = "fifo-channel-suite"
SCOPE = "equal-fifo-reservation-progress"
MODES = (
    "fifo-try-recv-reserved", "fifo-try-recv-published",
    "fifo-select-reserved", "fifo-select-published",
    "fifo-try-send-reserved", "fifo-try-send-reclaimed",
)
ORDER_FIELDS = (
    "holder_enter_order", "peer_enter_order", "select_registered_order",
    "select_wait_verified_order", "holder_reserve_order", "holder_commit_order",
    "trigger_order", "observer_trigger_order", "peer_ready_order", "observer_block_order",
    "holder_resume_order", "holder_return_order", "peer_reserve_order", "peer_commit_order",
    "peer_wait_resume_order", "peer_return_order",
)
SELECT_ORDERS = ("select_registered_order", "select_wait_verified_order", "peer_wait_resume_order")
SCHEDULE = {"holder": 180, "peer": 180, "observer": 220, "watchdog": 240}
CLAIMS = (
    "physical_measurement", "physical_execution_verified", "physical_timing_qualified",
    "priority_progress_qualified", "production_transport_qualified", "timing_qualified",
    "hard_realtime_bound", "production_qualified", "candidate_contract_satisfied",
)
EXCLUSIONS = (
    "async_channel_access", "manual_selected_operations", "selected_send",
    "endpoints_changed_during_window",
)
HEX256 = re.compile(r"[0-9a-f]{64}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(row, field, lower=0, upper=2**32 - 1):
    value = row.get(field)
    require(type(value) is int and lower <= value <= upper, f"invalid {field}")
    return value


def boolean(row, field):
    value = row.get(field)
    require(type(value) is bool, f"invalid {field}")
    return value


def ordered(orders, *fields):
    values = [orders[field] for field in fields]
    require(all(value > 0 for value in values)
            and all(first < second for first, second in zip(values, values[1:])),
            "incorrect FIFO event order: " + " < ".join(fields))


def analyze(metadata, result):
    """Validate a decoded metadata/result pair; return an unbound case result."""
    require(isinstance(metadata, dict) and isinstance(result, dict), "records must be objects")
    require(metadata.get("schema") == result.get("schema") == SCHEMA,
            "unknown equal-FIFO record schema")
    require(metadata.get("record") == "metadata" and result.get("record") == "result",
            "wrong equal-FIFO record order")
    run_id = metadata.get("run_id")
    require(isinstance(run_id, str) and bool(run_id.strip()) and result.get("run_id") == run_id,
            "missing or mixed equal-FIFO run identities")
    mode = metadata.get("mode")
    require(isinstance(mode, str) and mode in MODES, "unknown equal-FIFO case")
    require(result.get("mode") == mode, "mixed equal-FIFO case identities")
    selected, reverse = mode.startswith("fifo-select-"), mode.startswith("fifo-try-send-")
    reserved = mode.endswith("-reserved")
    trigger = (0x303 if reserved else 0x304) if reverse else (0x301 if reserved else 0x302)
    commit = 0x304 if reverse else 0x302

    require(metadata.get("contract") == CONTRACT and metadata.get("build_mode") == BUILD_MODE
            and integer(metadata, "hook_stage", 0, 5) == 5,
            "equal-FIFO case needs its own source recipe and scheduling contract")
    require(metadata.get("runtime") == "nuttx", "equal-FIFO proof requires native NuttX evidence")
    require(metadata.get("transport") == "crossbeam-channel"
            and metadata.get("transport_version") == "0.5.17", "wrong equal-FIFO transport source")
    for field, expected in (("capacity", 8), ("payload_bytes", 16), ("provider_count", 1),
                            ("arm_count", 3 if selected else 1), ("trigger_stage", trigger),
                            ("commit_stage", commit), ("participant_operation_budget", 1),
                            ("drain_budget", 8 if reverse else 0),
                            ("select_timeout_ms", 200 if selected else 0),
                            ("interference_iteration_limit", 2_000_000),
                            ("interference_time_limit_ms", 1000)):
        require(integer(metadata, field) == expected, f"unexpected equal-FIFO {field}")
    require(metadata.get("selection_bias") == ("stop-important-ordinary" if selected else "none"),
            "wrong selected-owner arm order")
    require(metadata.get("holder_api") == ("try_recv" if reverse else "try_send")
            and metadata.get("peer_api") == ("select_biased" if selected else "try_send" if reverse else "try_recv"),
            "wrong equal-FIFO API pair")
    require(boolean(metadata, "single_core_tcb_witness"), "missing single-core native state proof")
    build_id = metadata.get("build_id")
    require(isinstance(build_id, str) and HEX256.fullmatch(build_id), "missing firmware build identity")
    for role, priority in SCHEDULE.items():
        require(integer(metadata, role + "_policy", -1, 2) == 1
                and integer(metadata, role + "_priority", -1, 255) == priority,
                f"unverified FIFO scheduling contract for {role}")
    for field in EXCLUSIONS:
        require(boolean(metadata, field) is False, f"unsupported equal-FIFO admission: {field}")
    for row in (metadata, result):
        for field in CLAIMS:
            if field in row:
                require(boolean(row, field) is False,
                        "one runtime case cannot certify cohort, production, physical or timing qualification")
    for field in ("priority_progress_qualified", "production_transport_qualified", "timing_qualified"):
        require(boolean(result, field) is False, f"unsupported qualification: {field}")

    for field in ("success", "diagnostic_valid", "completed_without_repair", "progress_contract_satisfied",
                  "payload_valid", "initial_gate_waits_verified", "holder_done", "peer_done",
                  "final_native_waits_verified", "joins_complete", "watchdog_complete"):
        require(boolean(result, field), f"incomplete equal-FIFO observation: {field}")
    for field in ("recovery_applied", "hook_error", "holder_hook_returned_at_ready"):
        require(boolean(result, field) is False, f"invalid equal-FIFO observation: {field}")
    for field in ("failure_reason", "cleanup_error"):
        require(result.get(field) == "none", f"recorded equal-FIFO failure: {field}")
    for field in ("trigger_count", "holder_resume_count", "holder_reserved_count", "holder_committed_count",
                  "peer_reserved_count", "peer_committed_count", "holder_operation_calls", "peer_operation_calls"):
        require(integer(result, field) == 1, f"missing or repeated primary operation: {field}")
    require(integer(result, "peer_retry_count") == 0, "retry loop cannot qualify a single attempt")
    require(integer(result, "messages_sent") == integer(result, "messages_received") == (9 if reverse else 1),
            "wrong checked payload conservation")
    require(integer(result, "holder_commit_count_at_ready", 0, 1) == (0 if reserved else 1),
            "peer was not made READY on the required side of the slot release")

    for state, priority, expected_state, expected_priority in (
        ("holder_state_at_trigger", "holder_priority_at_trigger", "ready", 180),
        ("peer_state_at_trigger", "peer_priority_at_trigger", "wait_sem", 180),
        ("peer_ready_state", "peer_ready_priority", "ready", 180),
        ("holder_state_at_ready", "holder_priority_at_ready", "ready", 180),
        ("observer_state_at_holder_resume", "observer_priority_at_holder_resume", "wait_sem", 220),
    ):
        require(result.get(state) == expected_state
                and integer(result, priority, -1, 255) == expected_priority,
                f"missing original native state/priority observation: {state}")
    require(boolean(result, "initial_select_wait_verified") == selected,
            "wrong initial native selected-wait proof")
    for field, expected in (("registered_arms", 3 if selected else 0),
                            ("select_registration_count", 1 if selected else 0),
                            ("select_wait_resume_count", 1 if selected else 0),
                            ("select_timeout_count", 0)):
        require(integer(result, field) == expected, f"missing or repeated selected-wait evidence: {field}")
    require(result.get("select_outcome") == ("important" if selected else "none"),
            "selection timeout or another arm cannot count as important delivery")
    integer(result, "interference_iterations", 1, metadata["interference_iteration_limit"])
    integer(result, "interference_elapsed_ns", 0, metadata["interference_time_limit_ms"] * 1_000_000)

    orders = {field: integer(result, field) for field in ORDER_FIELDS}
    present = [value for value in orders.values() if value]
    require(len(present) == len(set(present)), "event IDs must identify distinct FIFO observations")
    ordered(orders, "holder_enter_order", "holder_reserve_order", "holder_commit_order", "holder_return_order")
    boundary = "holder_reserve_order" if reserved else "holder_commit_order"
    ordered(orders, boundary, "trigger_order", "observer_trigger_order", "peer_ready_order",
            "observer_block_order", "holder_resume_order", "holder_return_order")
    if reserved:
        ordered(orders, "holder_resume_order", "holder_commit_order")
    if selected:
        ordered(orders, "peer_enter_order", "select_registered_order", "select_wait_verified_order",
                "holder_enter_order")
        ordered(orders, "holder_commit_order", "peer_wait_resume_order", "peer_reserve_order",
                "peer_commit_order", "peer_return_order")
        ordered(orders, "holder_resume_order", "peer_wait_resume_order")
    else:
        require(all(orders[field] == 0 for field in SELECT_ORDERS),
                "direct operation fabricated selected-wait events")
        ordered(orders, "holder_commit_order", "peer_enter_order", "peer_reserve_order",
                "peer_commit_order", "peer_return_order")
        ordered(orders, "holder_resume_order", "peer_enter_order")
    require(result.get("outcome") == "completed-without-repair",
            "outcome disagrees with equal-FIFO ordering and completion")
    return {
        "schema": "channel-progress-analysis-v1", "run_id": run_id, "mode": mode,
        "contract": CONTRACT, "scope": SCOPE, "fixture_valid": True,
        "outcome": "completed-without-repair", "completed_without_repair": True,
        "progress_contract_satisfied": True, "build_bound": False, "runtime_kind": "unbound",
        "priority_progress_qualified": False, "production_transport_qualified": False,
        "physical_execution_verified": False, "physical_timing_qualified": False,
        "timing_qualified": False, "hard_realtime_bound": False,
        "metadata": metadata, "result": result,
    }
