#!/usr/bin/env python3
"""Summarize saved labeled diagnostics offline; never opens a device.

Only aggregates and input hashes leave the private capture directory. Already
averaged diagnostic snapshots cannot replay the full-rate feature stream/FSM.
Sensitivity comparisons are counterfactuals, never new accuracy qualification.
"""

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import statistics

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("presence_policy", HERE / "policy.py")
POLICY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(POLICY)
PHASES = ("empty_observe", "person_moving", "person_still", "empty_after")
EMPTY = ("empty_observe", "empty_after")
PRINT_ERROR = 0.5e-8  # The physical witness prints eight fractional digits.


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gain(sensitivity):
    if not math.isfinite(sensitivity) or not 0 <= sensitivity <= 1:
        raise ValueError("sensitivity outside vendor range")
    return 1 + 29 * sensitivity


def comparison(average, threshold):
    """Only claim a strict comparison if printed-value intervals are disjoint."""
    if average - PRINT_ERROR > threshold + PRINT_ERROR:
        return True
    if average + PRINT_ERROR < threshold - PRINT_ERROR:
        return False
    return None


def validate(samples):
    previous_host = previous_native = -1
    if not samples:
        raise ValueError("no classifier diagnostics")
    for sample in samples:
        if (
            sample["host_elapsed_ms"] <= previous_host
            or sample["ms"] <= previous_native
        ):
            raise ValueError("diagnostic clocks did not advance")
        previous_host, previous_native = sample["host_elapsed_ms"], sample["ms"]
        for key in ("wander", "jitter", "average", "threshold"):
            if not math.isfinite(sample[key]) or sample[key] < 0:
                raise ValueError("invalid diagnostic feature")
        for key in ("training", "ready", "calibrated"):
            if sample[key] not in (0, 1):
                raise ValueError("invalid diagnostic prerequisite")
        available = not sample["training"] and sample["ready"] and sample["calibrated"]
        if sample["state"] not in ("unknown", "not_detected", "presence_hint") or (
            (sample["state"] == "unknown") != (not available)
        ):
            raise ValueError("classifier state/prerequisites mismatch")
        if sample["advisory"] != 1 or sample["rf_freshness"] != "not_exported":
            raise ValueError("unqualified diagnostic must stay advisory")


def retained(samples, labels, instructions, name):
    starts = [x["host_elapsed_ms"] for x in labels["transitions"] if x["phase"] == name]
    if len(starts) != 1:
        raise ValueError("missing or ambiguous phase transition")
    stops = [
        x["host_elapsed_ms"]
        for x in instructions
        if x["phase_before_instruction"] == name
    ]
    if name != "empty_after" and not stops:
        raise ValueError("missing next movement instruction")
    start = starts[0] + 2000
    stop = min(stops) - 1000 if stops else math.inf
    selected = [
        x
        for x in samples
        if x["phase"] == name and start <= x["host_elapsed_ms"] <= stop
    ]
    if not selected or any(x["state"] == "unknown" for x in selected):
        raise ValueError("missing calibrated phase evidence")
    return selected


def ranges(samples):
    return {
        key: {
            "min": min(values),
            "median": statistics.median(values),
            "max": max(values),
        }
        for key in ("wander", "jitter", "average", "threshold")
        for values in ([x[key] for x in samples],)
    }


def summarize(phases, baseline, sensitivity):
    if not math.isfinite(baseline) or baseline <= PRINT_ERROR:
        raise ValueError("invalid learned baseline")
    default_gain = gain(sensitivity)
    phase_results = {}
    for name in PHASES:
        samples = phases[name]
        if not samples:
            raise ValueError("missing phase")
        if any(
            abs(x["threshold"] - baseline / default_gain) > 2 * PRINT_ERROR
            for x in samples
        ):
            raise ValueError(
                "diagnostic threshold differs from captured calibration/default"
            )
        decisions = [comparison(x["average"], x["threshold"]) for x in samples]
        mismatches = sum(
            actual is not None and actual != (sample["state"] == "presence_hint")
            for sample, actual in zip(samples, decisions)
        )
        if mismatches:
            raise ValueError("reported presence contradicts diagnostic decision rule")
        phase_results[name] = {
            "reports": len(samples),
            "presence_hints": sum(x["state"] == "presence_hint" for x in samples),
            "first_report_ms": samples[0]["ms"],
            "last_report_ms": samples[-1]["ms"],
            "features": ranges(samples),
            "decision_comparisons_ambiguous_at_print_precision": sum(
                x is None for x in decisions
            ),
            "all_averages_exceed_learned_baseline_even_at_minimum_gain": all(
                comparison(x["average"], baseline) is True for x in samples
            ),
            "minimum_average_over_learned_baseline": min(x["average"] for x in samples)
            / baseline,
            "sensitivity_counterfactuals": [
                {
                    "sensitivity": u,
                    "gain": gain(u),
                    "effective_threshold": baseline / gain(u),
                    "certain_positive_reports": sum(
                        comparison(x["average"], baseline / gain(u)) is True
                        for x in samples
                    ),
                    "ambiguous_reports": sum(
                        comparison(x["average"], baseline / gain(u)) is None
                        for x in samples
                    ),
                }
                for u in (0.0, 0.25, 0.5, 1.0)
            ],
        }
    empty = [x for name in EMPTY for x in phases[name]]
    occupied = [x for name in PHASES if name not in EMPTY for x in phases[name]]
    max_empty = max(x["average"] for x in empty)
    min_occupied = min(x["average"] for x in occupied)
    return {
        "learned_wander_baseline": baseline,
        "default_presence_sensitivity": sensitivity,
        "default_gain": default_gain,
        "default_effective_threshold": baseline / default_gain,
        "supported_effective_threshold_range": [baseline / gain(1), baseline / gain(0)],
        "phases": phase_results,
        "single_threshold_separation": {
            "max_empty_average": max_empty,
            "min_occupied_average": min_occupied,
            "perfect_separation_possible_for_recorded_averages": comparison(
                min_occupied, max_empty
            )
            is True,
            "threshold_at_max_empty_is_outside_supported_sensitivity_range": max_empty
            > baseline,
            "occupied_reports_above_max_empty_by_phase": {
                name: {
                    "reports": len(phases[name]),
                    "certain_positive": sum(
                        comparison(x["average"], max_empty) is True
                        for x in phases[name]
                    ),
                }
                for name in PHASES
                if name not in EMPTY
            },
            "scope": "descriptive same-capture diagnostic overlap; not tuning or held-out accuracy",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--reference-build", required=True, type=Path)
    parser.add_argument("--policy-result", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out = POLICY.BUILD.IDF.COMMON.validate_output(args.out)
    if out.exists():
        parser.error("choose a fresh output directory")
    labels_path = args.capture / "camera-maintenance-result.json"
    samples_path = args.capture / "presence-diagnostics.raw"
    instructions_path = args.capture / "instruction-markers.jsonl"
    labels = json.loads(labels_path.read_text())
    if labels["trial"] != "executed_supervised" or not labels["empty_room_confirmed"]:
        parser.error("requires the completed user-labeled capture")
    records = [
        json.loads(x)
        for x in samples_path.read_text().splitlines()
        if x.startswith("{")
    ]
    samples = [x for x in records if "state" in x]
    validate(samples)
    instructions = [json.loads(x) for x in instructions_path.read_text().splitlines()]
    training = re.findall(
        r"^PRESENCE_TRAIN_STOP ms=(\d+) status=0 wander=([\d.]+) jitter=([\d.]+)$",
        "\n".join(labels["training_events"]),
        re.M,
    )
    if len(training) != 1:
        parser.error("requires one successful calibration")
    baseline = float(training[0][1])
    reference_path = args.reference_build / "reference-build.json"
    config_path = args.reference_build / "sdkconfig"
    receipt = json.loads(reference_path.read_text())
    policy = json.loads(args.policy_result.read_text())
    if (
        receipt["presence_reference"]["source_packages_sha256"] != POLICY.BUILD.PACKAGES
        or policy["package_sha256"] != POLICY.BUILD.PACKAGES
        or receipt["image_sha256"] != labels["image_sha256"]
        or receipt["config_sha256"] != sha(config_path)
        or policy["checks"] != 30
        or len(policy["runs"]) != 2
        or not all(x["passed"] for x in policy["runs"])
    ):
        parser.error("reference/policy provenance mismatch")
    if any(
        sha(ROOT / name) != digest for name, digest in policy["fixture_sha256"].items()
    ):
        parser.error("policy witness source differs from its receipt")
    for name, filename in (
        ("esp-radar", "esp_radar.c"),
        ("esp_wifi_sensing", "esp_wifi_sensing.c"),
    ):
        if (
            policy["source_sha256"][name]
            != receipt["presence_reference"]["source_files_sha256"][
                f"{name}/src/{filename}"
            ]
        ):
            parser.error("policy function source differs from physical reference")
    sensitivity = re.findall(
        r"^CONFIG_ESP_WIFI_SENSING_DEFAULT_PRESENCE_SENSITIVITY=(\d+)$",
        config_path.read_text(),
        re.M,
    )
    if len(sensitivity) != 1:
        parser.error("missing reference sensitivity config")
    phases = {name: retained(samples, labels, instructions, name) for name in PHASES}
    if any(
        sample["ms"] <= int(training[0][0])
        for selected in phases.values()
        for sample in selected
    ):
        parser.error("retained phase precedes completed calibration")
    report = {
        "schema": 1,
        "date": "2026-10-23",
        "experiment": "offline_supervised_presence_calibration_analysis",
        "hardware_accessed": False,
        "thresholds_or_classifier_changed": False,
        "source_image_sha256": receipt["image_sha256"],
        "source_config_sha256": sha(config_path),
        "private_input_sha256": {
            p.name: sha(p) for p in (labels_path, samples_path, instructions_path)
        },
        "analysis_source_sha256": sha(HERE / "analyze.py"),
        "vendor_policy_witness": policy,
        "exclusion_ms": {
            "after_confirmation": 2000,
            "before_next_movement_instruction": 1000,
        },
        "analysis": summarize(phases, baseline, int(sensitivity[0]) / 1000),
        "limitations": [
            "Already averaged 2 Hz diagnostics are not the full-rate feature history or CSI matrices.",
            "Sensitivity comparisons do not rerun the full FSM; they apply its scalar rule to saved averages.",
            "Human-confirmed room labels do not independently map the RF sensing zone or prove RF freshness.",
            "Correlated reports from one trial cannot establish general accuracy or select a production threshold.",
            "The cause of post-training feature drift remains unresolved without bounded RF-derived inputs.",
            "Selected vendor function witnesses use host-local structures, allocation and logging stubs; no target ABI/SDK/FSM task qualification.",
        ],
    }
    out.mkdir()
    (out / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        "PASS: labeled diagnostic consistency, sensitivity bounds and overlap summarized; no new accuracy claim"
    )


if __name__ == "__main__":
    main()
