#!/usr/bin/env python3
"""Publish compact numerical evidence, excluding private transcripts/paths."""
import argparse
import hashlib
import json
import math
from pathlib import Path

from evidence import kernel_header_identity, psram_identity, require_restored
from results import validate_memory


def public_report(reports, *, period_us=2000):
    if not reports:
        raise ValueError("at least one device report required")
    if type(period_us) is not int or not 100 <= period_us <= 100000:
        raise ValueError("invalid requested pacing")
    result = dict(schema=1, target="ESP32-S3 / NuttX", source="messages", period_us=period_us,
                  ram_budget_bytes=250000, flash_budget_bytes=2000000,
                  cycles_per_us=240, interrupt_qualified=False, fault_path_review="open",
                  paired_kernel_headers_verified=True, builds={}, runs=[])
    identity = None
    for index, report in enumerate(reports):
        require_restored(report)
        if report["source"] != "messages" or report["period_us"] != period_us:
            raise ValueError("report uses a different source or offered rate")
        builds = report["builds"]
        if any(build.get("diagnostic_trace", False) or build.get("diagnostic_perfmon", False) or
               build.get("diagnostic_layout_padding_bytes") is not None or
               build.get("diagnostic_hot_iram") or build.get("diagnostic_faults") or
               build.get("diagnostic_pressure") for build in builds.values()):
            raise ValueError("diagnostic image is not an application footprint result")
        if set(builds) != {"c", "rust"} or not report["runs"]:
            raise ValueError("nonempty paired C/Rust report required")
        header = kernel_header_identity(builds)
        psram = psram_identity(builds)
        for field in ("config_identity", "kernel_archives", "c_flags", "c_compiler_sha256", "thread_stack"):
            if builds["c"][field] != builds["rust"][field]:
                raise ValueError("C/Rust build input mismatch: " + field)
        common = hashlib.sha256(json.dumps(builds["c"]["kernel_archives"], sort_keys=True).encode()).hexdigest()
        if identity is not None and identity != common:
            raise ValueError("kernel differs between measurements")
        identity = common
        for language, build in builds.items():
            # Source inventories are public repository paths, never host paths.
            if any(Path(name).is_absolute() or ".." in Path(name).parts
                   for name in build["source_sha256"]):
                raise ValueError("source inventory contains a private/non-repository path")
            public = dict(binary_bytes=build["binary_bytes"],
                          code_initialized_data_bytes=build["accounting"]["loadbearing_flash_bytes"],
                          resident_ram_bytes=build["accounting"]["resident_ram_bytes"],
                          config_identity=build["config_identity"], kernel_archive_inventory_sha256=common,
                          kernel_header_sha256=header,
                          artifacts=build["artifacts"], source_sha256=build["source_sha256"],
                          c_compiler_sha256=build["c_compiler_sha256"], c_flags=build["c_flags"],
                          thread_stack_bytes=build["thread_stack"])
            if psram is not None:
                public["psram_enabled"] = psram
            proof = build["compiler_input"]
            if proof:
                public["compiler"] = dict(
                    llvm_revision=proof["patch_ledger"]["upstream_revision"],
                    rust_revision=proof["patch_ledger"]["rust_revision"],
                    patches=[dict(name=p["name"], sha256=p["sha256"]) for p in proof["patch_ledger"]["patches"]],
                    driver_sha256=proof["compiler_driver_sha256"], target_sha256=proof["target_sha256"],
                    std_inventory_sha256=proof["std_inventory_sha256"], std_features=proof["std_features"],
                    input_sha256=proof["input_sha256"], package_sha256=proof["compiler_package_sha256"])
            if language in result["builds"] and result["builds"][language] != public:
                raise ValueError("firmware differs between measurements")
            result["builds"][language] = public
        for run in report["runs"]:
            row = run["result"]
            if (run["language"] not in builds or row["source"] != "messages" or
                    row["errors"] or run["done"]["status"] or row["received"] != row["events"]):
                raise ValueError("device result failed")
            peak = builds[run["language"]]["accounting"]["resident_ram_bytes"] + max(
                run["memory"]["full"], run["nsh_after"]["maxused"])
            result["runs"].append(dict(
                matrix=index, block=run["block"], language=run["language"], services=row["services"],
                events=row["events"], received=row["received"], mean_cycles=row["mean_cycles"],
                max_cycles=row["max_cycles"], misses_1ms=row["misses_1ms"],
                full_capacity_ram_bytes=run["full_capacity_ram_bytes"], observed_peak_ram_bytes=peak,
                memory=run["memory"], nsh_used_before=run["nsh_before"]["used"],
                nsh_used_after=run["nsh_after"]["used"]))
    return result


def sustained_report(report):
    """Separate faster-paced cohort; never relabel a pacing request as a rate.

    Wall time includes app setup/teardown and serial command completion, not
    just message processing. This firmware does not expose retry/occupancy
    counts, so sustained delivery is not evidence of sustained overload.
    """
    if (type(report.get("blocks")) is not int or report["blocks"] < 2 or
            report.get("services") != [20] or type(report.get("events")) is not int or
            not 20000 <= report["events"] <= 100000 or
            type(report.get("recovery_events")) is not int or
            not 100 <= report["recovery_events"] <= 100000):
        raise ValueError("bounded paired sustained/recovery matrix required")
    expected = [(block, language) for block in range(report["blocks"])
                for language in (("c", "rust") if block % 2 == 0 else ("rust", "c"))]
    if [(run["block"], run["language"]) for run in report["runs"]] != expected:
        raise ValueError("incomplete, duplicated or reordered sustained matrix")
    public = public_report([report], period_us=100)
    public.update(profile="sustained-delivery-and-recovery", blocks=report["blocks"],
                  recovery_period_us=2000, same_boot_recovery=True, same_process=False,
                  overload_qualified=False, retry_counts_available=False,
                  command_wall_time_includes_setup_teardown_and_serial=True,
                  restoration_verified=True, harness_sha256=report["harness_sha256"])
    if any(Path(name).is_absolute() or ".." in Path(name).parts
           for name in public["harness_sha256"]):
        raise ValueError("private harness inventory path")
    for private, row in zip(report["runs"], public["runs"]):
        recovery = private.get("recovery")
        if recovery is None or recovery.get("period_us") != 2000:
            raise ValueError("normal-rate recovery missing")
        for run, events in ((private, report["events"]), (recovery, report["recovery_events"])):
            result = run["result"]
            if (result["services"] != 20 or result["queues"] != 60 or
                    result["events"] != events or result["received"] != events or
                    result["source"] != "messages" or result["errors"] != 0 or
                    run["done"]["status"] != 0 or
                    not 0 <= result["mean_cycles"] <= result["max_cycles"] or
                    result["max_cycles"] <= 0 or not 0 <= result["misses_1ms"] <= events):
                raise ValueError("sustained/recovery delivery failed")
            validate_memory(run["memory"], 20)
            duration = run.get("command_wall_seconds")
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
                raise ValueError("positive finite command wall time required")
        if recovery["nsh_before"] != private["nsh_after"]:
            raise ValueError("recovery is not linked to post-load heap sample")
        row.update(command_wall_seconds=private["command_wall_seconds"],
                   recovery=dict(events=recovery["result"]["events"], received=recovery["result"]["received"],
                                 mean_cycles=recovery["result"]["mean_cycles"],
                                 max_cycles=recovery["result"]["max_cycles"],
                                 misses_1ms=recovery["result"]["misses_1ms"],
                                 command_wall_seconds=recovery["command_wall_seconds"],
                                 memory=recovery["memory"],
                                 nsh_used_after=recovery["nsh_after"]["used"],
                                 post_load_heap_change_bytes=(recovery["nsh_after"]["used"] -
                                                              private["nsh_after"]["used"]),
                                 post_load_allocation_change=(recovery["nsh_after"]["nused"] -
                                                              private["nsh_after"]["nused"])))
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sustained", action="store_true",
                        help="publish the separate 20-service faster-paced/recovery cohort")
    args = parser.parse_args()
    reports = [json.loads(path.read_text()) for path in args.report]
    if args.sustained and len(reports) != 1:
        parser.error("one sustained capture report required")
    result = sustained_report(reports[0]) if args.sustained else public_report(reports)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print("SERVICE_PUBLIC_REPORT_PASS", args.out)


if __name__ == "__main__":
    main()
