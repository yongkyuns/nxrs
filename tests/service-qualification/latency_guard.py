#!/usr/bin/env python3
"""Bounded regression screen for normal public reports; final firmware still needs measurement.

A separate benchmark can hide code-layout effects, so measure the actual final image
before drawing conclusions about its latency.
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path


MIN_SAMPLES = 3
META = ("target", "source", "period_us", "cycles_per_us")
BUILD_ID = ("config_identity", "kernel_archive_inventory_sha256")


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite numeric data")
    return value


def validate_report(report, label):
    if not isinstance(report, dict) or any(key not in report for key in META + ("builds", "runs")):
        raise ValueError(f"{label}: incomplete public report")
    if report.get("diagnostic_only") or report.get("footprint_claim") is False:
        raise ValueError(f"{label}: diagnostic/non-footprint report")
    if report.get("failure") is not None or report.get("restoration_verified") is False:
        raise ValueError(f"{label}: failed/unrestored report")
    for key in ("instrumentation", "diagnostic_trace", "diagnostic_perfmon", "diagnostic_hot_iram"):
        if report.get(key):
            raise ValueError(f"{label}: diagnostic report ({key})")
    if report.get("diagnostic_layout_padding_bytes") is not None:
        raise ValueError(f"{label}: diagnostic layout report")
    if report["source"] != "messages" or number(report["period_us"], "period_us") <= 0 or \
            number(report["cycles_per_us"], "cycles_per_us") <= 0:
        raise ValueError(f"{label}: unsupported source or timing metadata")
    builds = report["builds"]
    if not isinstance(builds, dict) or not builds:
        raise ValueError(f"{label}: missing builds")
    for language, build in builds.items():
        if not isinstance(build, dict) or any(not build.get(k) for k in BUILD_ID):
            raise ValueError(f"{label}: missing {language} build identity")
        for key in ("diagnostic_trace", "diagnostic_perfmon", "diagnostic_hot_iram", "instrumentation"):
            if build.get(key):
                raise ValueError(f"{label}: diagnostic build ({key})")
        if build.get("diagnostic_layout_padding_bytes") is not None:
            raise ValueError(f"{label}: diagnostic layout build")
    if not isinstance(report["runs"], list) or not report["runs"]:
        raise ValueError(f"{label}: missing runs")
    cells = {}
    for row in report["runs"]:
        if not isinstance(row, dict) or any(k in row for k in ("trace_rows", "path_us", "instrumentation")):
            raise ValueError(f"{label}: diagnostic run")
        if row.get("diagnostic_trace") or row.get("diagnostic_perfmon") or row.get("diagnostic_hot_iram"):
            raise ValueError(f"{label}: diagnostic run")
        if row.get("diagnostic_layout_padding_bytes") is not None:
            raise ValueError(f"{label}: diagnostic layout run")
        lang = row.get("language")
        if lang not in builds or row.get("failed") or row.get("status") not in (None, "ok", "pass"):
            raise ValueError(f"{label}: failed or unknown run")
        if row.get("errors") not in (None, 0, [], False) or row.get("received") != row.get("events"):
            raise ValueError(f"{label}: failed result")
        services, events = row.get("services"), row.get("events")
        matrix, block = row.get("matrix"), row.get("block")
        if (not isinstance(lang, str) or any(isinstance(v, bool) or not isinstance(v, int)
                for v in (services, events, matrix, block)) or min(services, events) <= 0 or
                min(matrix, block) < 0):
            raise ValueError(f"{label}: incomplete run identity")
        key, identity = (lang, services, events), (matrix, block)
        value = number(row.get("mean_cycles"), "mean_cycles")
        if value < 0:
            raise ValueError(f"{label}: negative mean_cycles")
        samples, identities = cells.setdefault(key, ([], set()))
        if identity in identities:
            raise ValueError(f"{label}: duplicate sample identity for {key}")
        identities.add(identity)
        samples.append(value / report["cycles_per_us"])
    return cells


def compare(baseline, candidate, relative_limit_percent=20.0, absolute_limit_us=5.0):
    relative = number(relative_limit_percent, "relative_limit_percent")
    absolute = number(absolute_limit_us, "absolute_limit_us")
    if relative < 0 or absolute < 0:
        raise ValueError("latency limits must be nonnegative")
    base_cells = validate_report(baseline, "baseline")
    candidate_cells = validate_report(candidate, "candidate")
    for field in META:
        if baseline[field] != candidate[field]:
            raise ValueError(f"report metadata mismatch: {field}")
    for lang in {key[0] for key in candidate_cells}:
        if lang not in baseline["builds"] or lang not in candidate["builds"]:
            raise ValueError(f"missing {lang} build identity")
        for field in BUILD_ID:
            if baseline["builds"][lang][field] != candidate["builds"][lang][field]:
                raise ValueError(f"{lang} build mismatch: {field}")
    cells = []
    for key in sorted(candidate_cells, key=lambda x: (x[0], x[1], x[2])):
        if key not in base_cells:
            raise ValueError(f"candidate cell has no baseline: {key}")
        if len(candidate_cells[key][0]) < MIN_SAMPLES or len(base_cells[key][0]) < MIN_SAMPLES:
            raise ValueError(f"cell {key} requires at least {MIN_SAMPLES} baseline and candidate samples")
        before = statistics.median(base_cells[key][0])
        after = statistics.median(candidate_cells[key][0])
        allowed = max(absolute, before * relative / 100.0)
        cells.append(dict(language=key[0], services=key[1], events=key[2],
                          baseline_median_us=before, candidate_median_us=after,
                          added_us=after - before, allowed_added_us=allowed,
                          budget_us=before + allowed, **{"pass": after <= before + allowed}))
    return {"pass": all(cell["pass"] for cell in cells), "compared_cells": len(cells), "cells": cells}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--relative-limit-percent", type=float, default=20.0)
    parser.add_argument("--absolute-limit-us", type=float, default=5.0)
    args = parser.parse_args(argv)
    try:
        result = compare(json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text()),
                         args.relative_limit_percent, args.absolute_limit_us)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"latency guard: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
