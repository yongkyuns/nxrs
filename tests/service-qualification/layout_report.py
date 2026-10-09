#!/usr/bin/env python3
"""Export restored, untraced layout/counter experiments, without private data."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from build import digest
from evidence import kernel_header_identity, require_restored
from perfmon_results import MODES

SYMBOLS = ("nxrs_sq_worker", "nxrs_sq_wait", "nxrs_sq_run", "poll", "mq_send",
           "mq_receive", "es_platform_cycles", "nxrs_sq_layout_padding", "_stext",
           "__wrap_nxrs_sq_ready", "__wrap_nxrs_cq_thread_join")
HOT_SYMBOLS = ("nxrs_sq_worker", "nxrs_sq_wait", "poll", "mq_send", "mq_receive")


def addresses(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-1] in SYMBOLS:
            if fields[-1] in result:
                raise ValueError("duplicate diagnostic symbol")
            result[fields[-1]] = int(fields[0], 16)
    if any(name not in result for name in HOT_SYMBOLS):
        raise ValueError("incomplete hot-path symbol evidence")
    return result


def public_build(record, folder, prefix):
    for name, expected in record["artifacts"].items():
        if digest(folder / name) != expected:
            raise ValueError("changed firmware artifact")
    symbols = addresses(subprocess.check_output([prefix + "nm", "--defined-only", folder / "app.elf"], text=True))
    pad = record.get("diagnostic_layout_padding_bytes")
    if pad is not None and symbols.get("nxrs_sq_layout_padding") != symbols.get("_stext"):
        raise ValueError("padding is not at the beginning of flash instructions")
    if record.get("diagnostic_hot_iram") and any(not 0x40370000 <= symbols[name] < 0x403e0000 for name in HOT_SYMBOLS):
        raise ValueError("hot path did not reach IRAM")
    if record.get("diagnostic_perfmon") and any(not 0x40370000 <= symbols.get(name, 0) < 0x403e0000
        for name in ("__wrap_nxrs_sq_ready", "__wrap_nxrs_cq_thread_join")):
        raise ValueError("counter wrappers did not reach IRAM")
    result = dict(artifacts=record["artifacts"], source_sha256=record["source_sha256"],
                  function_addresses=symbols, c_flags=record["c_flags"],
                  thread_stack_bytes=record["thread_stack"],
                  diagnostic_layout_padding_bytes=pad,
                  diagnostic_hot_iram=record.get("diagnostic_hot_iram", False),
                  diagnostic_perfmon=record.get("diagnostic_perfmon", False),
                  diagnostic_linker_script=record.get("diagnostic_linker_script"),
                  kernel_header_sha256=record.get("kernel_header_sha256"),
                  config_identity=record["config_identity"],
                  kernel_archive_inventory_sha256=hashlib.sha256(
                      json.dumps(record["kernel_archives"], sort_keys=True).encode()).hexdigest())
    proof = record["compiler_input"]
    if proof:
        result.update(rust_input_sha256=proof["input_sha256"],
                      compiler_driver_sha256=proof["compiler_driver_sha256"])
    return result


def public_cases(cases, prefix):
    result = dict(schema=1, target="ESP32-S3 / NuttX", source="messages", period_us=2000,
                  cycles_per_us=240, diagnostic_only=True, footprint_claim=False,
                  restoration_verified=True, cases=[])
    common = None
    labels = set()
    for label, path, c, rust in cases:
        if label in labels:
            raise ValueError("duplicate experiment label")
        labels.add(label)
        report = json.loads(Path(path).read_text())
        require_restored(report)
        if (report["source"] != "messages" or report["period_us"] != 2000 or
                report.get("diagnostic_trace") or not report["runs"]):
            raise ValueError("successful, restored, untraced message experiment required")
        builds = report["builds"]
        kernel_header_identity(builds)
        for field in ("config_identity", "kernel_archives", "c_flags", "c_compiler_sha256", "thread_stack"):
            if builds["c"][field] != builds["rust"][field]:
                raise ValueError("unmatched experiment inputs: " + field)
        identity = {field: builds["c"][field] for field in
                    ("config_identity", "kernel_archives", "kernel_header_sha256", "c_flags",
                     "c_compiler_sha256", "thread_stack")}
        if common is not None and common != identity:
            raise ValueError("kernel or native compiler differs between experiments")
        common = identity
        public = dict(label=label, paired_kernel_headers_verified=True,
                      builds={language: public_build(builds[language], Path(folder), prefix)
                                          for language, folder in (("c", c), ("rust", rust))}, runs=[])
        for run in report["runs"]:
            row = run["result"]
            if row["errors"] or run["done"]["status"] or row["received"] != row["events"]:
                raise ValueError("unsuccessful delivery")
            values = {key: row[key] for key in ("services", "events", "mean_cycles", "max_cycles", "misses_1ms")}
            values.update(language=run["language"], block=run["block"])
            if builds[run["language"]].get("diagnostic_perfmon"):
                pm = run["perfmon"]
                if pm["overflow"] or (pm["select0"], pm["mask0"]) != MODES[pm["mode"]] or (pm["select1"], pm["mask1"]) != (5, 32):
                    raise ValueError("invalid hardware counter evidence")
                values["perfmon"] = pm
            public["runs"].append(values)
        result["cases"].append(public)
    if not result["cases"]:
        raise ValueError("nonempty experiments required")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", nargs=4, action="append", required=True,
                        metavar=("LABEL", "REPORT", "C_BUILD", "RUST_BUILD"))
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = public_cases(args.case, args.prefix)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print("SERVICE_LAYOUT_REPORT_PASS", args.out)


if __name__ == "__main__":
    main()
