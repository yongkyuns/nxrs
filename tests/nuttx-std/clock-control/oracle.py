#!/usr/bin/env python3
"""Analyze clock observations without calibrating away an OS rate error.

A complete characterization can fail qualification. Missing/corrupt evidence
raises ValueError; an observed backend defect is retained in the result.
"""
import json

PREFIX = "CLOCK_PROBE "
SECOND = 1_000_000_000
PHASES = {"baseline": 500, "positive": 5000, "negative": 5000,
          "before_replace": 1200, "replaced": 5000,
          "before_cancel": 1200, "cancelled": 2000}
BASE = [("set", "seed"), ("set", "forward"), ("set", "backward"),
        ("sleep", "baseline_sleep"), ("phase", "baseline"),
        ("adjust", "initial_query")]
SOFTWARE = [("set", "positive_seed"), ("adjust", "positive"),
            ("sleep", "positive_sleep"), ("phase", "positive"),
            ("adjust", "positive_query"), ("set", "negative_seed"),
            ("adjust", "negative"), ("sleep", "negative_sleep"),
            ("phase", "negative"), ("adjust", "negative_query"),
            ("set", "replace_seed"), ("adjust", "replace_initial"),
            ("phase", "before_replace"), ("adjust", "replacement"),
            ("phase", "replaced"), ("set", "cancel_seed"),
            ("adjust", "cancel_initial"), ("phase", "before_cancel"),
            ("adjust", "cancel"), ("phase", "cancelled"),
            ("adjust", "final_query")]
DELTAS = {"positive": 2000, "negative": -2000, "replace_initial": 20000,
          "replacement": -2000, "cancel_initial": 20000, "cancel": 0}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(record, name, minimum=None):
    value = record.get(name)
    require(type(value) is int, f"invalid integer: {name}")
    require(minimum is None or value >= minimum, f"out of range: {name}")
    return value


def difference(before, after, clock):
    return after[clock + "_ns"] - before[clock + "_ns"]


def offset(sample):
    return sample["realtime_ns"] - sample["monotonic_ns"]


def analyze(text, mode, tick_us):
    require(mode in ("baseline", "software"), "unknown mode")
    records = [json.loads(line[len(PREFIX):]) for line in text.replace("\r", "").splitlines()
               if line.startswith(PREFIX)]
    require(len(records) >= 2 and all(type(item) is dict for item in records),
            "missing probe records")
    begin, complete = records[0], records[-1]
    require(begin.get("kind") == "begin" and complete.get("kind") == "complete",
            "missing begin/complete boundary")
    require(begin.get("schema") == complete.get("schema") == 1, "schema mismatch")
    expected_features = {"timekeeping": mode == "software", "adjtime_header": False,
                         "rtc": False, "rtc_hires": False,
                         "direct_implementation_only": mode == "software"}
    require(all(begin.get(key) is value for key, value in expected_features.items()),
            "unexpected clock capabilities")
    require(begin.get("tick_us") == tick_us and begin.get("reference_hz") == 20_000_000,
            "clock identity mismatch")
    integer(begin, "reported_realtime_resolution_ns", 1)
    elapsed = integer(complete, "reference_elapsed_ns", 1)
    require(elapsed < 60 * SECOND, "reference wrap guard exceeded")
    expected = BASE + (SOFTWARE if mode == "software" else [])
    operations = [item for item in records[1:-1] if item.get("kind") != "sample"]
    require([(item.get("kind"), item.get("name")) for item in operations] == expected,
            "operation inventory/order mismatch")
    expected_phases = {name: PHASES[name] for kind, name in expected if kind == "phase"}
    require(complete.get("phases") == len(expected_phases), "phase count mismatch")

    groups = [[] for _ in expected]
    next_operation = 0
    for item in records[1:-1]:
        if item.get("kind") != "sample":
            next_operation += 1
            continue
        require(next_operation < len(expected) and item.get("phase") == expected[next_operation][1],
                "sample/operation order mismatch")
        for key in ("realtime_ns", "monotonic_ns"):
            integer(item, key)
        for key in ("reference_ns", "read_span_ns", "index"):
            integer(item, key, 0)
        require(item["reference_ns"] <= elapsed, "sample exceeds reference lifetime")
        require(item["read_span_ns"] <= item["reference_ns"] * 2, "invalid read bracket")
        groups[next_operation].append(item)

    phases, steps, sleeps, adjustments = {}, {}, {}, {}
    monotonic_continuous = True
    monotonic_mutations_stable = True
    wall_non_decreasing = True
    last_reference = 0
    previous = None
    for item, samples in zip(operations, groups):
        kind, name = item["kind"], item["name"]
        require(bool(samples), f"missing samples: {name}")
        if kind != "phase":
            require([s.get("point") for s in samples] == ["before", "after"]
                    and all(s["index"] == 0 for s in samples), f"invalid pair: {name}")
            first, last = samples
        else:
            regression = [s for s in samples if s.get("point", "").startswith("regression_")]
            ordinary = [s for s in samples if s not in regression]
            require(ordinary[0].get("point") == "start"
                    and ordinary[-1].get("point") == "end", f"missing phase boundary: {name}")
            checkpoints = ordinary[1:-1]
            require(all(s.get("point") == "checkpoint" and s["index"] == i
                        for i, s in enumerate(checkpoints)), f"invalid checkpoints: {name}")
            require(1 <= len(checkpoints) <= PHASES[name] // 250, "checkpoint count mismatch")
            first, last = ordinary[0], ordinary[-1]
            require(all(s["index"] == 0 for s in [first, last, *regression]), "invalid boundary index")
            require(all(a["reference_ns"] <= b["reference_ns"]
                        for a, b in zip(ordinary, ordinary[1:])), "reference regressed")
            require(item.get("duration_ms") == PHASES[name], "phase duration mismatch")
            integer(item, "reads", len(ordinary))
            backwards = integer(item, "realtime_regressions", 0)
            mono_backwards = integer(item, "monotonic_regressions", 0)
            minimum = integer(item, "min_realtime_delta_ns")
            for key in ("min_positive_realtime_delta_ns", "min_positive_monotonic_delta_ns",
                        "max_read_span_ns"):
                integer(item, key, 0)
            require(max(backwards, mono_backwards) <= item["reads"], "regressions exceed reads")
            require(item["max_read_span_ns"] >= max(s["read_span_ns"] for s in samples),
                    "read span exceeds reported maximum")
            require(bool(backwards) == (minimum < 0), "inconsistent regression count")
            if backwards:
                require([s.get("point") for s in regression] ==
                        ["regression_before", "regression_after"], "missing backward pair")
                require(first["reference_ns"] <= regression[0]["reference_ns"]
                        <= regression[1]["reference_ns"] <= last["reference_ns"]
                        and difference(*regression, "realtime") < 0, "invalid backward pair")
            else:
                require(not regression, "unexpected backward pair")
            duration = difference(first, last, "reference")
            require(duration >= PHASES[name] * 1_000_000, "short observation interval")
            monotonic_continuous &= mono_backwards == 0 and all(
                difference(a, b, "monotonic") >= 0 for a, b in zip(ordinary, ordinary[1:]))
            wall_non_decreasing &= all(
                difference(a, b, "realtime") >= 0 for a, b in zip(ordinary, ordinary[1:]))
            phases[name] = {
                "reference_elapsed_ns": duration, "reads": item["reads"],
                "realtime_rate": difference(first, last, "realtime") / duration,
                "monotonic_rate": difference(first, last, "monotonic") / duration,
                "realtime_regressions": backwards, "monotonic_regressions": mono_backwards,
                "min_realtime_delta_ns": minimum,
                "observed_realtime_resolution_ns": item["min_positive_realtime_delta_ns"],
                "observed_monotonic_resolution_ns": item["min_positive_monotonic_delta_ns"],
                "max_read_span_ns": item["max_read_span_ns"],
                "wall_minus_monotonic_change_ns": offset(last) - offset(first),
                "endpoint_uncertainty_ns": tick_us * 1000 + first["read_span_ns"] + last["read_span_ns"],
                "correction_trace": [{"elapsed_ns": s["reference_ns"] - first["reference_ns"],
                                      "change_ns": offset(s) - offset(first)} for s in ordinary],
            }
        require(last_reference <= first["reference_ns"] <= last["reference_ns"],
                "operation reference order mismatch")
        last_reference = last["reference_ns"]
        monotonic_continuous &= difference(first, last, "monotonic") >= 0
        if previous is not None:
            monotonic_continuous &= difference(previous, first, "monotonic") >= 0
            wall_non_decreasing &= difference(previous, first, "realtime") >= 0
        pairs = [(first, last)] if kind in ("set", "adjust") else []
        if previous is not None:
            pairs.append((previous, first))
        for a, b in pairs:
            reference_delta = difference(a, b, "reference")
            tolerance = tick_us * 2000 + a["read_span_ns"] + b["read_span_ns"] + reference_delta * .01
            monotonic_mutations_stable &= abs(difference(a, b, "monotonic") - reference_delta) <= tolerance
        if kind != "set":
            wall_non_decreasing &= difference(first, last, "realtime") >= 0
        previous = last
        if kind == "set":
            integer(item, "target_ns", 1)
            require(item.get("result") == 0 and item.get("errno") == 0, "set failed")
            window = difference(first, last, "reference") + first["read_span_ns"] + last["read_span_ns"]
            error = last["realtime_ns"] - item["target_ns"]
            steps[name] = {"target_error_ns": error,
                           "within_read_window": -tick_us * 1000 <= error <= window + tick_us * 2000,
                           "monotonic_elapsed_ns": difference(first, last, "monotonic"),
                           "reference_elapsed_ns": difference(first, last, "reference")}
        elif kind == "sleep":
            require(item.get("requested_ns") == 100_000_000, "sleep request mismatch")
            sleeps[name] = {"requested_ns": item["requested_ns"],
                            "reference_elapsed_ns": difference(first, last, "reference"),
                            "monotonic_elapsed_ns": difference(first, last, "monotonic")}
        elif kind == "adjust":
            for key in ("result", "errno", "old_seconds", "old_microseconds", "delta_us"):
                integer(item, key)
            require(item.get("has_delta") is (name in DELTAS)
                    and item["delta_us"] == DELTAS.get(name, 0), "adjustment mismatch")
            adjustments[name] = {key: value for key, value in item.items() if key not in ("kind", "name")}

    gates = {
        "explicit_steps": all(item["within_read_window"] for item in steps.values()),
        "realtime_baseline_rate_within_one_percent": abs(phases["baseline"]["realtime_rate"] - 1) < .01,
        "monotonic_rate_within_one_percent": all(abs(p["monotonic_rate"] - 1) < .01 for p in phases.values()),
        "monotonic_non_decreasing": monotonic_continuous,
        "monotonic_stable_across_clock_controls": monotonic_mutations_stable,
        "no_unrequested_realtime_regression": wall_non_decreasing and all(
            p["realtime_regressions"] == 0 for p in phases.values()),
        "sleep_reference_within_ten_percent": all(
            abs(s["reference_elapsed_ns"] - s["requested_ns"]) <= s["requested_ns"] / 10 + tick_us * 2000
            for s in sleeps.values()),
        "public_adjustment_api": begin["adjtime_header"],
        "pending_query": all(a["result"] == 0 for name, a in adjustments.items() if name.endswith("query")),
        "complete_old_delta": all(a["old_seconds"] != 1234567 and abs(a["old_microseconds"]) < 1_000_000
                                  for a in adjustments.values() if a["result"] == 0)
                              and any(a["result"] == 0 for a in adjustments.values()),
    }
    # This gate checks only endpoint observations at their reported resolution.
    # It does not turn coarse checkpoints into a proof of continuous slewing.
    if mode == "software":
        gates["adjustment_requests_succeeded"] = all(adjustments[name]["result"] == 0 for name in DELTAS)
        gates["correction_endpoints_within_resolution"] = all(
            abs(phases[name]["wall_minus_monotonic_change_ns"] - requested) <=
            phases[name]["endpoint_uncertainty_ns"]
            and (requested == 0 or phases[name]["endpoint_uncertainty_ns"] < abs(requested))
            for name, requested in (("positive", 2_000_000), ("negative", -2_000_000),
                                    ("replaced", -2_000_000), ("cancelled", 0)))
    return {"characterization_complete": True, "qualified": all(gates.values()),
            "gates": gates, "failed_gates": [name for name, ok in gates.items() if not ok],
            "features": begin, "phases": phases, "steps": steps,
            "sleeps": sleeps, "adjustments": adjustments,
            "limits": ["Emulated APB reference; no physical RTC or deep-sleep evidence.",
                       "100 ms sleeps need not overlap an applied adjustment.",
                       "Checkpoints and endpoint resolution do not prove continuous slew rate.",
                       "Firmware hashes, not the optional in-band build_id, bind capture identity."]}
