"""Validate native single-item std transfer evidence without a transport claim.

The common analyzer owns strict JSON decoding, transcript order and build/session
binding. This module checks the evidence for each primitive case. A completed
Busy result proves the named single attempt returned; it never counts as an
accepted or delivered payload.
"""
from __future__ import annotations

import re

SCHEMA = "std-transfer-progress-v1"
BUILD_MODE = "std-mutex-suite"
MODES = (
    "mutex-admit-held", "mutex-admit-released",
    "mutex-take-held", "mutex-take-released",
    "condvar-wait-publish", "condvar-publish-before-wait",
    "condvar-close-wait",
)
SCHEDULE = {"caller": 180, "holder": 120, "observer": 220, "watchdog": 240}
COUNTERS = ("initial_value_count", "accepted", "transferred", "final_value_count", "busy_count")
EXPECTED = {
    "mutex-admit-held": ("busy", (0, 0, 0, 0, 1)),
    "mutex-admit-released": ("accepted", (0, 1, 1, 0, 0)),
    "mutex-take-held": ("busy", (1, 0, 0, 1, 1)),
    "mutex-take-released": ("taken", (1, 0, 1, 0, 0)),
    "condvar-wait-publish": ("taken", (0, 1, 1, 0, 0)),
    "condvar-publish-before-wait": ("taken", (0, 1, 1, 0, 0)),
    "condvar-close-wait": ("closed", (0, 0, 0, 0, 0)),
}
ORDER_FIELDS = (
    "prewarm_order", "caller_start_order", "holder_start_order", "holder_guard_order",
    "caller_attempt_order", "caller_return_order", "holder_release_order",
    "wait_verified_order", "publish_order", "close_order",
)
CLAIMS = (
    "physical_measurement", "physical_execution_verified", "physical_timing_qualified",
    "priority_progress_qualified", "production_transport_qualified", "timing_qualified",
    "hard_realtime_bound", "production_qualified",
)
HEX256 = re.compile(r"[0-9a-f]{64}\Z")

# The source ledger uses paths relative to the complete Rust library tree.
# Independently hashed at rust-lang/rust 52618eb338609df44978b0ca4451ab7941fd1c7a
# and esp-rs/rust abf50ae2e46066e67e29fe856f9764c23aa9a3ca, then checked against
# retained prepared-SDK ledgers. Common nxrs ABI patches do not edit these files.
STD_SOURCE_SHA256 = {
    "std/src/sync/poison/mutex.rs": "16ef24b3381c0a785889baee19744ade4346be56a42ea9778034e881af56900f",
    "std/src/sync/poison/condvar.rs": "b8202047cd8cf8bf5ff03336eefc4c03a0e546d18ea60f59deaca28be1be37e4",
    "std/src/sys/sync/mutex/mod.rs": "d7220a27794733fb47c515a991ecd2123b5ba4823d28674eb7324dfcfd66d567",
    "std/src/sys/sync/mutex/pthread.rs": "1aa80435780abe34a365179635e92fb5935894fb9b4f615eb45b336958c28035",
    "std/src/sys/pal/unix/sync/mutex.rs": "1df3c112f84ccfa7ff048aa88a604dc6029b405f59e0aa96d35381b589aa9b17",
    "std/src/sys/sync/condvar/mod.rs": "34b21d5ee225213988cf772a4ceae69d20ecdb5102d259a2644575b041185497",
    "std/src/sys/sync/condvar/pthread.rs": "515dac39964e931abbcf56fa963254adab34cbd7a76916ccd03fd4d863acfd4f",
    "std/src/sys/pal/unix/sync/condvar.rs": "4b21456c5b928b032904232b0aab6dde205c3edbd8562c522a5274e23cd796dc",
    "std/src/sys/sync/once_box.rs": "395ab1241ab919abedd060b790527a605763b5a94260e51e702ba2df5852ad6c",
}
ESP_STD_SOURCE_SHA256 = {
    **STD_SOURCE_SHA256,
    "std/src/sync/poison/mutex.rs": "d8ac9dc942771d5767f3398d73eaa8c73d9f31c75a5b71f674b59f1921719e24",
    "std/src/sync/poison/condvar.rs": "cb3bfbca24e41ff5f16157bc7206f1a2362513e5a65fb80cfb2cdcd6c8e8f305",
    "std/src/sys/sync/mutex/mod.rs": "ea2feae381d8e1be81a8ca4d8c13c5c3560e082d10fc9835f98dbede186bf5f6",
    "std/src/sys/sync/condvar/mod.rs": "207002bea900f8851de508c9aa4a2d1ac3b08a3464155d5d66291cd014824d51",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_std_sources(library, sdk):
    """Require the audited API, dispatch, pthread and initialization sources.

    The caller separately verifies the complete ledger hash and its binding to
    the compiled private std. Choosing the exact SDK map rejects a mixed-source
    dispatch even when an outer ledger has been resealed consistently.
    """
    require(isinstance(library, dict), "std primitive source ledger must be an object")
    require(isinstance(sdk, str) and sdk in {"nightly-2025-09-15", "esp-1.90.0.0"},
            "unqualified SDK for std primitive sources")
    expected = STD_SOURCE_SHA256 if sdk == "nightly-2025-09-15" else ESP_STD_SOURCE_SHA256
    for path, digest in expected.items():
        require(library.get(path) == digest, f"missing or changed audited std primitive source: {path}")


def integer(row, field, lower=0, upper=2**32 - 1):
    value = row.get(field)
    require(type(value) is int and lower <= value <= upper, f"invalid {field}")
    return value


def boolean(row, field):
    value = row.get(field)
    require(type(value) is bool, f"invalid {field}")
    return value


def _ordered(orders, *fields):
    values = [orders[field] for field in fields]
    require(all(value > 0 for value in values)
            and all(first < second for first, second in zip(values, values[1:])),
            "incorrect event order: " + " < ".join(fields))


def analyze(metadata, result):
    """Validate a decoded metadata/result pair; return an unbound summary."""
    require(isinstance(metadata, dict) and isinstance(result, dict), "records must be objects")
    require(metadata.get("schema") == result.get("schema") == SCHEMA,
            "unknown std transfer record schema")
    require(metadata.get("record") == "metadata" and result.get("record") == "result",
            "wrong std transfer record order")
    run_id = metadata.get("run_id")
    require(isinstance(run_id, str) and bool(run_id.strip()) and result.get("run_id") == run_id,
            "missing or mixed std transfer run identities")
    mode = metadata.get("mode")
    require(isinstance(mode, str) and mode in MODES, "unknown std transfer mode")
    require(result.get("mode") == mode, "mixed std transfer case identities")
    mutex = mode.startswith("mutex-")
    held = mutex and mode.endswith("-held")
    waiting = mode in {"condvar-wait-publish", "condvar-close-wait"}
    expected_outcome, expected_counts = EXPECTED[mode]

    require(metadata.get("build_mode") == BUILD_MODE
            and integer(metadata, "hook_stage", 0, 4) == 0,
            "std transfer needs the clean shared suite image")
    require(metadata.get("runtime") == "nuttx", "std transfer requires native NuttX evidence")
    require(metadata.get("transport") == "std-mutex-condvar", "wrong transfer primitives")
    for field, value in (("capacity", 1), ("payload_bytes", 16), ("provider_count", 1), ("arm_count", 1)):
        require(integer(metadata, field, 1) == value, f"unexpected {field}")
    require(metadata.get("selection_bias") == "none", "std primitive cases do not select queues")
    require(metadata.get("witness_api") == ("try_lock" if mutex else "condvar"),
            "wrong standard API under observation")
    build_id = metadata.get("build_id")
    require(isinstance(build_id, str) and HEX256.fullmatch(build_id),
            "missing firmware build identity")
    for field in ("single_core_tcb_witness", "prewarmed", "prewarm_before_workers"):
        require(boolean(metadata, field), f"missing primitive setup proof: {field}")
    for role, priority in SCHEDULE.items():
        require(integer(metadata, role + "_policy", -1, 2) == 1
                and integer(metadata, role + "_priority", -1, 255) == priority,
                f"unverified FIFO scheduling contract for {role}")

    for row in (metadata, result):
        for field in CLAIMS:
            if field in row:
                require(boolean(row, field) is False,
                        "runtime cannot self-certify production, physical or timing qualification")
    for field in ("priority_progress_qualified", "production_transport_qualified", "timing_qualified"):
        require(boolean(result, field) is False, f"primitive result cannot qualify {field}")
    for field in ("success", "diagnostic_valid", "completed_without_repair",
                  "primitive_contract_satisfied", "payload_valid", "caller_start_gate_verified",
                  "holder_start_gate_verified", "worker_priorities_verified", "caller_done",
                  "holder_done", "joins_complete", "watchdog_complete"):
        require(boolean(result, field), f"incomplete std transfer experiment: {field}")
    require(boolean(result, "recovery_applied") is False,
            "std transfer contract cannot pass after priority repair")

    require(integer(result, "try_lock_calls") == (1 if mutex else 0),
            "try-lock admission must make exactly one attempt")
    counts = tuple(integer(result, field, 0, 1) for field in COUNTERS)
    require(counts == expected_counts, "ownership/admission/delivery counts disagree with the case")
    initial, accepted, transferred, final, _ = counts
    require(initial + accepted == transferred + final, "owned values were lost or duplicated")
    for field, expected in (
        ("original_payload_retained", mode == "mutex-admit-held"),
        ("protected_payload_retained", mode == "mutex-take-held"),
        ("guard_held_at_attempt", held),
        ("guard_held_at_return", held),
        ("initial_condvar_wait_verified", waiting),
    ):
        require(boolean(result, field) == expected, f"missing or inconsistent {field}")
    waits = integer(result, "condvar_wait_calls")
    require(waits >= 1 if waiting else waits == 0,
            "condition-wait count disagrees with the predicate case")
    require(result.get("initial_wait_state") == ("wait_sem" if waiting else "unavailable")
            and integer(result, "initial_wait_priority", -1, 255) == (180 if waiting else -1),
            "condition wait lacks the actual caller TCB state and original priority")

    orders = {field: integer(result, field) for field in ORDER_FIELDS}
    present = [value for value in orders.values() if value]
    require(len(present) == len(set(present)), "event IDs must identify distinct observations")
    _ordered(orders, "prewarm_order", "caller_start_order")
    _ordered(orders, "prewarm_order", "holder_start_order")
    workers_started = max(orders["caller_start_order"], orders["holder_start_order"])
    require(all(value > workers_started for field, value in orders.items()
                if value and field not in {"prewarm_order", "caller_start_order", "holder_start_order"}),
            "an operation preceded completed worker setup")

    if mutex:
        require(orders["wait_verified_order"] == orders["publish_order"] == orders["close_order"] == 0,
                "mutex case fabricated condition-wait or publication events")
        if held:
            _ordered(orders, "holder_guard_order", "caller_attempt_order",
                     "caller_return_order", "holder_release_order")
        else:
            _ordered(orders, "holder_guard_order", "holder_release_order",
                     "caller_attempt_order", "caller_return_order")
    elif waiting:
        changed = "close_order" if mode == "condvar-close-wait" else "publish_order"
        absent = "publish_order" if mode == "condvar-close-wait" else "close_order"
        require(orders[absent] == 0, "wrong condition-predicate transition")
        _ordered(orders, "caller_attempt_order", "wait_verified_order", "holder_guard_order",
                 changed, "holder_release_order", "caller_return_order")
    else:
        require(orders["wait_verified_order"] == orders["close_order"] == 0,
                "publication-before-wait case fabricated a wait or close")
        _ordered(orders, "holder_guard_order", "publish_order", "holder_release_order",
                 "caller_attempt_order", "caller_return_order")

    require(result.get("outcome") == expected_outcome,
            "outcome disagrees with independent ownership and primitive observations")
    return {
        "schema": "channel-progress-analysis-v1", "run_id": run_id,
        "mode": mode, "fixture_valid": True, "outcome": expected_outcome,
        "scope": "std-transfer-primitives", "completed_without_repair": True,
        "progress_contract_satisfied": True, "primitive_contract_satisfied": True,
        "priority_progress_qualified": False, "production_transport_qualified": False,
        "timing_qualified": False, "physical_timing_qualified": False,
        "physical_execution_verified": False, "hard_realtime_bound": False,
        "runtime_kind": "unbound", "build_bound": False,
        "metadata": metadata, "result": result,
    }
