"""Strict inventory and independent arithmetic for production time API output."""
import json

PREFIX = "TIME_SYNC "
SECOND = 1_000_000_000
FIELDS = {
    "begin": "schema variant tick_us",
    "observation": "name request_us reference_ns monotonic_ns wall_ns uncertainty_ns reads max_read_span_ns max_pair_bound_ns",
    "hal_admission": "slew_rate_ppm exclusive pre_epoch",
    "hal_pending": "name request_us elapsed_ns pending_us uncertainty_us cancelled_us",
    "hal_range": "request_us pending_us rejected_unchanged",
    "hal_lifecycle": "reopened drop_preserved set_cleared reference_elapsed_ns",
    "controller": "name state initialized source age_ns bound_ns pending_known pending_us",
    "rejected": "name unchanged",
    "lifecycle": "old_epoch_rejected controller_bytes reference_elapsed_ns",
    "complete": "schema variant reference_elapsed_ns",
}
STRINGS = {"kind", "variant", "name", "state"}
BOOLS = {"exclusive", "pre_epoch", "rejected_unchanged", "reopened", "drop_preserved",
         "set_cleared", "initialized", "pending_known", "unchanged", "old_epoch_rejected"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def analyze(text, variant):
    require(variant in ("baseline", "controller"), "unknown software variant")
    require(not any(marker in text for marker in ("PANIC", "panic:", "panicked at", "Assertion failed")),
            "target reported a kernel/Rust failure")
    rows = [json.loads(line[len(PREFIX):]) for line in text.replace("\r", "").splitlines()
            if line.startswith(PREFIX)]
    require(len(rows) == (3 if variant == "baseline" else 25), "incomplete or duplicated transcript")
    index = 0

    def take(kind, name=None):
        nonlocal index
        row = rows[index]
        index += 1
        require(type(row) is dict and row.get("kind") == kind, f"record {index}: expected {kind}")
        require(set(row) == {"kind", *FIELDS[kind].split()}, f"{kind}: missing/extra field")
        for key, value in row.items():
            expected = str if key in STRINGS else bool if key in BOOLS else int
            require(type(value) is expected, f"{kind}.{key}: wrong type")
        if name is not None:
            require(row["name"] == name, f"{kind}: wrong sequence")
        return row

    observations, states = [], []

    def observation(name, request, duration):
        row = take("observation", name)
        require(row["request_us"] == request, "correction differs from requested public operation")
        elapsed, mono, wall = (row[key] for key in ("reference_ns", "monotonic_ns", "wall_ns"))
        uncertainty, span, bound = (row[key] for key in
                                    ("uncertainty_ns", "max_read_span_ns", "max_pair_bound_ns"))
        require(duration <= elapsed < duration + SECOND // 10, "observation window differs")
        require(row["reads"] >= 2 and 0 < span < elapsed, "missing dense read observations")
        require(200_000 <= uncertainty <= 200_000 + 2 * span, "incoherent endpoint bracket")
        require(200_000 + span <= bound < 500_000, "dense read budget cannot exclude old staircase")
        require(abs(mono - elapsed) <= uncertainty, "std Instant differs from independent counter")
        expected = ((request > 0) - (request < 0)) * min(abs(request) * 1000, elapsed // 2000)
        require(abs(wall - mono - expected) <= uncertainty, "wall correction differs from 500 ppm elapsed budget")
        require(elapsed <= row["reads"] * (bound - 200_000) * 2000, "sample count contradicts interval bound")
        observations.append(row)

    def status(name, state):
        row = take("controller", name)
        require(row["state"] == state and row["initialized"] and row["source"] == 0,
                "controller status/selected source differs")
        require(row["age_ns"] >= 0 and row["bound_ns"] >= -1, "invalid reference evidence")
        if state in ("Synchronized", "Holdover", "Adjusting"):
            require(row["pending_known"] and row["bound_ns"] >= 0, "controller status lacks native/evidence check")
        if state == "Synchronized":
            require(row["pending_us"] == 0 and row["bound_ns"] <= 1_000_000,
                    "synchronized before correction/accuracy was verified")
            require(row["age_ns"] < 6 * SECOND, "stale reference reported synchronized")
        if state == "Holdover":
            require(6 * SECOND <= row["age_ns"] < 12 * SECOND, "holdover reference age differs")
        states.append(row)
        return row

    require(take("begin") == {"kind": "begin", "schema": 1, "variant": variant, "tick_us": 100},
            "wrong schema, variant, or tick")
    observation("baseline", 0, SECOND // 4)
    if variant == "controller":
        require(take("hal_admission") == {"kind": "hal_admission", "slew_rate_ppm": 500,
                                         "exclusive": True, "pre_epoch": True}, "HAL admission failed")
        for name, request in (("positive", 2000), ("negative", -2000)):
            observation(name, request, 5 * SECOND // 4)
            row = take("hal_pending", name)
            require(row["request_us"] == request, "pending request differs")
            elapsed, pending, uncertainty = (row[key] for key in
                                            ("elapsed_ns", "pending_us", "uncertainty_us"))
            require(5 * SECOND // 4 <= elapsed < 2 * SECOND, "pending observation elapsed differs")
            require(1 <= uncertainty < 100, "pending uncertainty hides the correction")
            expected = ((request > 0) - (request < 0)) * max(abs(request) - elapsed // 2_000_000, 0)
            require(abs(pending - expected) <= uncertainty and pending * request > 0,
                    "query differs from 500 ppm elapsed budget")
            require(0 < abs(row["cancelled_us"]) <= abs(pending)
                    and row["cancelled_us"] * request > 0, "cancel residual differs")
        for request in (2_500_123, -2_500_123):
            row = take("hal_range")
            require(row["request_us"] == request and row["rejected_unchanged"]
                    and abs(row["pending_us"] - request) < 10, "signed/range bridge contract failed")
        row = take("hal_lifecycle")
        require(all(row[key] for key in ("reopened", "drop_preserved", "set_cleared")), "HAL lifecycle failed")
        require(0 < row["reference_elapsed_ns"] < 10 * SECOND, "HAL reference lifetime differs")
        status("seed", "Checking")
        status("seed_checked", "Synchronized")
        for name, sign, settled in (("controller_positive", 1, "positive_settled"),
                                    ("controller_negative", -1, "negative_settled")):
            if sign < 0:
                before = status("before_replacement", "Adjusting")
                require(1000 <= before["pending_us"] <= 1800, "replacement did not encounter an active correction")
            row = status(name, "Adjusting")
            request = row["pending_us"]
            require(1800 <= abs(request) <= 2200 and request * sign > 0, "controller chose wrong slew")
            require(row["bound_ns"] >= abs(request) * 1000 - 100_000, "accuracy bound excludes unapplied correction")
            observation(name, request, 9 * SECOND // 2)
            status(settled, "Synchronized")
        require(take("rejected", "reordered")["unchanged"], "reordered observation mutated time")
        status("holdover", "Holdover")
        status("refreshed", "Synchronized")
        status("resumed", "Synchronized")
        row = take("lifecycle")
        require(row["old_epoch_rejected"] and 0 < row["controller_bytes"] <= 4096,
                "continuity lifecycle or bounded controller layout differs")
        require(0 < row["reference_elapsed_ns"] < 60 * SECOND, "controller reference lifetime differs")
    complete = take("complete")
    require(complete["schema"] == 1 and complete["variant"] == variant, "completion identity differs")
    require(sum(row["reference_ns"] for row in observations) <= complete["reference_elapsed_ns"] < 60 * SECOND,
            "completion lifetime differs")
    return {"qualified": True, "variant": variant, "observations": observations,
            "controller_states": states, "complete": complete,
            "limits": ["Explicit instruction-driven QEMU model; no physical RTC/sleep qualification.",
                       "Dense sample extrema and API failure checks are enforced by target Rust assertions.",
                       "Source observations are synthetic policy inputs; GNSS/NTP source quality is not measured."]}
