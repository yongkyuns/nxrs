"""Validation for fresh RAM observations, separate from historical timing data.

An observed watermark is not a worst-case stack bound. Unknown observations are
represented explicitly and must never become a measured zero-byte stack.
"""
import re


ALLOC_PROFILES = ("alloc-baseline", "alloc-threads-0", "alloc-threads-1",
                  "alloc-threads-5", "alloc-threads-10", "alloc-threads-20",
                  "alloc-queues")
TRAFFIC_PROFILES = ("normal", "burst", "work-medium", "work-long", "io-wait", "saturation")


def reject_faults(text):
    if re.search(r"ES_FAIL|\bpanic(?:ked)?\b|\bassertion failed\b|\bassert failed\b|\bGuru Meditation\b|\bFATAL ERROR\b", text, re.I):
        raise ValueError("capture contains a firmware failure, including after completion")


def rows(text, marker):
    found = []
    for line in text.splitlines():
        if not line.startswith(marker + " "):
            continue
        row = {}
        for token in line[len(marker) + 1:].split():
            if token.count("=") != 1:
                raise ValueError(f"{marker}: malformed field")
            key, value = token.split("=")
            if not key or not value or key in row:
                raise ValueError(f"{marker}: empty or duplicate field")
            row[key] = int(value) if re.fullmatch(r"-?\d+", value) else value
        if not row:
            raise ValueError(f"{marker}: empty record")
        found.append(row)
    return found


def number(row, key):
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"{key} must be a nonnegative integer")
    return value


def stack_rows(text, platform, workers=20, configuration=None, require_observed=False):
    observations = rows(text, "ES_STACK")
    seen = set()
    for row in observations:
        scope = row.get("scope")
        if scope not in ("worker", "shared", "main", "idle", "irq", "workqueue"):
            raise ValueError("unknown stack scope")
        identity = (scope, number(row, "id"))
        if identity in seen:
            raise ValueError("duplicate stack observation")
        seen.add(identity)
        number(row, "requested")
        reserved, used, available = (number(row, key) for key in ("reserved", "used", "available"))
        if available not in (0, 1) or used > reserved:
            raise ValueError("invalid stack capacity or availability")
        if not available and used:
            raise ValueError("unavailable stack cannot claim observed usage")
        if available and (reserved == 0 or (scope in ("worker", "shared") and used == 0)):
            raise ValueError("observed stack must include nonzero execution usage")
        capture = row.get("capture")
        expected_capture = ("post-main-cumulative" if scope == "shared" else
                            "before_thread_return" if scope == "worker" else "report_snapshot")
        if capture not in (expected_capture, "disabled", "unavailable") or (available and capture != expected_capture):
            raise ValueError("stack capture coverage disagrees with availability")
        if "guard_ok" in row and number(row, "guard_ok") != 1:
            raise ValueError("stack guard was damaged or not verified")
        if scope == "shared" and available:
            painted, unpainted, protected, guard = (number(row, key) for key in
                ("painted_bytes", "unpainted_bytes", "protected_bytes", "guard_bytes"))
            if (number(row, "guard_ok") != 1 or number(row, "sp_gap_bytes") != 256 or
                    painted + unpainted + protected != reserved or used < unpainted or
                    not 0 < guard <= painted or protected != 128 or
                    reserved - used < protected + guard):
                raise ValueError("shared stack coverage or guard accounting is contradictory")
        if configuration and scope in ("worker", "shared"):
            key = "worker_stack_bytes" if scope == "worker" else "shared_stack_bytes"
            requested = number(row, "requested")
            if requested != configuration.get(key):
                raise ValueError("stack request disagrees with frozen build configuration")
    primary = {(scope, identifier) for scope, identifier in seen if scope in ("worker", "shared")}
    expected = {("shared", 0)} if platform == "embassy" else {("worker", i) for i in range(workers)}
    if primary != expected:
        raise ValueError("missing or unexpected service stack observations")
    if require_observed and any(not row["available"] for row in observations
                                if (row["scope"], row["id"]) in expected):
        raise ValueError("service stack observations unavailable; rebuild with stack watermark support")
    return observations


def stack_summary(observations):
    observed = [row for row in observations if row["available"]]
    return {
        "observed": len(observed),
        "unavailable": len(observations) - len(observed),
        "minimum_observed_remaining_bytes": min(
            (row["reserved"] - row["used"] for row in observed), default=None),
        "minimum_observed_remaining_above_guards_bytes": min(
            (max(0, row["reserved"] - row["used"] - row.get("protected_bytes", 0)
                 - row.get("guard_bytes", 0)) for row in observed), default=None),
        "worst_case_bound": False,
        "note": "Coverage is reported per stack. Pre-exit scans omit teardown; shared scans omit early startup.",
    }


def allocation_output(text, profile, platform, configuration):
    reject_faults(text)
    if profile not in ALLOC_PROFILES or platform == "embassy":
        raise ValueError("unsupported allocation control")
    if "ES_FAIL" in text or text.splitlines().count("ES_PASS") != 1:
        raise ValueError("allocation control did not pass")
    if rows(text, "ES_COMMAND_EXIT") != [{"status": 0}]:
        raise ValueError("allocation control did not exit successfully")
    terminal = rows(text, "ES_ALLOC_RESULT")
    workers = int(profile.rsplit("-", 1)[1]) if profile.startswith("alloc-threads-") else 0
    if len(terminal) != 1 or terminal[0] != dict(
            platform=platform, profile=profile, requested_workers=workers, errors=0):
        raise ValueError("allocation result identity or qualification failure")
    phases = rows(text, "ES_ALLOC")
    expected = (["baseline", "threads", "threads-joined", "cleanup"] if profile.startswith("alloc-threads-")
                else ["baseline", "queues-empty", "queues-full", "queues-drained", "cleanup"]
                if profile == "alloc-queues" else ["baseline"])
    if [row.get("stage") for row in phases] != expected:
        raise ValueError("allocation phase order or completeness mismatch")
    for row in phases:
        for key in ("heap_used", "available", "requested_workers", "live_workers",
                    "worker_stack_requested", "worker_stack_reserved", "worker_stack_reservation_available",
                    "queue_payload_capacity", "static_storage",
                    "diagnostics", "active_queues"):
            number(row, key)
        if row["available"] not in (0, 1) or (not row["available"] and row["heap_used"]):
            raise ValueError("invalid allocator availability")
        if row.get("storage") != ("static" if platform == "zephyr-c" else "dynamic"):
            raise ValueError("allocation storage policy mismatch")
        if row.get("static_scope") != "adapter":
            raise ValueError("static storage must identify its limited adapter scope")
        if (row["diagnostics"] > row["static_storage"] or
                row["static_storage"] != phases[0]["static_storage"] or
                row["diagnostics"] != phases[0]["diagnostics"]):
            raise ValueError("adapter static storage and its diagnostic subset are contradictory")
        live = workers if row["stage"] == "threads" else 0
        if row["live_workers"] != live or row["requested_workers"] != workers:
            raise ValueError("allocation worker lifecycle mismatch")
        active_queues = configuration["queues"] if row["stage"].startswith("queues-") else 0
        if row["active_queues"] != active_queues:
            raise ValueError("allocation queue lifecycle mismatch")
        static = platform == "zephyr-c"
        requested = configuration["worker_stack_bytes"]
        if row["worker_stack_requested"] != live * requested:
            raise ValueError("allocation stack requests disagree with build")
        if static:
            if (row["worker_stack_reservation_available"] != 1 or
                    row["worker_stack_reserved"] < 20 * requested or
                    row["worker_stack_reserved"] != phases[0]["worker_stack_reserved"]):
                raise ValueError("static stack reservations are missing or inconsistent")
        elif row["worker_stack_reservation_available"] != 0 or row["worker_stack_reserved"] != 0:
            raise ValueError("pthread requests cannot claim measured allocator reservations")
        capacity = 30720 if static or active_queues else 0
        if row["queue_payload_capacity"] != capacity:
            raise ValueError("allocation capacity mismatch")
    # Comparing phases of one binary measures runtime increments only; static
    # stacks/buffers remain present in every phase, even with no active workers.
    baseline = phases[0]
    deltas = {row["stage"]: row["heap_used"] - baseline["heap_used"]
              if row["available"] and baseline["available"] else None for row in phases}
    observations = stack_rows(text, platform, workers, configuration,
                              require_observed=workers > 0)
    return {"result": terminal[0], "phases": phases, "live_heap_delta_from_baseline": deltas,
            "stacks": observations, "stack_summary": stack_summary(observations),
            "note": "Runtime deltas do not remove statically reserved queues/stacks or prove allocator minimums."}
