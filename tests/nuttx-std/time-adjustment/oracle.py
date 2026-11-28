"""Independent arithmetic and inventory checks for TIME_ADJUSTMENT observations."""
import json

PREFIX = "TIME_ADJUSTMENT "
SECOND = 1_000_000_000
PHASES = [("baseline", 0), ("positive", 2000), ("negative", -2000),
          ("near_second", -2000), ("replacement", -2000), ("cancelled", 0)]
WAITS = [("positive", False, 2000), ("negative", False, -2000),
         ("forward", True, 3 * SECOND), ("backward", True, -3 * SECOND)]
FIELDS = {
    "begin": "schema features tick_us",
    "phase": "name request_us reference_ns monotonic_ns wall_ns uncertainty_ns reads "
             "realtime_regressions monotonic_regressions min_realtime_delta_ns "
             "max_read_span_ns pending_us slew_discontinuities max_slew_excess_ns "
             "max_slew_uncertainty_ns",
    "pending": "phase index elapsed_ns remaining_us uncertainty_us",
    "large": "request_us pending_us cancelled_us",
    "set_clears_adjustment": "pending_us",
    "wait": "name step action requested_ns reference_ns monotonic_ns wall_ns uncertainty_ns "
            "action_offset_ns action_completed_offset_ns request_offset_ns request_span_ns "
            "pending_elapsed_ns pending_uncertainty_us pending_us",
    "complete": "schema phases waits large reference_elapsed_ns",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sign(value):
    return (value > 0) - (value < 0)


def pending(request, elapsed_ns, remaining, uncertainty_us):
    require(elapsed_ns >= 0 and uncertainty_us >= 1, "invalid pending observation bracket")
    # 500 ppm consumes one microsecond per 2,000,000 reference nanoseconds.
    expected = sign(request) * max(abs(request) - elapsed_ns // 2_000_000, 0)
    require(remaining == 0 or sign(remaining) == sign(request), "pending sign differs")
    require(abs(remaining) <= abs(request), "pending exceeds original request")
    if request:
        require(uncertainty_us * 2 < min(abs(request), 500),
                "pending uncertainty hides one second of correction")
    require(abs(remaining - expected) <= uncertainty_us, "pending differs from 500 ppm budget")
    return expected


def analyze(text, tick_us):
    require(type(tick_us) is int and tick_us > 0, "invalid tick")
    rows = [json.loads(line[len(PREFIX):]) for line in text.replace("\r", "").splitlines()
            if line.startswith(PREFIX)]
    require(len(rows) == 39, "expected begin, six phases/24 checkpoints, two large, clear, four waits, complete")
    index = 0

    def take(kind):
        nonlocal index
        row = rows[index]
        index += 1
        require(type(row) is dict and row.get("kind") == kind, f"record {index}: expected {kind}")
        require(set(row) == {"kind", *FIELDS[kind].split()}, f"{kind}: missing or extra fields")
        for key, value in row.items():
            expected = str if key in ("kind", "name", "phase") else bool if key == "step" else int
            require(type(value) is expected, f"{kind}.{key}: wrong type")
        return row

    begin = take("begin")
    require(begin == {"kind": "begin", "schema": 1, "features": 3, "tick_us": tick_us},
            "wrong schema, public software features, or tick")
    tick_ns = tick_us * 1000
    phases, checkpoints, waits, large = {}, [], {}, []
    for name, delta in PHASES:
        row = take("phase")
        require(row["name"] == name and row["request_us"] == delta, "phase inventory/order differs")
        reference, mono, wall = (row[key] for key in ("reference_ns", "monotonic_ns", "wall_ns"))
        uncertainty, span = row["uncertainty_ns"], row["max_read_span_ns"]
        require(4_500_000_000 <= reference < 5 * SECOND, f"{name}: unequal observation window")
        require(row["reads"] >= 2 and 0 < span < reference, f"{name}: missing dense observations")
        require(2 * tick_ns <= uncertainty <= 2 * tick_ns + 2 * span, "incoherent endpoint bracket")
        require(row["realtime_regressions"] == row["monotonic_regressions"] == 0,
                f"{name}: backward clock reads")
        require(row["min_realtime_delta_ns"] >= 0 and
                row["min_realtime_delta_ns"] * row["reads"] <= wall, "invalid minimum increment")
        require(row["slew_discontinuities"] == row["max_slew_excess_ns"] == 0,
                f"{name}: correction discontinuity")
        maximum = row["max_slew_uncertainty_ns"]
        require(2 * tick_ns + span <= maximum < 500_000,
                "dense correction bound cannot exclude the old 500 us staircase")
        require(reference <= row["reads"] * (maximum - 2 * tick_ns) * 2000,
                "dense sample count contradicts its maximum interval budget")
        require(row["pending_us"] == 0, f"{name}: adjustment did not finish/cancel")
        require(abs(mono - reference) <= uncertainty, f"{name}: Instant/APB divergence")
        if delta:
            require(uncertainty < abs(delta) * 500, "endpoint cannot distinguish correction")
        require(abs(wall - mono - delta * 1000) <= uncertainty, f"{name}: wrong applied correction")
        phases[name] = row
        previous = 0
        for ordinal in range(4):
            point = take("pending")
            require(point["phase"] == name and point["index"] == ordinal, "checkpoint inventory/order differs")
            elapsed = point["elapsed_ns"]
            require((ordinal + 1) * SECOND <= elapsed < (ordinal + 2) * SECOND and elapsed > previous,
                    "checkpoint elapsed time is unordered or outside its interval")
            expected = pending(delta, elapsed, point["remaining_us"], point["uncertainty_us"])
            checkpoints.append({**point, "expected_remaining_us": expected})
            previous = elapsed
    for request in (2_500_123, -2_500_123):
        row = take("large")
        require(row["request_us"] == request, "large request inventory/order differs")
        value, cancelled = row["pending_us"], row["cancelled_us"]
        require(sign(value) == sign(cancelled) == sign(request), "large residual sign differs")
        # This row omits query elapsed time. Only the full 60 s lifetime bounds
        # initial progress; the target independently checks its measured budget.
        require(0 <= abs(request) - abs(value) <= 30_000 and 0 <= abs(value) - abs(cancelled) <= 2,
                "large residual lost seconds/fraction or cancellation replaced its value")
        large.append(row)
    require(take("set_clears_adjustment")["pending_us"] == 0, "explicit set retained pending correction")
    for name, step, action in WAITS:
        row = take("wait")
        require((row["name"], row["step"], row["action"]) == (name, step, action), "wait inventory/order differs")
        requested = 250_000_000 if step else 1_250_000_000
        reference, mono, wall = (row[key] for key in ("reference_ns", "monotonic_ns", "wall_ns"))
        uncertainty, span = row["uncertainty_ns"], row["request_span_ns"]
        start, end = row["action_offset_ns"], row["action_completed_offset_ns"]
        require(row["requested_ns"] == requested, "wrong sleep duration")
        require(0 < start <= end < reference and 0 <= span <= end - start,
                "clock mutation did not complete strictly inside the wait")
        require(2 * tick_ns <= uncertainty <= requested // 50 and reference + tick_ns >= requested,
                "wait returned before its tick allowance")
        require(reference <= requested + requested // 50 + uncertainty,
                "wait exceeded declared 2 percent scheduling tolerance")
        require(abs(mono - reference) <= uncertainty, "wait Instant/APB divergence")
        if step:
            require(all(row[key] == 0 for key in ("request_offset_ns", "pending_elapsed_ns",
                    "pending_uncertainty_us", "pending_us")), "step retained adjustment state")
            correction = action
            require(uncertainty + span < abs(correction), "step is smaller than observation uncertainty")
        else:
            offset = row["request_offset_ns"]
            require(start <= offset <= end, "request epoch escaped control bracket")
            require(row["pending_elapsed_ns"] >= reference - offset, "query preceded wait endpoint")
            pending(action, row["pending_elapsed_ns"], row["pending_us"], row["pending_uncertainty_us"])
            require(sign(row["pending_us"]) == sign(action) and abs(row["pending_us"]) < abs(action) - 500,
                    "wait did not overlap unfinished correction")
            correction = sign(action) * min((reference - offset) // 2000, abs(action) * 1000)
            require(uncertainty + span < abs(correction), "wait correction is smaller than uncertainty")
        require(abs(wall - mono - correction) <= uncertainty + span, "wrong wall correction during wait")
        waits[name] = {**row, "expected_applied_ns": correction}
    complete = take("complete")
    require((complete["schema"], complete["phases"], complete["waits"], complete["large"]) == (1, 6, 4, 2),
            "wrong completion inventory")
    observed = sum(row["reference_ns"] for row in phases.values()) + sum(row["reference_ns"] for row in waits.values())
    require(observed <= complete["reference_elapsed_ns"] < 60 * SECOND, "invalid reference lifetime")
    return {"characterization_complete": True, "qualified": True, "begin": begin, "phases": phases,
            "pending": checkpoints, "large": large, "waits": waits, "complete": complete,
            "limits": ["Instruction-driven QEMU time; no physical RTC or sleep-retention qualification.",
                       "Dense extrema are target aggregates; individual samples are not in the transcript.",
                       "Public-call success, timeval normalization and paired control-call continuity are target assertions.",
                       "Large-query elapsed time is not logged; its precise rate check is a target assertion."]}
