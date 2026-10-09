#!/usr/bin/env python3
"""Publish compact numerical evidence, excluding private transcripts/paths."""
import argparse
import hashlib
import json
from pathlib import Path

from evidence import kernel_header_identity, require_restored


def public_report(reports):
    if not reports:
        raise ValueError("at least one device report required")
    result = dict(schema=1, target="ESP32-S3 / NuttX", source="messages", period_us=2000,
                  ram_budget_bytes=250000, flash_budget_bytes=2000000,
                  cycles_per_us=240, interrupt_qualified=False, fault_path_review="open",
                  paired_kernel_headers_verified=True, builds={}, runs=[])
    identity = None
    for index, report in enumerate(reports):
        require_restored(report)
        if report["source"] != "messages" or report["period_us"] != 2000:
            raise ValueError("report uses a different source or offered rate")
        builds = report["builds"]
        if any(build.get("diagnostic_trace", False) or build.get("diagnostic_perfmon", False) or
               build.get("diagnostic_layout_padding_bytes") is not None or
               build.get("diagnostic_hot_iram") or build.get("diagnostic_faults") for build in builds.values()):
            raise ValueError("diagnostic image is not an application footprint result")
        if set(builds) != {"c", "rust"} or not report["runs"]:
            raise ValueError("nonempty paired C/Rust report required")
        header = kernel_header_identity(builds)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = public_report([json.loads(path.read_text()) for path in args.report])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print("SERVICE_PUBLIC_REPORT_PASS", args.out)


if __name__ == "__main__":
    main()
