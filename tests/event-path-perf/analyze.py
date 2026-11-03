#!/usr/bin/env python3
"""Validate one finite P2 transcript; report raw ticks without timing promotion."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import re
import struct
import sys

BASE = Path(__file__).resolve().parents[1] / "std-channel-perf"
SPEC = importlib.util.spec_from_file_location(
    "event_path_p0_analysis", BASE / "analyze.py")
p0 = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(BASE))
try:
    SPEC.loader.exec_module(p0)
finally:
    sys.path.pop(0)
require, integer, boolean, unique_keys = p0.require, p0.integer, p0.boolean, p0.unique_keys

PREFIX = "EVENT_PATH_PERF "
SCHEMA = "event-path-perf-v1"
U32 = (1 << 32) - 1
U64 = (1 << 64) - 1
HALF = 1 << 31
TIMER_FAMILY = "timer-provider-handler"
TIMER_DELAY_NS = 50_000_000
CONTROL_FAMILIES = {"semaphore", "park", "fixed-peer"}
WAIT_FAMILIES = {"blocked", "semaphore", "park", TIMER_FAMILY}
THREAD_FAMILIES = WAIT_FAMILIES | {"fixed-peer", "finite-fanin"}
SCHEDULED_FANIN = {
    f"fanin-messaging-{providers}-{mode}": (schedule, period)
    for providers in (1, 4)
    for mode, schedule, period in (
        ("normal", "finite-absolute-1ms", 1_000_000),
        ("saturation", "finite-held-owner-burst", 0),
    )
}


def case_contracts() -> dict:
    cases = {}
    transports = {"std": "raw-std", "messaging": "messaging", "crossbeam": "crossbeam-direct"}
    for label, transport in transports.items():
        receive = "inbox-wait-none" if label == "messaging" else "recv"
        for family in ("ready", "blocked"):
            for suffix, admission in (("try", "try-send"), ("send", "send")):
                cases[f"{family}-{label}-{suffix}"] = (family, transport, admission, receive, 0, 1)
        cases[f"full-{label}"] = ("full-rejection", transport, "try-send", "untimed-validation-drain", 0, 1)
        for providers in (1, 4):
            cases[f"fanin-{label}-{providers}"] = (
                "finite-fanin", transport, "try-send", "recv-timeout-10ms", 0, providers)
    for providers in (1, 4):
        for mode in ("normal", "saturation"):
            cases[f"fanin-messaging-{providers}-{mode}"] = (
                "finite-fanin", "messaging", "try-send", "recv-timeout-10ms", 0, providers)
    for family in ("ready", "blocked"):
        cases[f"{family}-select-one"] = (
            family, "crossbeam-select-one", "try-send", "select-biased-one", 1, 1)
        cases[f"{family}-select-three"] = (
            family, "crossbeam-select-three-ordinary", "try-send", "select-biased-three-ordinary", 3, 1)
    for family, admission, receive in (
            ("semaphore", "publish-and-sem-post", "sem-clockwait"),
            ("park", "publish-and-unpark", "thread-park-predicate"),
            ("fixed-peer", "publish-and-yield-now", "known-peer-marker")):
        cases[family] = (family, "notification-control", admission, receive, 0, 1)
    cases["timer-provider-messaging"] = (
        TIMER_FAMILY, "messaging", "try-send", "inbox-wait-none", 0, 1)
    return cases


CASES = case_contracts()


def records(text: str):
    require(not p0.FAILURE.search(text) and "EVENT_PATH_PERF FAIL" not in text,
            "failure or kernel fault in transcript")
    rows = []
    for line in text.replace("\r", "").splitlines():
        if line.startswith(PREFIX):
            def invalid_constant(value):
                raise ValueError(f"invalid JSON constant: {value}")
            row = json.loads(line[len(PREFIX):], object_pairs_hook=unique_keys,
                             parse_constant=invalid_constant)
            require(isinstance(row, dict) and row.get("schema") == SCHEMA, "unknown record schema")
            require(isinstance(row.get("record"), str), "invalid record type")
            require(row.get("record") not in {"failure", "fatal"},
                    f"fixture failed: {row.get('reason', 'unspecified')}")
            # No firmware record may certify physical timing or general
            # transport progress, even if an unexpected field is injected.
            for key, value in row.items():
                if key.endswith("_qualified") or key == "physical_execution_verified":
                    require(value is False, f"firmware attempted qualification promotion: {key}")
                if key == "actual_switch_count":
                    require(value is None, "application markers cannot certify switch counts")
            rows.append(row)
            require(len(rows) <= 4096 * 4 + 7, "unbounded transcript record count")
        elif "EVENT_PATH_PERF" in line:
            raise ValueError("truncated or interleaved event-path record")
    require(len(rows) >= 4 and rows[0].get("record") == "experiment"
            and rows[-1].get("record") == "result", "missing experiment or terminal result")
    run_id = rows[0].get("run_id")
    require(isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,240}", run_id),
            "invalid run identity")
    require(all(row.get("run_id") == run_id for row in rows), "mixed run identities")
    index = 1
    providers = []
    while index < len(rows) - 1 and rows[index].get("record") == "provider":
        providers.append(rows[index])
        index += 1
    require(index < len(rows) - 1 and rows[index].get("record") == "scheduling",
            "missing or out-of-order scheduling record")
    scheduling = rows[index]
    samples = rows[index + 1:-1]
    require(all(row.get("record") == "sample" for row in samples),
            "mixed runs, unknown records or duplicate summaries")
    return rows[0], providers, scheduling, samples, rows[-1]


def raw_distribution(values: list[int], unit: str = "ticks"):
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(ordered), f"min_{unit}": ordered[0],
        f"p50_{unit}": ordered[math.ceil(len(ordered) * .50) - 1],
        f"p95_{unit}": ordered[math.ceil(len(ordered) * .95) - 1],
        f"p99_{unit}": ordered[math.ceil(len(ordered) * .99) - 1],
        f"max_{unit}": ordered[-1], "percentile_method": "nearest-rank",
    }


def payload_checksum(payload: str, sequence: int, provider: int) -> int:
    """Independent reconstruction from the fixture's published meaningful fields."""
    if payload == "bytes16":
        rotated_sequence = ((sequence << 7) | (sequence >> 25)) & U32
        rotated_provider = ((provider >> 3) | (provider << 29)) & U32
        words = [sequence, provider, rotated_sequence ^ provider ^ 0x9173AC5E,
                 ((~sequence) ^ rotated_provider ^ 0x13572468) & U32]
    elif payload == "imu-sample":
        floats = [float(sequence & 1023), .5, 9.75, float(provider & 1023), -.25, .125]
        words = [sequence, 0, sequence, provider, 5000]
        words += [struct.unpack("<I", struct.pack("<f", value))[0] for value in floats]
    else:
        require(payload == "none", "unknown payload checksum")
        return sequence
    checksum = 0
    for word in words:
        checksum = ((((checksum << 11) | (checksum >> 53)) & U64) + word) & U64
    return checksum


def validate_experiment(metadata: dict):
    case = metadata.get("case")
    require(isinstance(case, str) and case in CASES, "unknown named case")
    contract = CASES[case]
    for key, expected in zip(("family", "transport", "admission", "receive_api",
                              "selector_arms", "provider_count"), contract):
        require(metadata.get(key) == expected and type(metadata.get(key)) is type(expected),
                f"named case disagrees with {key}")
    family, _, _, _, arms, providers = contract
    native = metadata.get("runtime_kind") == "nuttx-unqualified"
    require(native or metadata.get("runtime_kind") == "host-functional-only", "unknown runtime")
    require(family != "fixed-peer" or native, "host cannot establish the fixed-peer FIFO contract")
    build_id = metadata.get("build_id")
    require(isinstance(build_id, str) and (re.fullmatch("[0-9a-f]{64}", build_id)
            or (not native and build_id == "unrecorded-host")), "invalid build identity")
    samples = integer(metadata, "samples", 1, 4096)
    warmup = integer(metadata, "warmup", 0, 1024)
    require(family != "finite-fanin" or (samples <= 256 and warmup <= 64),
            "finite fan-in storage bound exceeded")
    timer_case = family == TIMER_FAMILY
    require(not timer_case or (samples <= 128 and warmup <= 32),
            "timer-provider storage bound exceeded")
    require(boolean(metadata, "counts_include_warmup"), "count scope must include warmup")
    require(not boolean(metadata, "witness_hooks"), "timing fixture contains witness hooks")
    require(metadata.get("crossbeam_version") == "0.5.17", "unexpected Crossbeam version")
    require(integer(metadata, "worker_stack") == 16384, "unmatched worker stack")
    require(metadata.get("blocked_state_name") == "wait_sem", "unexpected native wait-state proof")
    require(metadata.get("active_arm") == "ordinary", "unexpected selected arm")
    release = SCHEDULED_FANIN.get(case)
    require(metadata.get("release_schedule") == (
        "native-timeout-after-owner-wait" if timer_case else release[0]
        if release else "finite-closed-loop-or-unscheduled-fanin"),
            "unexpected workload release schedule")
    # Older captures of the original cases did not emit these fields. Missing
    # fields never reinterpret an old run as a scheduled workload.
    if release or timer_case or "release_period_ns" in metadata:
        require(integer(metadata, "release_period_ns", 0, U64) == (release[1] if release else 0),
                "unexpected workload release period")
    if release or timer_case or "warmup_scope" in metadata:
        require(metadata.get("warmup_scope") == (
            "per-provider-prefix" if family == "finite-fanin" else "sequential-prefix"),
                "unexpected warmup scope")
    if timer_case or "timer_delay_ns" in metadata:
        require(integer(metadata, "timer_delay_ns", 0, U64) == (TIMER_DELAY_NS if timer_case else 0),
                "unexpected timer delay")
    payload = metadata.get("payload")
    capacity = integer(metadata, "capacity", 0, 16)
    size = integer(metadata, "payload_bytes", 0, 48)
    alignment = integer(metadata, "payload_alignment", 0, 8)
    if family in CONTROL_FAMILIES:
        require((payload, capacity, size, alignment) == ("none", 0, 0, 0),
                "notification control must not claim a message payload")
    elif payload == "bytes16":
        require((capacity, size, alignment) == (8, 16, 4), "baseline payload/capacity mismatch")
    else:
        require(payload == "imu-sample" and capacity == 16 and alignment in (4, 8)
                and size == ((44 + alignment - 1) // alignment) * alignment,
                "normalized IMU layout/capacity mismatch")
    require(integer(metadata, "stop_capacity") == (1 if arms == 3 else 0)
            and integer(metadata, "important_capacity") == (capacity if arms == 3 else 0),
            "selector admission capacities differ from named topology")
    require(integer(metadata, "counter_bits") == 32, "unexpected counter width")
    native_bits = integer(metadata, "counter_native_bits", 32, 64)
    require(native_bits in (32, 64), "unsupported native counter width")
    frequency = integer(metadata, "clock_hz", 1, 1_000_000_000_000)
    resolution = integer(metadata, "monotonic_resolution_ns", 1, U64)
    source = integer(metadata, "clock_source", 1, 7)
    request = metadata.get("clock_source_request")
    if native:
        require((request == "baseline" and source in (1, 2, 3, 7))
                or (request == "native-counter" and source in (5, 6)),
                "counter source disagrees with target/request")
        if source in (5, 6):
            require(native_bits == 32, "native CPU counter width mismatch")
    else:
        require((request, source, native_bits, frequency) == ("baseline", 4, 64, 1_000_000_000),
                "host counter identity mismatch")
    return family, providers, native, samples, warmup, frequency, resolution


def validate_scheduling(schedule: dict, provider_records: list, family: str, count: int, native: bool):
    sender = integer(schedule, "sender_tid", 1, (1 << 31) - 1)
    receiver = integer(schedule, "receiver_tid", 1, (1 << 31) - 1)
    require((sender == receiver) == (family not in THREAD_FAMILIES), "worker identity/topology mismatch")
    require(boolean(schedule, "single_core_verified") == native, "invalid single-core proof")
    require(boolean(schedule, "peer_ready_verified") == (family == "fixed-peer"), "invalid runnable-peer proof")

    def policy(row, policy_key, priority_key, expected):
        value = integer(row, policy_key, 0, 2)
        priority = integer(row, priority_key, 0, 255)
        if native:
            require(value == 1 and priority == expected, "unverified FIFO policy or priority")
        else:
            require((value == 0 and priority == 0) or (value in (1, 2) and 1 <= priority <= 99),
                    "invalid host scheduler readback")
        return value, priority

    sender_policy = policy(schedule, "sender_policy", "sender_priority", 120)
    receiver_priority = 180 if family in WAIT_FAMILIES | {"finite-fanin"} else 120
    policy(schedule, "receiver_policy", "receiver_priority", receiver_priority)
    if family not in THREAD_FAMILIES:
        require((schedule["receiver_policy"], schedule["receiver_priority"]) == sender_policy,
                "same-thread scheduling readbacks differ")
    require(len(provider_records) == (count if family == "finite-fanin" else 0),
            "provider schedule count mismatch")
    tids = set()
    for index, provider in enumerate(provider_records):
        require(integer(provider, "provider") == index, "provider identity gap or duplicate")
        tid = integer(provider, "tid", 1, (1 << 31) - 1)
        require(tid not in tids and tid != receiver, "provider task identity collision")
        tids.add(tid)
        observed = policy(provider, "policy", "priority", 120)
        if index == 0:
            require(tid == sender and observed == sender_policy, "sender does not identify provider zero")


def analyze(text: str) -> dict:
    metadata, providers, scheduling, samples, result = records(text)
    family, provider_count, native, count, warmup, frequency, resolution = validate_experiment(metadata)
    validate_scheduling(scheduling, providers, family, provider_count, native)
    expected_count = count * provider_count
    total = (count + warmup) * provider_count
    require(len(samples) == expected_count == integer(result, "sample_count", 1, 16384),
            "missing or extra retained samples")
    for key in ("success", "threads_joined", "structural_valid"):
        require(boolean(result, key), f"result did not establish {key}")
    for key in ("timing_qualified", "priority_progress_qualified", "physical_execution_verified", "clock_qualified"):
        require(not boolean(result, key), f"firmware attempted qualification promotion: {key}")
    require("actual_switch_count" in result and result["actual_switch_count"] is None,
            "scheduler switch count is not measured")
    terminal = boolean(result, "terminal_quiescence_verified")
    require(terminal == (native and family in THREAD_FAMILIES), "missing or fabricated final quiescence proof")
    attempted = integer(result, "attempted", 1, 20480)
    admitted = integer(result, "admitted", 0, total)
    rejected = integer(result, "rejected", 0, total)
    handled = integer(result, "handled", 0, total)
    require(attempted == total and admitted + rejected == attempted and handled == admitted,
            "attempted/admitted/rejected/handled conservation failed")
    if family == "full-rejection":
        require((admitted, rejected) == (0, total), "full case admitted an event")
    elif family != "finite-fanin":
        require((admitted, rejected) == (total, 0), "delivery case lost or rejected work")
    release = SCHEDULED_FANIN.get(metadata["case"])
    timer_case = family == TIMER_FAMILY
    strict_fields = release is not None or timer_case
    saturation = release is not None and release[1] == 0
    epoch = integer(result, "release_epoch_ns", 0, U64) if release else None
    if release:
        require(epoch + (count + warmup - 1) * release[1] <= U64,
                "absolute release schedule overflows u64")
    if not release:
        require(result.get("release_epoch_ns") is None
                and (not timer_case or "release_epoch_ns" in result),
                "unscheduled case claims a release epoch")
    if strict_fields or "owner_hold_verified" in result:
        require(boolean(result, "owner_hold_verified") == (native and saturation),
                "missing or fabricated saturation owner-hold proof")
    owner_release = integer(result, "owner_release_ticks", 0, U32) if saturation else None
    if not saturation:
        require(result.get("owner_release_ticks") is None
                and (not strict_fields or "owner_release_ticks" in result),
                "non-saturation case claims an owner release marker")
    if saturation:
        capacity = metadata["capacity"]
        require(total > capacity and (admitted, rejected) == (capacity, total - capacity),
                "saturation must fill the held-owner queue and reject every remaining attempt")

    admission_ticks, accepted_ticks, rejected_ticks, delivery_ticks, receive_ticks = [], [], [], [], []
    release_lateness = []
    deadline_lateness, provider_admission, provider_handler, receive_handler, handler_duration = [], [], [], [], []
    last_release = [0] * provider_count
    provider_full = [False] * provider_count
    retained_admitted = 0
    retained_checksum = 0
    retained_returns = 0
    next_sequence = [warmup + 1] * provider_count
    for index, sample in enumerate(samples):
        require(integer(sample, "index") == index, "sample index gap or duplicate")
        provider = integer(sample, "provider", 0, provider_count - 1)
        require(provider == index // count, "samples must retain provider-major order")
        sequence = integer(sample, "sequence", warmup + 1, count + warmup)
        require(sequence == next_sequence[provider], "provider sequence gap, duplicate or reorder")
        next_sequence[provider] += 1
        require(boolean(sample, "payload_valid"), "payload validation failed")
        accepted = boolean(sample, "accepted")
        if family == "full-rejection":
            require(not accepted, "full rejection sample claims admission")
        elif family != "finite-fanin":
            require(accepted, "delivery sample claims rejection")
        if release:
            planned = integer(sample, "planned_release_ns", 0, U64)
            observed = integer(sample, "release_observed_ns", 0, U64)
            require(planned == epoch + (sequence - 1) * release[1],
                    "planned release disagrees with epoch, sequence or period")
            require(observed >= planned and observed >= last_release[provider],
                    "observed release is early or decreases within a provider")
            last_release[provider] = observed
            release_lateness.append(observed - planned)
            if saturation:
                require(not accepted or not provider_full[provider],
                        "held-owner queue admitted work after a full rejection")
                provider_full[provider] |= not accepted
        else:
            require(sample.get("planned_release_ns") is None
                    and sample.get("release_observed_ns") is None
                    and (not timer_case or {"planned_release_ns", "release_observed_ns"} <= sample.keys()),
                    "unscheduled case claims planned or observed releases")
        require(boolean(sample, "blocked_verified") == (native and family in WAIT_FAMILIES),
                "missing or fabricated blocked-wait proof")
        peer = integer(sample, "peer_tid", 0, (1 << 31) - 1)
        require(peer == (scheduling["receiver_tid"] if family in CONTROL_FAMILIES else 0),
                "sample peer identity mismatch")
        returns = integer(sample, "park_returns", 0, U32)
        if family == "park":
            require(not native or returns >= 1, "blocked park has no completed park return")
        else:
            require(returns == 0, "non-parking sample claims park returns")
        retained_returns += returns
        start = integer(sample, "start_ticks", 0, U32)
        admission_end = integer(sample, "admission_return_ticks", 0, U32)
        receive = integer(sample, "receive_return_ticks", 0, U32)
        admission = (admission_end - start) & U32
        deltas = [admission]
        if timer_case:
            timer = sample.get("timer")
            require(isinstance(timer, dict) and set(timer) == {
                "plan_ns", "deadline_ns", "provider_resume_ns", "provider_resume_ticks",
                "provider_wait_observed_ns", "owner_wait_observed_ns", "provider_wait_verified",
                "expired", "handler_start_ticks", "handler_end_ticks",
            }, "missing or malformed timer evidence")
            plan = integer(timer, "plan_ns", 0, U64)
            deadline = integer(timer, "deadline_ns", 0, U64)
            resume_ns = integer(timer, "provider_resume_ns", 0, U64)
            require(plan + TIMER_DELAY_NS <= U64 and deadline == plan + TIMER_DELAY_NS,
                    "timer deadline differs from its checked delay")
            require(boolean(timer, "expired") and resume_ns >= deadline,
                    "timer did not expire before provider resume")
            require(boolean(timer, "provider_wait_verified") == native,
                    "missing or fabricated provider-wait proof")
            provider_wait = integer(timer, "provider_wait_observed_ns", 0, U64)
            owner_wait = integer(timer, "owner_wait_observed_ns", 0, U64)
            require((owner_wait <= plan and plan <= provider_wait < deadline) if native
                    else (owner_wait == 0 and provider_wait == 0),
                    "timer wait observations disagree with runtime or deadline")
            resume = integer(timer, "provider_resume_ticks", 0, U32)
            handler_start = integer(timer, "handler_start_ticks", 0, U32)
            handler_end = integer(timer, "handler_end_ticks", 0, U32)
            admission_start_delta = (start - resume) & U32
            admission_return_delta = (admission_end - resume) & U32
            receive_delta = (receive - resume) & U32
            handler_start_delta = (handler_start - resume) & U32
            handler_end_delta = (handler_end - resume) & U32
            require(admission_start_delta <= receive_delta <= handler_start_delta <= handler_end_delta
                    and admission_start_delta <= admission_return_delta,
                    "timer-provider admission, receive or handler markers are reordered")
            deltas.extend([admission_start_delta, admission_return_delta, receive_delta,
                           handler_start_delta, handler_end_delta])
            deadline_lateness.append(resume_ns - deadline)
            provider_admission.append(admission_start_delta)
            provider_handler.append(handler_end_delta)
            receive_handler.append(handler_start_delta - receive_delta)
            handler_duration.append(handler_end_delta - handler_start_delta)
        else:
            require(sample.get("timer") is None, "non-timer case claims timer evidence")
        if saturation:
            owner_release_delta = (owner_release - start) & U32
            require(admission <= owner_release_delta, "admission returned after owner release")
            deltas.append(owner_release_delta)
        admission_ticks.append(admission)
        (accepted_ticks if accepted else rejected_ticks).append(admission)
        receive_start = sample.get("receive_start_ticks")
        if family == "ready":
            begin = integer(sample, "receive_start_ticks", 0, U32)
            before = (begin - start) & U32
            delivered = (receive - start) & U32
            require(admission <= before <= delivered, "ready operation timestamps are reordered")
            receive_ticks.append((receive - begin) & U32)
            deltas.extend([before, delivered])
        else:
            require("receive_start_ticks" in sample and receive_start is None,
                    "unexpected receive-start timestamp for this case")
        if accepted:
            delivery = (receive - start) & U32
            deltas.append(delivery)
            delivery_ticks.append(delivery)
            retained_admitted += 1
            retained_checksum = (retained_checksum + payload_checksum(metadata["payload"], sequence, provider)) & U64
            if saturation:
                require(owner_release_delta <= delivery, "owner received before the held-owner release")
            if family == "fixed-peer":
                require(delivery <= admission, "peer marker occurred after yielding sender resumed")
        else:
            require(receive == 0, "rejected attempt has a receive timestamp")
        window = integer(sample, "elapsed_upper_ns", 1, U64)
        require(window >= resolution * 2, "monotonic window omits its resolution guard")
        if release:
            require(observed - epoch <= window - resolution * 2,
                    "observed release exceeds the active monotonic window")
        if timer_case:
            require(resume_ns - deadline <= window - resolution * 2,
                    "provider resume exceeds the post-deadline monotonic window")
        require(max(deltas) < HALF and window * frequency < HALF * 1_000_000_000,
                "ambiguous counter wrap or reversed timestamp")
        require(max(deltas) * 1_000_000_000 <= window * frequency,
                "counter delta exceeds monotonic window")
    require(next_sequence == [count + warmup + 1] * provider_count, "incomplete provider sequences")
    warmup_admitted = admitted - retained_admitted
    require(0 <= warmup_admitted <= warmup * provider_count, "warmup/retained admission totals disagree")
    if family == "full-rejection":
        preserved = sum(payload_checksum(metadata["payload"], slot, 1)
                        for slot in range(metadata["capacity"])) & U64
        retained_checksum = (preserved * count) & U64
        total_checksum = (preserved * (count + warmup)) & U64
    elif family != "finite-fanin" or warmup_admitted == warmup * provider_count:
        total_checksum = (retained_checksum + sum(
            payload_checksum(metadata["payload"], sequence, provider)
            for provider in range(provider_count) for sequence in range(1, warmup + 1))) & U64
    elif warmup_admitted == 0:
        total_checksum = retained_checksum
    else:
        # Excluded warmup admission is nondeterministic under fan-in contention.
        # Its aggregate checksum is retained, never claimed reconstructed.
        total_checksum = None
    require(integer(result, "retained_checksum", 0, U64) == retained_checksum,
            "retained payload checksum mismatch")
    observed_checksum = integer(result, "checksum", 0, U64)
    if total_checksum is not None:
        require(observed_checksum == total_checksum, "total payload checksum mismatch")
    park_returns = integer(result, "park_returns", 0, U32)
    if family == "park":
        require(park_returns >= retained_returns + (warmup if native else 0),
                "parking returns omit completed waits")
        require(warmup != 0 or park_returns == retained_returns, "parking-return total mismatch")
    else:
        require(park_returns == 0, "non-parking case claims park returns")
    return {
        "schema": "event-path-analysis-v1", "metadata": metadata,
        "providers": providers, "scheduling": scheduling, "result": result, "samples": samples,
        "run_id": metadata["run_id"], "outcome": metadata["runtime_kind"],
        "structural_valid": True, "timing_qualified": False, "priority_progress_qualified": False,
        "physical_execution_verified": False, "clock_qualified": False,
        "total_checksum_reconstructed": total_checksum is not None,
        "retained_admitted": retained_admitted, "retained_rejected": expected_count - retained_admitted,
        "release_lateness": ({
            "scope": "Retained attempts only; unqualified monotonic observations before admission.",
            "timing_qualified": False,
            "observed_ns": raw_distribution(release_lateness, "ns"),
        } if release else None),
        "timer_deadline_lateness": ({
            "scope": "Retained provider resumes after the planned deadline; unqualified monotonic observations, not IRQ timestamps.",
            "timing_qualified": False,
            "observed_ns": raw_distribution(deadline_lateness, "ns"),
        } if timer_case else None),
        "raw_ticks": {
            "admission_call": raw_distribution(admission_ticks),
            "accepted_admission_call": raw_distribution(accepted_ticks),
            "rejected_admission_call": raw_distribution(rejected_ticks),
            "admission_to_receive_return": raw_distribution(delivery_ticks),
            "ready_receive_call": raw_distribution(receive_ticks),
            "provider_to_admission_start": raw_distribution(provider_admission),
            "provider_to_handler_end": raw_distribution(provider_handler),
            "receive_to_handler_start": raw_distribution(receive_handler),
            "handler_duration": raw_distribution(handler_duration),
        },
        "scope": "Finite-case characterization; no calibrated duration, pure switch cost, offered-load or production progress claim.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expect-runtime", choices=("host-functional-only", "nuttx-unqualified"))
    args = parser.parse_args()
    try:
        summary = analyze(args.transcript.read_text())
        if args.expect_runtime:
            require(summary["outcome"] == args.expect_runtime, "unexpected runtime outcome")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"event-path transcript rejected: {error}\n")
    encoded = json.dumps(summary, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
