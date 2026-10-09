#!/usr/bin/env python3
"""Export timing-only diagnostic evidence; never export private transcripts."""
import argparse
import hashlib
import json
from pathlib import Path

from evidence import kernel_header_identity, require_restored


def public_trace(report):
    require_restored(report)
    if not report.get("diagnostic_trace") or report["source"] != "messages":
        raise ValueError("successful, restored message-source diagnostic required")
    builds = report["builds"]
    if set(builds) != {"c", "rust"} or not report["runs"]:
        raise ValueError("paired nonempty diagnostic required")
    header = kernel_header_identity(builds)
    for field in ("config_identity", "kernel_archives", "c_compiler_sha256", "c_flags", "thread_stack"):
        if builds["c"][field] != builds["rust"][field]:
            raise ValueError("paired input mismatch: " + field)
    result = dict(schema=1, target="ESP32-S3 / NuttX", diagnostic_only=True,
                  source="messages", period_us=report["period_us"], cycles_per_us=240,
                  footprint_claim=False, restoration_verified=True,
                  paired_kernel_headers_verified=True, builds={}, runs=[])
    result["rust_worker"] = report.get("rust_worker")
    result["rust_entry"] = report.get("rust_entry")
    for language, build in builds.items():
        if not build.get("diagnostic_trace"):
            raise ValueError("firmware lacks diagnostic wrappers")
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in build["source_sha256"]):
            raise ValueError("non-repository source path")
        public = dict(artifacts=build["artifacts"], source_sha256=build["source_sha256"],
                      diagnostic_worker_switch=build.get("diagnostic_worker_switch", False),
                      diagnostic_entry_switch=build.get("diagnostic_entry_switch", False),
                      config_identity=build["config_identity"], c_flags=build["c_flags"],
                      c_compiler_sha256=build["c_compiler_sha256"],
                      kernel_header_sha256=header,
                      kernel_archive_inventory_sha256=hashlib.sha256(
                          json.dumps(build["kernel_archives"], sort_keys=True).encode()).hexdigest())
        proof = build["compiler_input"]
        if proof:
            public["rust_input_sha256"] = proof["input_sha256"]
            public["compiler_driver_sha256"] = proof["compiler_driver_sha256"]
            public["llvm_revision"] = proof["patch_ledger"]["upstream_revision"]
            public["rust_revision"] = proof["patch_ledger"]["rust_revision"]
        result["builds"][language] = public
    for run in report["runs"]:
        if not run.get("instrumentation") or run["result"]["errors"] or run["done"]["status"]:
            raise ValueError("failed or uninstrumented run")
        if builds[run["language"]].get("diagnostic_worker_switch") and not run.get("worker_selection_verified"):
            raise ValueError("same-image worker selection is not proven")
        if builds[run["language"]].get("diagnostic_entry_switch") and not run.get("entry_selection_verified"):
            raise ValueError("same-image entry selection is not proven")
        rows = run["trace_rows"]
        count, events = run["result"]["services"], run["result"]["events"]
        if (run["result"]["received"] != events or len(rows) != count or
                [row["id"] for row in rows] != list(range(count)) or
                any(row["events"] != events or row["errors"] for row in rows)):
            raise ValueError("incomplete diagnostic delivery")
        led = count - 2
        if sum(row["transit_cycles"] + row["worker_cycles"] for row in rows[:led + 1]) != rows[led]["end_cycles"]:
            raise ValueError("diagnostic path does not close")
        denominator = events * 240
        path_us = dict(
            transit=sum(row["transit_cycles"] for row in rows[:led + 1]) / denominator,
            worker_excluding_led=sum(row["worker_cycles"] - row["led_cycles"] for row in rows[:led + 1]) / denominator,
            led=sum(row["led_cycles"] for row in rows[:led + 1]) / denominator,
            end=rows[led]["end_cycles"] / denominator,
        )
        # Raw sums remain authoritative; rounded averages are only presentation.
        result["runs"].append(dict(language=run["language"], block=run["block"], services=count,
                                    service_loop_language=run.get("service_loop_language", run["language"]),
                                    entry_language=run.get("entry_language", run["language"]),
                                    worker_selection_verified=run.get("worker_selection_verified", False),
                                    entry_selection_verified=run.get("entry_selection_verified", False),
                                    events=events, trace_rows=rows, path_us=path_us,
                                    runtime_record_mean_us=run["mean_us"],
                                    misses_1ms=run["result"]["misses_1ms"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = public_trace(json.loads(args.report.read_text()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print("SERVICE_TRACE_REPORT_PASS", args.out)


if __name__ == "__main__":
    main()
