#!/usr/bin/env python3
"""Validate private control matrices and export sanitized numeric summaries."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import statistics

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control_measure = load(
    "event_services_report_control_measure", HERE / "control_measure.py"
)
legacy_report = load("event_services_legacy_report", HERE / "report.py")
PLATFORMS = ("nuttx-c", "nuttx-rust", "zephyr-c", "embassy")
LAYOUTS = ("one", "three")
CASE_RE = re.compile(r"^(nuttx-c|nuttx-rust|zephyr-c|embassy)-(one|three)$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
TRAFFIC_METRICS = (
    "attempted",
    "accepted",
    "received",
    "rejected",
    "missed",
    "errors",
    "publication_p99_us",
    "publication_max_us",
    "start_p99_us",
    "start_max_us",
    "finish_p99_us",
    "finish_max_us",
    "control_p99_us",
    "control_max_us",
    "queue_p99_us",
    "queue_max_us",
    "worst_service_p99_us",
    "work_jobs",
    "work_p99_us",
    "work_max_us",
    "hal_calls",
    "hal_errors",
)
CONTROL_METRICS = (
    "timer_ms",
    "work_iterations",
    "work_jobs",
    "work_p99_us",
    "work_max_us",
    "hal_calls",
    "hal_errors",
    "diagnostic_bytes",
)


def _int(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _hash(value, name):
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value.lower()


def _median_max(values):
    return {"median": statistics.median(values), "max": max(values)}


def _metadata(entry, case, platform, layout):
    if entry.get("platform") != platform or entry.get("layout") != layout:
        raise ValueError("control matrix platform/layout metadata mismatch")
    artifact_bytes = entry.get("artifact_bytes")
    if not isinstance(artifact_bytes, dict):
        raise ValueError("artifact_bytes metadata is missing")
    clean_bytes = {}
    for name, size in artifact_bytes.items():
        clean_name = legacy_report._relative_source_name(name)
        if "/" in clean_name:
            raise ValueError("artifact byte names cannot contain directories")
        clean_bytes[clean_name] = _int(size, f"artifact_bytes.{clean_name}", 1)
    if not {"image.bin", "app.elf"}.issubset(clean_bytes):
        raise ValueError("image.bin and app.elf sizes are required")
    sections = entry.get("section_accounting")
    if not isinstance(sections, dict):
        raise ValueError("section_accounting metadata is missing")
    configuration = entry.get("configuration")
    if not isinstance(configuration, dict) or configuration.get(
        "publication_timer_resolution_ms"
    ) not in (1, 10):
        raise ValueError("publication timer resolution is missing or unsupported")
    for key in ("loadbearing_flash_bytes", "resident_ram_bytes"):
        _int(sections.get(key), f"section_accounting.{key}")
    return {
        "case": case,
        "platform": platform,
        "layout": layout,
        "image_sha256": _hash(entry.get("image_sha256"), "image_sha256"),
        "elf_sha256": _hash(entry.get("elf_sha256"), "elf_sha256"),
        "artifact_bytes": dict(sorted(clean_bytes.items())),
        "configuration": legacy_report._sanitize_metadata(configuration),
        "kernel_config_identity": (
            _hash(entry["kernel_config_identity"], "kernel_config_identity")
            if entry.get("kernel_config_identity")
            else None
        ),
        "source_sha256": legacy_report._source_hashes(entry.get("source_sha256")),
        "section_accounting": {
            "loadbearing_flash_bytes": sections["loadbearing_flash_bytes"],
            "resident_ram_bytes": sections["resident_ram_bytes"],
            "flash_sections": legacy_report._sanitize_metadata(
                sections.get("flash_sections", [])
            ),
            "resident_ram_sections": legacy_report._sanitize_metadata(
                sections.get("resident_ram_sections", [])
            ),
            "excluded_dummy_padding": legacy_report._sanitize_metadata(
                sections.get("excluded_dummy_padding", [])
            ),
        },
    }


def _safe_measurement_path(root, relative, case, block):
    expected = f"{case}-block-{block:02d}"
    if relative != expected:
        raise ValueError("measurement directory does not match case/block")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("unsafe measurement directory")
    base = root.resolve(strict=True)
    resolved = (root / Path(*path.parts)).resolve(strict=True)
    if not resolved.is_relative_to(base) or not resolved.is_dir():
        raise ValueError("measurement directory escapes matrix output")
    return resolved


def _compare_projection(stored, checked):
    keys = {"profile", "raw_output_sha256", "free_memory"}
    if checked["profile"] == "saturation":
        keys |= {"result", "cycles", "resources"}
    else:
        keys |= {"result", "services", "resources", "memory", "control"}
    expected = {key: checked[key] for key in keys}
    if stored != expected:
        raise ValueError(
            "numeric matrix row disagrees with reparsed private measurement"
        )


def _validate_matrix(path):
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read control matrix: {exc}") from exc
    if (
        not isinstance(record, dict)
        or record.get("schema") != 1
        or record.get("kind") != "event-services-control"
        or record.get("contract_version") != 1
    ):
        raise ValueError("unsupported control matrix schema")
    if (
        record.get("failure") is not None
        or record.get("restore_verified") is not True
        or record.get("restore_error") not in (None, "")
    ):
        raise ValueError("control matrix failed or restore was not verified")
    _hash(record.get("backup_sha256"), "backup_sha256")
    blocks = _int(record.get("blocks"), "blocks", 1)
    runs_per_profile = _int(record.get("runs_per_profile"), "runs_per_profile", 1)
    profiles = record.get("profiles")
    if (
        not isinstance(profiles, list)
        or not profiles
        or any(profile not in control_measure.CONTROL_PROFILES for profile in profiles)
        or len(set(profiles)) != len(profiles)
    ):
        raise ValueError("invalid control profile selection")
    order = record.get("order")
    if not isinstance(order, list) or not order:
        raise ValueError("control matrix contains no completed entries")

    seen = set()
    cases = set()
    entries = []
    for entry in order:
        if not isinstance(entry, dict):
            raise ValueError("control matrix entries must be objects")
        case = entry.get("case")
        match = CASE_RE.fullmatch(case) if isinstance(case, str) else None
        block = _int(entry.get("block"), "block")
        if not match or block >= blocks or (case, block) in seen:
            raise ValueError("invalid or duplicate control case/block")
        platform, layout = match.groups()
        metadata = _metadata(entry, case, platform, layout)
        run_rows = entry.get("runs")
        if not isinstance(run_rows, list) or len(run_rows) != runs_per_profile * len(
            profiles
        ):
            raise ValueError(f"incomplete run rows for {case} block {block}")
        measurement_path = _safe_measurement_path(
            path.parent, entry.get("measurement_dir"), case, block
        )
        try:
            raw_record = json.loads((measurement_path / "measurement.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                "private measurement record is missing or malformed"
            ) from exc
        checked = control_measure.verify_record(
            raw_record,
            image_sha256=metadata["image_sha256"],
            platform=platform,
            layout=layout,
            profiles=profiles,
            runs=runs_per_profile,
        )
        control_measure.validate_build_timer(checked, metadata["configuration"])
        if len(checked) != len(run_rows):
            raise ValueError("numeric and private run counts differ")
        per_profile = {profile: 0 for profile in profiles}
        checked_runs = []
        for stored, raw in zip(run_rows, checked):
            _compare_projection(stored, raw)
            profile = raw["profile"]
            per_profile[profile] += 1
            if profile == "saturation":
                clean = {
                    "profile": profile,
                    "result": raw["result"],
                    "cycles": raw["cycles"],
                    "resources": raw["resources"],
                    "free_memory": raw["free_memory"],
                    "raw_output_sha256": raw["raw_output_sha256"],
                }
            else:
                result = raw["result"]
                control = raw["control"]
                services = raw["services"]
                work_peer0 = services[0]
                nonzero_p99 = max(row["start_p99_us"] for row in services[1:])
                nonzero_queue_p99 = max(row["queue_p99_us"] for row in services[1:])
                qualified = (
                    result["rejected"] == 0
                    and result["missed"] == 0
                    and result["errors"] == 0
                    and control["hal_errors"] == 0
                )
                clean = {
                    "profile": profile,
                    "result": result,
                    "control": control,
                    "services": services,
                    "resources": raw["resources"],
                    "memory": raw["memory"],
                    "free_memory": raw["free_memory"],
                    "raw_output_sha256": raw["raw_output_sha256"],
                    "work_peer0": {
                        "start_p99_us": work_peer0["start_p99_us"],
                        "start_max_us": work_peer0["start_max_us"],
                        "finish_p99_us": work_peer0["finish_p99_us"],
                        "queue_p99_us": work_peer0["queue_p99_us"],
                    },
                    "worst_nonzero_service_start_p99_us": nonzero_p99,
                    "worst_nonzero_service_queue_p99_us": nonzero_queue_p99,
                    "qualified": qualified,
                }
            clean["block"] = block
            clean["run_index"] = per_profile[profile] - 1
            checked_runs.append(clean)
        if any(count != runs_per_profile for count in per_profile.values()):
            raise ValueError(f"incomplete profile count for {case} block {block}")
        entries.append((metadata, block, checked_runs))
        seen.add((case, block))
        cases.add(case)

    if seen != {(case, block) for case in cases for block in range(blocks)}:
        raise ValueError("control matrix is missing one or more case blocks")
    by_case = {}
    for metadata, block, runs in entries:
        current = by_case.setdefault(
            metadata["case"], {**metadata, "blocks": [], "runs": []}
        )
        for key in metadata:
            if current[key] != metadata[key]:
                raise ValueError(f"inconsistent build metadata across blocks: {key}")
        current["blocks"].append(block)
        current["runs"].extend(runs)
    return record, profiles, blocks, runs_per_profile, by_case


def build_report(matrix_path):
    path = Path(matrix_path)
    record, profiles, blocks, runs_per_profile, source_cases = _validate_matrix(path)
    cases_out = {}
    for case in sorted(source_cases):
        source = source_cases[case]
        platform = source["platform"]
        layout = source["layout"]
        section = source["section_accounting"]
        artifact = source["artifact_bytes"]
        rows = source["runs"]
        nfree = [
            row["free_memory"]["maxused"]
            for row in rows
            if row["free_memory"] is not None
        ]
        nfree_required = platform.startswith("nuttx-")
        nfree_missing = (
            sum(row["free_memory"] is None for row in rows) if nfree_required else 0
        )
        whole_ram = (
            [section["resident_ram_bytes"] + value for value in nfree]
            if nfree_required
            else [section["resident_ram_bytes"] for _ in rows]
        )
        nfree_complete = not nfree_required or nfree_missing == 0
        case_out = {
            "platform": platform,
            "layout": layout,
            "image_sha256": source["image_sha256"],
            "elf_sha256": source["elf_sha256"],
            "image_bytes": artifact["image.bin"],
            "elf_bytes": artifact["app.elf"],
            "configuration": source["configuration"],
            "kernel_config_identity": source["kernel_config_identity"],
            "source_sha256": source["source_sha256"],
            "flash_and_ram": {
                "loadbearing_flash_bytes": section["loadbearing_flash_bytes"],
                "image_bytes": artifact["image.bin"],
                "resident_static_ram_bytes": section["resident_ram_bytes"],
                "nfree_maxused_peak_bytes": max(nfree) if nfree else None,
                "nfree_observed_run_count": len(nfree),
                "nfree_unavailable_run_count": nfree_missing,
                "whole_ram_peak_bytes": max(whole_ram)
                if whole_ram and nfree_complete
                else None,
            },
            "blocks": sorted(source["blocks"]),
            "profiles": {},
        }
        for profile in profiles:
            profile_rows = sorted(
                (row for row in rows if row["profile"] == profile),
                key=lambda row: (row["block"], row["run_index"]),
            )
            if profile == "saturation":
                runs_out = []
                all_cycles = []
                for row in profile_rows:
                    cycles = []
                    for cycle in row["cycles"]:
                        derived = {
                            **cycle,
                            "full_minus_empty_heap": cycle["full_heap"]
                            - cycle["empty_heap"],
                            "drained_minus_empty_heap": cycle["drained_heap"]
                            - cycle["empty_heap"],
                        }
                        cycles.append(derived)
                        all_cycles.append(derived)
                    runs_out.append(
                        {
                            "block": row["block"],
                            "run_index": row["run_index"],
                            "result": row["result"],
                            "cycles": cycles,
                            "resources": row["resources"],
                            "free_memory": row["free_memory"],
                            "raw_output_sha256": row["raw_output_sha256"],
                        }
                    )
                heap_fields = (
                    "empty_heap",
                    "full_heap",
                    "drained_heap",
                    "full_minus_empty_heap",
                    "drained_minus_empty_heap",
                )
                heap_allocated = [
                    resource["heap_allocated"]
                    for row in profile_rows
                    for resource in row["resources"]
                    if "heap_allocated" in resource
                ]
                static_resources = [
                    {
                        key: value
                        for key, value in resource.items()
                        if key != "heap_allocated"
                    }
                    for row in profile_rows
                    for resource in row["resources"]
                ]
                case_out["profiles"][profile] = {
                    "run_count": len(runs_out),
                    "qualified_run_count": len(runs_out),
                    "heap_stability": {
                        key: len({cycle[key] for cycle in all_cycles}) <= 1
                        for key in heap_fields
                    },
                    "heap_metrics": {
                        key: _median_max([cycle[key] for cycle in all_cycles])
                        for key in heap_fields
                    },
                    "resource_heap_allocated_metrics": (
                        _median_max(heap_allocated) if heap_allocated else None
                    ),
                    "resource_heap_allocated_stable": (
                        len(set(heap_allocated)) <= 1 if heap_allocated else None
                    ),
                    "static_resources_stable": (
                        len(
                            {
                                json.dumps(row, sort_keys=True)
                                for row in static_resources
                            }
                        )
                        <= 1
                    ),
                    "runs": runs_out,
                }
                continue

            result_values = {
                key: _median_max([row["result"][key] for row in profile_rows])
                for key in TRAFFIC_METRICS
                if key in profile_rows[0]["result"]
            }
            control_values = {
                key: _median_max([row["control"][key] for row in profile_rows])
                for key in CONTROL_METRICS
            }
            peer0_metrics = {
                key: _median_max([row["work_peer0"][key] for row in profile_rows])
                for key in profile_rows[0]["work_peer0"]
            }
            worst_nonzero = {
                "start_p99_us": [
                    row["worst_nonzero_service_start_p99_us"] for row in profile_rows
                ],
                "queue_p99_us": [
                    row["worst_nonzero_service_queue_p99_us"] for row in profile_rows
                ],
            }
            resource_keys = sorted(
                set.intersection(*(set(row["resources"]) for row in profile_rows))
            )
            memory_keys = sorted(
                set.intersection(*(set(row["memory"]) for row in profile_rows))
            )
            resource_metrics = {
                key: _median_max([row["resources"][key] for row in profile_rows])
                for key in resource_keys
                if all(type(row["resources"][key]) is int for row in profile_rows)
            }
            memory_metrics = {
                key: _median_max([row["memory"][key] for row in profile_rows])
                for key in memory_keys
                if all(type(row["memory"][key]) is int for row in profile_rows)
            }
            case_out["profiles"][profile] = {
                "run_count": len(profile_rows),
                "qualified_run_count": sum(row["qualified"] for row in profile_rows),
                "qualification": "zero rejects, misses, protocol errors, and HAL errors",
                "result_metrics": result_values,
                "control_metrics": control_values,
                "resource_metrics": resource_metrics,
                "memory_metrics": memory_metrics,
                "work_peer0_latency": peer0_metrics,
                "worst_nonzero_service_latency": {
                    key: _median_max(values) for key, values in worst_nonzero.items()
                },
                "runs": [
                    {
                        key: row[key]
                        for key in (
                            "block",
                            "run_index",
                            "result",
                            "control",
                            "services",
                            "resources",
                            "memory",
                            "work_peer0",
                            "worst_nonzero_service_start_p99_us",
                            "worst_nonzero_service_queue_p99_us",
                            "qualified",
                            "free_memory",
                            "raw_output_sha256",
                        )
                    }
                    for row in profile_rows
                ],
            }
        cases_out[case] = case_out
    return {
        "schema": 1,
        "kind": "event-services-control-report",
        "backup_sha256": _hash(record["backup_sha256"], "backup_sha256"),
        "profiles": profiles,
        "blocks": blocks,
        "runs_per_profile": runs_per_profile,
        "cases": cases_out,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("--out must name a fresh file")
    try:
        result = build_report(args.matrix)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    args.out.chmod(0o600)
    print(f"EVENT_SERVICES_CONTROL_REPORT_PASS {args.out.name}")


if __name__ == "__main__":
    main()
