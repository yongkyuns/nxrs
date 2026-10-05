#!/usr/bin/env python3
"""Summarize a successful event-services matrix without exposing serial logs."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import statistics

PROFILES = ("normal", "burst")
EXPECTED_ATTEMPTS = {"normal": 3900, "burst": 6240}
CASE_RE = re.compile(r"^(nuttx-c|nuttx-rust|zephyr-c|embassy)-(one|three)$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAC_RE = re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b")
LOCAL_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:dev|Users|private|var|tmp|home|Volumes)/[^\s,;]+")
WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*")
FREE_MEMORY_KEYS = ("total", "used", "free", "maxused", "maxfree", "nused", "nfree")
RESULT_NUMERIC = (
    "services", "queues", "event_bytes", "slots", "attempted", "accepted",
    "received", "rejected", "errors", "missed", "publication_p99_us",
    "publication_max_us", "start_p99_us", "start_max_us", "finish_p99_us",
    "finish_max_us", "control_p99_us", "control_max_us", "queue_p99_us",
    "queue_max_us", "worst_service_p99_us",
    "queue_peak_observed", "last_finish_us", "delivery_ok", "capacity_ok",
    "deadlines_ok",
)
SERVICE_NUMERIC = (
    "id", "received", "start_p99_us", "start_max_us", "finish_p99_us",
    "control_p99_us", "queue_p99_us", "queue_max_us", "missed_control",
    "missed_data", "missed_status", "rejected",
)


def _integer(value, label, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _sha256(value, label):
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return value.lower()


def _relative_source_name(name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("source hash names must be relative POSIX paths")
    path = PurePosixPath(name)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", name):
        raise ValueError("absolute source paths cannot be exported")
    return path.as_posix()


def _sanitize_metadata(value):
    """Keep structured build metadata while redacting accidental local identifiers."""
    if isinstance(value, str):
        value = MAC_RE.sub("[redacted-mac]", value)
        value = LOCAL_PATH_RE.sub("[redacted-path]", value)
        return WINDOWS_PATH_RE.sub("[redacted-path]", value)
    if isinstance(value, list):
        return [_sanitize_metadata(item) for item in value]
    if isinstance(value, dict):
        return {_sanitize_metadata(str(key)): _sanitize_metadata(item)
                for key, item in value.items()}
    if value is None or type(value) in (bool, int, float):
        return value
    raise ValueError("metadata contains a non-JSON value")


def _source_hashes(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("source_sha256 must be a nonempty mapping")
    output = {}
    for name, digest in value.items():
        safe_name = _relative_source_name(name)
        if safe_name in output:
            raise ValueError("duplicate sanitized source hash name")
        output[safe_name] = _sha256(digest, f"source_sha256[{safe_name}]")
    return dict(sorted(output.items()))


def _validate_free_memory(value, platform):
    if value is None:
        return None
    if platform not in ("nuttx-c", "nuttx-rust"):
        raise ValueError("non-NuttX runs must not include free_memory snapshots")
    if not isinstance(value, dict) or set(value) != set(FREE_MEMORY_KEYS):
        raise ValueError("NuttX free_memory snapshot is missing or incomplete")
    for key in FREE_MEMORY_KEYS:
        _integer(value[key], f"free_memory.{key}")
    if (value["used"] > value["total"] or value["free"] > value["total"] or
            value["used"] + value["free"] != value["total"] or
            value["maxused"] > value["total"] or value["maxfree"] > value["total"]):
        raise ValueError("NuttX free_memory snapshot is inconsistent")
    return dict(value)


def _validate_run(row, platform, layout, profile):
    if not isinstance(row, dict) or row.get("profile") != profile:
        raise ValueError("run profile does not match matrix profile")
    result = row.get("result")
    if not isinstance(result, dict) or result.get("platform") != platform or result.get("profile") != profile:
        raise ValueError("run platform/profile result marker mismatch")
    for key in RESULT_NUMERIC:
        _integer(result.get(key), f"result.{key}")
    fixed = {"services": 20, "queues": 20 if layout == "one" else 60,
             "event_bytes": 64, "slots": 480}
    if any(result[key] != value for key, value in fixed.items()):
        raise ValueError("run topology does not match case layout")
    for p99, maximum in (("publication_p99_us", "publication_max_us"),
                         ("start_p99_us", "start_max_us"),
                         ("finish_p99_us", "finish_max_us"),
                         ("control_p99_us", "control_max_us"),
                         ("queue_p99_us", "queue_max_us")):
        if result[p99] > result[maximum]:
            raise ValueError(f"{p99} exceeds {maximum}")
    if result["accepted"] + result["rejected"] != result["attempted"]:
        raise ValueError("accepted and rejected counts do not sum to attempted")
    if result["attempted"] != EXPECTED_ATTEMPTS[profile]:
        raise ValueError("profile attempted-event count mismatch")
    if result["received"] != result["accepted"]:
        raise ValueError("received count does not equal accepted count")

    services = row.get("services")
    if not isinstance(services, list) or len(services) != 20:
        raise ValueError("each run must preserve all 20 service rows")
    service_rows = {}
    for service in services:
        if not isinstance(service, dict):
            raise ValueError("service row must be an object")
        for key in SERVICE_NUMERIC:
            _integer(service.get(key), f"service.{key}")
        service_id = service["id"]
        if service_id >= 20 or service_id in service_rows:
            raise ValueError("service IDs must be unique values from 0 through 19")
        if service["start_p99_us"] > service["start_max_us"]:
            raise ValueError("service start p99 exceeds maximum")
        if service["queue_p99_us"] > service["queue_max_us"]:
            raise ValueError("service queue p99 exceeds maximum")
        service_rows[service_id] = dict(service)
    if set(service_rows) != set(range(20)):
        raise ValueError("service IDs must be exactly 0 through 19")
    ordered_services = [service_rows[index] for index in range(20)]
    if sum(service["received"] for service in ordered_services) != result["received"]:
        raise ValueError("per-service received counts do not sum to result")
    if sum(service["rejected"] for service in ordered_services) != result["rejected"]:
        raise ValueError("per-service rejected counts do not sum to result")
    missed = sum(service["missed_control"] + service["missed_data"] +
                 service["missed_status"] for service in ordered_services)
    if missed != result["missed"]:
        raise ValueError("per-service deadline misses do not sum to result")
    if (result["errors"] != 0 or result["delivery_ok"] != 1 or
            result["capacity_ok"] != int(result["rejected"] == 0) or
            result["deadlines_ok"] != int(result["missed"] == 0)):
        raise ValueError("run status flags disagree with event counts")
    if result["queue_peak_observed"] > (24 if layout == "one" else 8):
        raise ValueError("observed queue peak exceeds the selected layout capacity")
    if result["worst_service_p99_us"] != max(s["start_p99_us"] for s in ordered_services):
        raise ValueError("worst service p99 does not match per-service rows")

    resources = row.get("resources")
    memory = row.get("memory")
    if not isinstance(resources, dict) or not isinstance(memory, dict):
        raise ValueError("run resources and memory rows are required")
    for key, value in resources.items():
        if key not in ("note", "heap_note"):
            _integer(value, f"resources.{key}")
    for key in ("application_state", "diagnostics", "heap_before", "heap_live"):
        _integer(memory.get(key), f"memory.{key}")
    if resources.get("queue_buffers") != 30720:
        raise ValueError("run queue buffer allocation must be 30,720 bytes")
    free_memory = _validate_free_memory(row.get("free_memory"), platform)
    raw_hash = _sha256(row.get("raw_output_sha256"), "raw_output_sha256")

    # Copy only parsed numeric rows and the output digest; raw serial and device
    # material are deliberately not part of the exported representation.
    safe_result = {key: value for key, value in result.items()
                   if type(value) is int or key in ("platform", "profile")}
    safe_services = [{key: value for key, value in service.items()
                      if type(value) is int} for service in ordered_services]
    safe_resources = {key: value for key, value in resources.items()
                      if type(value) is int}
    safe_memory = {key: value for key, value in memory.items()
                   if type(value) is int}
    return {
        "profile": profile,
        "result": safe_result,
        "services": safe_services,
        "resources": safe_resources,
        "memory": safe_memory,
        "free_memory": free_memory,
        "raw_output_sha256": raw_hash,
    }


def _numeric_summary(rows, keys):
    summary = {}
    for key in keys:
        values = [row[key] for row in rows]
        summary[key] = {"median": statistics.median(values), "max": max(values)}
    return summary


def _validate_build_metadata(entry, platform, layout):
    if entry.get("platform") != platform or entry.get("layout") != layout:
        raise ValueError("matrix case platform/layout metadata mismatch")
    image_hash = _sha256(entry.get("image_sha256"), "image_sha256")
    elf_hash = _sha256(entry.get("elf_sha256"), "elf_sha256")
    source_hashes = _source_hashes(entry.get("source_sha256"))
    artifact_bytes = entry.get("artifact_bytes")
    if not isinstance(artifact_bytes, dict):
        raise ValueError("artifact_bytes must be a mapping")
    safe_artifact_bytes = {}
    for name, size in artifact_bytes.items():
        safe_name = _relative_source_name(name)
        if "/" in safe_name:
            raise ValueError("artifact byte names must not contain directory paths")
        safe_artifact_bytes[safe_name] = _integer(size, f"artifact_bytes.{safe_name}", minimum=1)
    if "image.bin" not in safe_artifact_bytes or "app.elf" not in safe_artifact_bytes:
        raise ValueError("image.bin and app.elf byte sizes are required")
    sections = entry.get("section_accounting")
    if not isinstance(sections, dict):
        raise ValueError("section_accounting is required")
    for key in ("loadbearing_flash_bytes", "resident_ram_bytes"):
        _integer(sections.get(key), f"section_accounting.{key}")
    for key in ("flash_sections", "resident_ram_sections", "excluded_dummy_padding"):
        if not isinstance(sections.get(key), list):
            raise ValueError(f"section_accounting.{key} must be a list")
    for section in sections["excluded_dummy_padding"]:
        if (not isinstance(section, dict) or not isinstance(section.get("name"), str)):
            raise ValueError("excluded section rows must include a name")
        _integer(section.get("size"), "excluded_dummy_padding.size")
    if not isinstance(entry.get("configuration"), dict):
        raise ValueError("configuration metadata is required")
    kernel_identity = entry.get("kernel_config_identity")
    if kernel_identity is not None:
        kernel_identity = _sha256(kernel_identity, "kernel_config_identity")
    return {
        "case": entry["case"],
        "platform": platform,
        "layout": layout,
        "image_sha256": image_hash,
        "elf_sha256": elf_hash,
        "artifact_bytes": dict(sorted(safe_artifact_bytes.items())),
        "source_sha256": source_hashes,
        "configuration": _sanitize_metadata(entry["configuration"]),
        "kernel_config_identity": kernel_identity,
        "resolved_configuration": _sanitize_metadata(entry.get("resolved_configuration")),
        "section_accounting": {
            "loadbearing_flash_bytes": sections["loadbearing_flash_bytes"],
            "resident_ram_bytes": sections["resident_ram_bytes"],
            "flash_sections": _sanitize_metadata(sections["flash_sections"]),
            "resident_ram_sections": _sanitize_metadata(sections["resident_ram_sections"]),
            "excluded_dummy_padding": _sanitize_metadata(sections["excluded_dummy_padding"]),
        },
    }


def _matrix_entries(record):
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise ValueError("unsupported matrix schema")
    if ("failure" not in record or record.get("failure") is not None or
            record.get("restore_verified") is not True or \
            record.get("restore_error") not in (None, "")):
        raise ValueError("matrix must be successful and restore_verified=true")
    blocks = _integer(record.get("blocks"), "matrix.blocks", minimum=1)
    runs_per_profile = _integer(record.get("runs_per_profile"),
                                 "matrix.runs_per_profile", minimum=1)
    profiles = record.get("profiles")
    if (not isinstance(profiles, list) or not profiles or
            any(not isinstance(p, str) or p not in PROFILES for p in profiles) or
            len(set(profiles)) != len(profiles)):
        raise ValueError("report matrices accept distinct normal/burst profiles only")
    order = record.get("order")
    if not isinstance(order, list) or not order:
        raise ValueError("matrix order must contain completed case blocks")
    cases = set()
    seen = set()
    entries = []
    for entry in order:
        if not isinstance(entry, dict):
            raise ValueError("matrix order entries must be objects")
        case = entry.get("case")
        match = CASE_RE.fullmatch(case) if isinstance(case, str) else None
        block = _integer(entry.get("block"), "matrix block")
        if not match or block >= blocks or (case, block) in seen:
            raise ValueError("matrix order contains an invalid or duplicate case block")
        platform, layout = match.groups()
        metadata = _validate_build_metadata(entry, platform, layout)
        runs = entry.get("runs")
        if not isinstance(runs, list) or len(runs) != runs_per_profile * len(profiles):
            raise ValueError(f"incomplete run count for {case} block {block}")
        per_profile = {profile: 0 for profile in profiles}
        checked_runs = []
        for row in runs:
            if not isinstance(row, dict) or row.get("profile") not in per_profile:
                raise ValueError("matrix run has an unrequested profile")
            profile = row["profile"]
            per_profile[profile] += 1
            run = _validate_run(row, platform, layout, profile)
            run.update(block=block, run_index=per_profile[profile] - 1)
            checked_runs.append(run)
        if any(count != runs_per_profile for count in per_profile.values()):
            raise ValueError(f"incomplete profile count for {case} block {block}")
        entries.append((metadata, block, checked_runs))
        seen.add((case, block))
        cases.add(case)
    expected = {(case, block) for case in cases for block in range(blocks)}
    if seen != expected:
        raise ValueError("matrix is missing one or more case blocks")
    return entries, profiles, blocks, runs_per_profile, sorted(cases)


def _consistent_metadata(previous, current):
    for key in ("platform", "layout", "image_sha256", "elf_sha256", "artifact_bytes",
                "source_sha256", "configuration", "kernel_config_identity",
                "resolved_configuration", "section_accounting"):
        if previous[key] != current[key]:
            raise ValueError(f"inconsistent build metadata across blocks: {key}")


def build_report(matrix_path):
    """Validate one matrix and produce compact summaries plus every numeric run row."""
    matrix_path = Path(matrix_path)
    try:
        record = json.loads(matrix_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read matrix JSON: {exc}") from exc
    entries, profiles, blocks, runs_per_profile, cases = _matrix_entries(record)
    case_data = {}
    for metadata, block, runs in entries:
        case = metadata["case"]
        if case not in case_data:
            case_data[case] = {**metadata, "blocks": [], "profiles": {}}
        else:
            _consistent_metadata(case_data[case], metadata)
        case_data[case]["blocks"].append(block)
        for run in runs:
            case_data[case]["profiles"].setdefault(run["profile"], []).append(run)

    cases_out = {}
    for case in cases:
        source = case_data[case]
        platform = source["platform"]
        resident = source["section_accounting"]["resident_ram_bytes"]
        image_bytes = source["artifact_bytes"]["image.bin"]
        case_out = {
            "platform": platform,
            "layout": source["layout"],
            "image_sha256": source["image_sha256"],
            "elf_sha256": source["elf_sha256"],
            "image_bytes": image_bytes,
            "elf_bytes": source["artifact_bytes"]["app.elf"],
            "configuration": source["configuration"],
            "kernel_config_identity": source["kernel_config_identity"],
            "resolved_configuration": source["resolved_configuration"],
            "source_sha256": source["source_sha256"],
            "section_accounting": source["section_accounting"],
            "blocks": sorted(source["blocks"]),
            "profiles": {},
        }
        for profile in profiles:
            runs = sorted(source["profiles"][profile],
                          key=lambda row: (row["block"], row["run_index"]))
            results = [row["result"] for row in runs]
            services = [row["services"] for row in runs]
            resources = [row["resources"] for row in runs]
            memories = [row["memory"] for row in runs]
            service_summary = []
            for service_id in range(20):
                service_runs = [rows[service_id] for rows in services]
                service_summary.append({
                    "id": service_id,
                    "metrics": _numeric_summary(service_runs, SERVICE_NUMERIC[1:]),
                })
            free_maxused = [row["free_memory"]["maxused"] for row in runs
                            if row["free_memory"] is not None]
            whole_ram = ([resident + value for value in free_maxused]
                         if platform in ("nuttx-c", "nuttx-rust") else [resident] * len(runs))
            whole_ram_complete = (len(free_maxused) == len(runs)
                                  if platform in ("nuttx-c", "nuttx-rust") else True)
            ledger = {
                "loadbearing_flash_bytes": source["section_accounting"]["loadbearing_flash_bytes"],
                "resident_ram_bytes": resident,
                "image_bytes": image_bytes,
                "whole_ram_footprint_bytes": ({"median": statistics.median(whole_ram),
                                                "max": max(whole_ram)}
                                               if whole_ram and whole_ram_complete else None),
                "free_memory_maxused_bytes": ({"median": statistics.median(free_maxused),
                                                "max": max(free_maxused)}
                                               if free_maxused else None),
                "free_memory_observed_run_count": len(free_maxused),
                "whole_ram_unavailable_run_count": (
                    len(runs) - len(free_maxused)
                    if platform in ("nuttx-c", "nuttx-rust") else 0),
                "arena_accounting": ("resident ELF sections only; static arena is already included"
                                     if platform in ("zephyr-c", "embassy") else
                                     "resident ELF sections plus per-run free_memory.maxused"),
            }
            case_out["profiles"][profile] = {
                "run_count": len(runs),
                "qualified_run_count": sum(
                    row["result"]["rejected"] == 0 and row["result"]["missed"] == 0
                    for row in runs),
                "qualification": "zero rejected and zero missed events per run",
                "result_metrics": _numeric_summary(results, RESULT_NUMERIC),
                "service_metrics": service_summary,
                "resource_metrics": _numeric_summary(
                    resources, sorted(set.intersection(*(set(row) for row in resources)) -
                                      {"note", "heap_note"})),
                "memory_metrics": _numeric_summary(
                    memories, sorted(set.intersection(*(set(row) for row in memories)))),
                "resource_ledger": ledger,
                "runs": runs,
            }
        cases_out[case] = case_out

    return {
        "schema": 1,
        "input": {"backup_sha256": _sha256(record.get("backup_sha256"), "backup_sha256"),
                  "restore_verified": True, "blocks": blocks,
                  "runs_per_profile_per_block": runs_per_profile,
                  "profiles": list(profiles), "case_count": len(cases)},
        "method": {
            "aggregation": "median and maximum of per-run numeric observations",
            "quantiles": "p99 values are summarized across reported per-run p99s; "
                         "no pooled p99 or pooled quantile is inferred",
            "queue_latency": "posted-to-handler-start wake latency; reported separately "
                            "from release-to-handler-start, which includes publication "
                            "timing and 100 Hz tick quantization",
            "raw_serial": "omitted; raw-output SHA-256 digests are retained",
            "privacy": "no device paths, MAC addresses or raw serial text are exported",
        },
        "cases": cases_out,
    }


def write_report(matrix_path, output_path):
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    report = build_report(matrix_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    output_path.chmod(0o600)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True, type=Path,
                        help="successful matrix.json produced by run_matrix.py")
    parser.add_argument("--out", required=True, type=Path,
                        help="fresh path for the private sanitized JSON report")
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error("--out must name a fresh file")
    try:
        write_report(args.matrix, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"EVENT_SERVICES_REPORT_PASS {args.out}")


if __name__ == "__main__":
    main()
