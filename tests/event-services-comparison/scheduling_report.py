#!/usr/bin/env python3
"""Reparse private scheduling measurements and export sanitized summaries."""
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


control = load("event_services_scheduling_report_control", HERE / "control_measure.py")
shared_report = load("event_services_scheduling_report_shared", HERE / "report.py")
scheduling = load("event_services_scheduling_report_matrix", HERE / "scheduling_matrix.py")
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
COUNTS = ("attempted", "accepted", "received", "rejected", "missed")


def _hash(value, label):
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return value.lower()


def _summary(values):
    return {"median": statistics.median(values), "max": max(values)}


def _require_case_metadata_identity(previous, current):
    for key, label in (("image_sha256", "image_sha256"), ("elf_sha256", "elf_sha256"),
                       ("source_sha256", "source_sha256"), ("raw_configuration", "configuration"),
                       ("sections", "section accounting"), ("artifact_bytes", "artifact sizes"),
                       ("kernel_config_identity", "kernel configuration")):
        if previous[key] != current[key]:
            raise ValueError(f"case build metadata changed across blocks: {label}")


def _leaf_artifact_name(name):
    if (not isinstance(name, str) or not name or "\\" in name or
            name in (".", "..") or PurePosixPath(name).name != name or
            PurePosixPath(name).is_absolute()):
        raise ValueError("artifact byte names must be leaf filenames")
    return name


def _validated_artifact_bytes(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("artifact byte metadata is invalid")
    output = {_leaf_artifact_name(name): size for name, size in value.items()}
    if any(type(size) is not int or size < 1 for size in output.values()):
        raise ValueError("artifact byte sizes must be positive integers")
    if not {"image.bin", "app.elf"}.issubset(output):
        raise ValueError("image.bin and app.elf sizes are required")
    return output


def _validate_rotated_order(prepared, blocks):
    first_block_cases = [entry["case"] for entry in prepared if entry["block"] == 0]
    expected = [
        (case, block)
        for block in range(blocks)
        for case in scheduling.matrix.rotated_cases(first_block_cases, block)
    ]
    if [(entry["case"], entry["block"]) for entry in prepared] != expected:
        raise ValueError("matrix case/block order does not follow the rotated schedule")


def _validate_matched_inputs(prepared):
    if not prepared:
        raise ValueError("matrix has no build inputs")
    source_maps = {json.dumps(entry["source_sha256"], sort_keys=True) for entry in prepared}
    if len(source_maps) != 1:
        raise ValueError("scheduling cases were built from different firmware inputs")
    fixed_configuration = {
        "cpu_mhz": 240, "cores": 1, "flash_mode": "DIO",
        "flash_frequency_mhz": 40, "queues": 60, "slots": 480,
        "event_bytes": 64, "duration_us": 2_000_000, "drain_us": 500_000,
        "application_opt_level": "O2", "work_short_iterations": 10_000,
        "work_medium_iterations": 100_000, "work_long_iterations": 400_000,
        "work_service": 0, "work_kind": 1,
    }
    for entry in prepared:
        config = entry["raw_configuration"]
        if any(type(config.get(key)) is not type(value) or config.get(key) != value
               for key, value in fixed_configuration.items()):
            raise ValueError("scheduling case changed the common workload or board configuration")
    native = [entry["kernel_config_identity"] for entry in prepared
              if entry["platform"].startswith("nuttx-")]
    if native and (any(identity is None for identity in native) or len(set(native)) != 1):
        raise ValueError("NuttX C/Rust kernel configurations differ")


def _measurement_path(root, relative, case, block):
    expected = f"{case}-block-{block:02d}"
    if relative != expected:
        raise ValueError("measurement directory does not match case/block")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(p in ("", ".", "..") for p in path.parts):
        raise ValueError("unsafe measurement directory")
    base = root.resolve(strict=True)
    resolved = (root / Path(*path.parts)).resolve(strict=True)
    if not resolved.is_relative_to(base) or not resolved.is_dir():
        raise ValueError("measurement directory escapes matrix output")
    return resolved


def _clean_configuration(configuration):
    if not isinstance(configuration, dict):
        raise ValueError("build configuration is required")
    return shared_report._sanitize_metadata(configuration)


def _compare_projection(stored, parsed):
    expected = {key: parsed[key] for key in (
        "profile", "result", "services", "resources", "memory", "control",
        "free_memory", "raw_output_sha256")}
    expected["scheduling"] = parsed["scheduling"]
    if stored != expected:
        raise ValueError("stored scheduling row disagrees with reparsed private measurement")


def validate_matrix(path):
    try:
        record = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read scheduling matrix: {exc}") from exc
    if (not isinstance(record, dict) or record.get("schema") != 1 or
            record.get("kind") != "event-services-scheduling" or
            record.get("contract_version") != 1):
        raise ValueError("unsupported scheduling matrix schema")
    if record.get("failure") is not None or record.get("restore_verified") is not True or record.get("restore_error") not in (None, ""):
        raise ValueError("matrix failed or firmware restore was not verified")
    _hash(record.get("backup_sha256"), "backup_sha256")
    blocks, runs = record.get("blocks"), record.get("runs_per_profile")
    if type(blocks) is not int or blocks < 1 or type(runs) is not int or runs < 1:
        raise ValueError("blocks and runs_per_profile must be positive integers")
    profiles = record.get("profiles")
    if (not isinstance(profiles, list) or not profiles or
            any(not isinstance(p, str) or p not in scheduling.PROFILES for p in profiles) or
            len(set(profiles)) != len(profiles)):
        raise ValueError("invalid scheduling profile selection")
    entries = record.get("order")
    if not isinstance(entries, list) or not entries:
        raise ValueError("scheduling matrix has no completed cases")
    seen, labels, prepared = set(), set(), []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("matrix entries must be objects")
        case = entry.get("case")
        if case not in scheduling.CASE_POLICIES:
            raise ValueError("unknown scheduling case")
        platform, policy, mode = scheduling.CASE_POLICIES[case]
        block = entry.get("block")
        if type(block) is not int or not 0 <= block < blocks or (case, block) in seen:
            raise ValueError("invalid or duplicate case/block")
        if (entry.get("platform"), entry.get("layout"), entry.get("policy"), entry.get("work_mode")) != (platform, "three", policy, mode):
            raise ValueError("case label and scheduling policy metadata disagree")
        configuration = entry.get("configuration")
        required = {
            "publication_timer_resolution_ms": 1, "embassy_scheduling": policy,
            "work_mode": mode, "scheduling_budget_events": 4,
            "scheduling_budget_us": 500, "work_chunk_iterations": 10_000,
            "io_wait_us": 3_000,
        }
        if (not isinstance(configuration, dict) or
                any(type(configuration.get(k)) is not type(v) or configuration.get(k) != v
                    for k, v in required.items())):
            raise ValueError("case scheduling configuration mismatch")
        if platform != "embassy" and mode == "chunked":
            raise ValueError("chunked native scheduling is unsupported")
        _hash(entry.get("image_sha256"), "image_sha256")
        _hash(entry.get("elf_sha256"), "elf_sha256")
        source = shared_report._source_hashes(entry.get("source_sha256"))
        kernel_identity = entry.get("kernel_config_identity")
        if kernel_identity is not None:
            kernel_identity = _hash(kernel_identity, "kernel_config_identity")
        if platform.startswith("nuttx-") and kernel_identity is None:
            raise ValueError("NuttX kernel configuration identity is required")
        sections = entry.get("section_accounting")
        if not isinstance(sections, dict):
            raise ValueError("section accounting metadata is required")
        for key in ("resident_ram_bytes", "loadbearing_flash_bytes"):
            if type(sections.get(key)) is not int or sections[key] < 0:
                raise ValueError(f"invalid section_accounting.{key}")
        artifact_bytes = _validated_artifact_bytes(entry.get("artifact_bytes"))
        measurement = _measurement_path(Path(path).parent, entry.get("measurement_dir"), case, block)
        try:
            raw_record = json.loads((measurement / "measurement.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("private measurement is missing or malformed") from exc
        checked = control.verify_record(raw_record, image_sha256=entry["image_sha256"],
                                        platform=platform, layout="three", profiles=profiles, runs=runs)
        control.validate_build_timer(checked, configuration)
        per_profile, clean_runs = {p: 0 for p in profiles}, []
        for stored, raw in zip(entry.get("runs", []), checked):
            parsed_base = scheduling.validate_run(raw_record["runs"][len(clean_runs)]["raw_output"],
                                                  raw["profile"], "three", platform,
                                                  configuration, policy)
            parsed = {key: raw[key] for key in ("profile", "result", "services", "resources", "memory", "control", "free_memory", "raw_output_sha256")}
            parsed["scheduling"] = parsed_base["scheduling"]
            _compare_projection(stored, parsed)
            per_profile[raw["profile"]] += 1
            clean_runs.append(parsed)
        if len(entry.get("runs", [])) != runs * len(profiles) or any(v != runs for v in per_profile.values()):
            raise ValueError("incomplete matrix run projection")
        prepared_entry = {"case": case, "block": block, "platform": platform,
                         "policy": policy, "work_mode": mode,
                         "configuration": _clean_configuration(configuration),
                         "raw_configuration": configuration,
                         "kernel_config_identity": kernel_identity,
                         "source_sha256": source, "sections": sections,
                         "artifact_bytes": artifact_bytes,
                         "image_sha256": entry["image_sha256"].lower(),
                         "elf_sha256": entry["elf_sha256"].lower(), "runs": clean_runs}
        prepared.append(prepared_entry)
        seen.add((case, block)); labels.add(case)
    if seen != {(case, block) for case in labels for block in range(blocks)}:
        raise ValueError("matrix is missing case blocks")
    _validate_rotated_order(prepared, blocks)
    identity_by_case = {}
    for entry in prepared:
        previous = identity_by_case.setdefault(entry["case"], entry)
        _require_case_metadata_identity(previous, entry)
    _validate_matched_inputs(prepared)
    # Comparisons share a configuration only if all fixed workload parameters match.
    for field in ("scheduling_budget_events", "scheduling_budget_us", "work_chunk_iterations", "io_wait_us", "publication_timer_resolution_ms"):
        values = {entry["configuration"][field] for entry in prepared}
        if len(values) != 1:
            raise ValueError(f"cases use different {field}; configurations must be compared separately")
    return record, prepared, profiles, blocks, runs


def build_report(matrix_path):
    record, entries, profiles, blocks, runs_per_profile = validate_matrix(matrix_path)
    grouped_entries = {}
    for entry in entries:
        grouped_entries.setdefault(entry["case"], []).append(entry)
    cases = {}
    for case, case_entries in grouped_entries.items():
        case_entries.sort(key=lambda entry: entry["block"])
        entry = case_entries[0]
        rows = [row for block_entry in case_entries for row in block_entry["runs"]]
        profiles_out = {}
        for profile in profiles:
            group = [row for row in rows if row["profile"] == profile]
            metric_rows = []
            for row in group:
                result, service, scheduling_row = row["result"], row["services"][0], row["scheduling"]
                peer = row["services"][1:]
                metric_rows.append({
                    **{key: result[key] for key in COUNTS},
                    "protocol_errors": result["errors"],
                    "aggregate_start_p99_us": result["start_p99_us"],
                    "aggregate_start_max_us": result["start_max_us"],
                    "aggregate_queue_p99_us": result["queue_p99_us"],
                    "aggregate_queue_max_us": result["queue_max_us"],
                    "aggregate_control_p99_us": result["control_p99_us"],
                    "aggregate_control_max_us": result["control_max_us"],
                    "service0_start_p99_us": service["start_p99_us"],
                    "service0_start_max_us": service["start_max_us"],
                    "service0_queue_p99_us": service["queue_p99_us"],
                    "service0_queue_max_us": service["queue_max_us"],
                    "peer_worst_start_p99_us": max(s["start_p99_us"] for s in peer),
                    "peer_worst_start_max_us": max(s["start_max_us"] for s in peer),
                    "peer_worst_queue_p99_us": max(s["queue_p99_us"] for s in peer),
                    "peer_worst_queue_max_us": max(s["queue_max_us"] for s in peer),
                    "peer_worst_control_p99_us": max(s["control_p99_us"] for s in peer),
                    "peer_control_missed_total": sum(s["missed_control"] for s in peer),
                    "control_missed_total": sum(s["missed_control"] for s in row["services"]),
                    **{k: row["control"][k] for k in ("work_iterations", "work_jobs", "work_p99_us", "work_max_us")},
                    **{k: scheduling_row[k] for k in ("yields", "io_jobs", "io_p99_us", "io_max_us", "work_digest", "diagnostic_bytes")},
                })
            summaries = {key: _summary([row[key] for row in metric_rows]) for key in metric_rows[0]}
            profiles_out[profile] = {"runs": metric_rows, "metrics": summaries}
        sections = entry["sections"]
        memory = [row["memory"] for row in rows]
        heap_peaks = [m["heap_live"] for m in memory]
        heap_peaks.extend(r["resources"].get("heap_allocated", 0) for r in rows)
        if entry["platform"].startswith("nuttx-"):
            heap_peaks.extend(
                row["free_memory"]["maxused"]
                for row in rows if row["free_memory"] is not None
            )
        cases[entry["case"]] = {
            "platform": entry["platform"], "layout": "three", "policy": entry["policy"],
            "work_mode": entry["work_mode"], "configuration": entry["configuration"],
            "image_sha256": entry["image_sha256"], "elf_sha256": entry["elf_sha256"],
            "source_sha256": entry["source_sha256"], "image_bytes": entry["artifact_bytes"]["image.bin"],
            "loaded_flash_bytes": sections["loadbearing_flash_bytes"],
            "resident_ram_bytes": sections["resident_ram_bytes"],
            "peak_heap_bytes": max(heap_peaks),
            "blocks": [block_entry["block"] for block_entry in case_entries],
            "profiles": profiles_out,
        }
    grouped = {}
    for case, data in cases.items():
        key = json.dumps(data["configuration"], sort_keys=True, separators=(",", ":"))
        grouped.setdefault(key, []).append(case)
    return {"schema": 1, "kind": "event-services-scheduling-report",
            "backup_sha256": _hash(record["backup_sha256"], "backup_sha256"),
            "profiles": profiles, "blocks": blocks, "runs_per_profile": runs_per_profile,
            "configuration_groups": [
                {"configuration": cases[case_list[0]]["configuration"], "cases": sorted(case_list)}
                for case_list in grouped.values()], "cases": cases}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error("--out must name a fresh file")
    try:
        output = build_report(args.matrix)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n")
    args.out.chmod(0o600)
    print(f"EVENT_SERVICES_SCHEDULING_REPORT_PASS {args.out.name}")


if __name__ == "__main__":
    main()
