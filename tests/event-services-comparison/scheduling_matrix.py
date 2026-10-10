#!/usr/bin/env python3
"""Run the bounded Embassy scheduling comparison on a local device."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from rtos_harness.matrix import restored_session


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix = load("event_services_scheduling_matrix_helpers", HERE / "run_matrix.py")
control = load("event_services_scheduling_control_measure", HERE / "control_measure.py")
MEASURE = HERE / "control_measure.py"
CASE_POLICIES = {
    "nuttx-c-three": ("nuttx-c", "native", "monolithic"),
    "nuttx-rust-three": ("nuttx-rust", "native", "monolithic"),
    "zephyr-c-three": ("zephyr-c", "native", "monolithic"),
    "embassy-three-event": ("embassy", "event", "monolithic"),
    "embassy-three-natural": ("embassy", "natural", "monolithic"),
    "embassy-three-budget": ("embassy", "budget", "monolithic"),
    "embassy-three-chunked": ("embassy", "budget", "chunked"),
}
PROFILES = tuple(p for p in control.CONTROL_TRAFFIC_PROFILES if p not in ("overload", "io-wait")) + ("io-wait",)
SCHED_FIELDS = ("policy", "work_mode", "budget_events", "budget_us",
                "chunk_iterations", "io_wait_us", "yields", "io_jobs",
                "io_p99_us", "io_max_us", "work_digest", "diagnostic_bytes")
def validate_work_mode(platform, work_mode):
    if platform != "embassy" and work_mode != "monolithic":
        raise ValueError("native scheduling variants only support monolithic work")


def validate_matched_inputs(frozen):
    """Policies may differ; frozen firmware sources and offered work may not."""
    sources = {json.dumps(v["provenance"]["source_sha256"], sort_keys=True)
               for v in frozen.values()}
    if len(sources) != 1:
        raise ValueError("scheduling cases were built from different firmware inputs")
    expected = {"cpu_mhz": 240, "cores": 1, "flash_mode": "DIO",
                "flash_frequency_mhz": 40, "queues": 60, "slots": 480,
                "event_bytes": 64, "duration_us": 2000000, "drain_us": 500000,
                "application_opt_level": "O2", "work_short_iterations": 10000,
                "work_medium_iterations": 100000, "work_long_iterations": 400000,
                "work_service": 0, "work_kind": 1}
    for variant in frozen.values():
        if any(variant["configuration"].get(k) != value for k, value in expected.items()):
            raise ValueError("scheduling case changed the common workload or board configuration")
    native = [v["provenance"].get("kernel_config_identity") for v in frozen.values()
              if v["platform"].startswith("nuttx-")]
    if native and (any(not identity for identity in native) or len(set(native)) != 1):
        raise ValueError("NuttX C/Rust kernel configurations differ")


def _variant(directory, case):
    if case not in CASE_POLICIES:
        raise ValueError(f"unsupported scheduling case: {case}")
    platform, policy, work_mode = CASE_POLICIES[case]
    provenance_path = directory / "build-provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid build provenance for {case}") from exc
    layout = provenance.get("layout") if isinstance(provenance, dict) else None
    if layout != "three" or provenance.get("platform") != platform:
        raise ValueError(f"{case} must declare platform={platform}, layout=three")
    # The shared validator owns path safety, artifact hashing, source identity,
    # and the core platform/layout contract. Its case is the matching base case.
    base_case = f"{platform}-three"
    frozen = matrix.frozen_image(directory, base_case)
    configuration = provenance.get("configuration")
    if isinstance(configuration, dict) and configuration.get("instrumentation", "full") != "full":
        raise ValueError("lean footprint images cannot supply scheduling latency distributions")
    expected = {
        "publication_timer_resolution_ms": 1,
        "embassy_scheduling": policy,
        "work_mode": work_mode,
        "scheduling_budget_events": 4,
        "scheduling_budget_us": 500,
        "work_chunk_iterations": 10_000,
        "io_wait_us": 3_000,
    }
    if not isinstance(configuration, dict) or any(configuration.get(k) != v for k, v in expected.items()):
        raise ValueError(f"{case} build configuration does not match its scheduling contract")
    if platform != "embassy" and policy != "native":
        raise ValueError(f"{case} must use native scheduling")
    validate_work_mode(platform, work_mode)
    return {**frozen, "case": case, "platform": platform, "layout": "three",
            "policy": policy, "work_mode": work_mode, "configuration": configuration}


def validate_sched_row(text, profile, configuration, policy):
    text = control.measure.normalize_output(text)
    rows = control._rows(text, "ES_SCHED", 1)
    row = rows[0]
    if set(row) != set(SCHED_FIELDS):
        raise ValueError("ES_SCHED field set mismatch")
    expected_string = {"policy": policy, "work_mode": configuration["work_mode"]}
    for key, value in expected_string.items():
        if row.get(key) != value:
            raise ValueError(f"ES_SCHED {key} disagrees with build configuration")
    numeric_config = {
        "budget_events": "scheduling_budget_events", "budget_us": "scheduling_budget_us",
        "chunk_iterations": "work_chunk_iterations", "io_wait_us": "io_wait_us",
    }
    for key, config_key in numeric_config.items():
        control._number(row, key, "ES_SCHED")
        if row[key] != configuration[config_key]:
            raise ValueError(f"ES_SCHED {key} disagrees with build configuration")
    for key in ("yields", "io_jobs", "io_p99_us", "io_max_us", "work_digest", "diagnostic_bytes"):
        control._number(row, key, "ES_SCHED")
    if row["diagnostic_bytes"] != 276:
        raise ValueError("ES_SCHED diagnostic_bytes must be 276")
    if profile not in ("work-short", "work-medium", "work-long") and row["work_digest"] != 0:
        raise ValueError("ES_SCHED work_digest must be zero without CPU work")
    if row["io_jobs"] != (117 if profile == "io-wait" else 0):
        raise ValueError("ES_SCHED I/O job count disagrees with profile")
    if row["io_p99_us"] > row["io_max_us"]:
        raise ValueError("ES_SCHED I/O p99 exceeds maximum")
    if row["io_jobs"] == 0 and (row["io_p99_us"] or row["io_max_us"]):
        raise ValueError("ES_SCHED I/O latency must be zero without jobs")
    if row["io_jobs"] and min(row["io_p99_us"], row["io_max_us"]) < configuration["io_wait_us"]:
        raise ValueError("ES_SCHED I/O duration is shorter than configured wait")
    if policy in ("native", "natural") and row["yields"] != 0:
        raise ValueError("natural scheduling must not report cooperative yields")
    return dict(row)


def validate_run(raw, profile, layout, platform, configuration, policy):
    parsed = control.validate_traffic_output(raw, profile, layout, platform)
    parsed["scheduling"] = validate_sched_row(raw, profile, configuration, policy)
    return parsed


def command(case, image, output, port, flasher, runs, profiles):
    variant = CASE_POLICIES[case]
    platform = variant[0]
    result = [sys.executable, str(MEASURE), "--image", str(image), "--platform", platform,
              "--layout", "three", "--port", port, "--flasher", flasher,
              "--out", str(output), "--runs", str(runs)]
    for profile in profiles:
        result.extend(("--profile", profile))
    return result


def _measurements(output, image, case, profiles, runs, variant):
    record = json.loads((output / "measurement.json").read_text())
    checked = control.verify_record(record, image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                                    platform=variant["platform"], layout="three",
                                    profiles=profiles, runs=runs)
    control.validate_build_timer(checked, variant["configuration"])
    safe = []
    for row in checked:
        raw_row = validate_run(record["runs"][len(safe)]["raw_output"], row["profile"], "three",
                               variant["platform"], variant["configuration"], variant["policy"])
        clean = {k: row[k] for k in ("profile", "result", "services", "resources", "memory",
                                      "control", "free_memory", "raw_output_sha256")}
        clean["scheduling"] = raw_row["scheduling"]
        safe.append(clean)
    return safe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--port", required=True)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--case", choices=CASE_POLICIES, action="append")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--blocks", type=int, default=2)
    parser.add_argument("--profile", choices=PROFILES, action="append")
    args = parser.parse_args()
    cases, profiles = args.case or list(CASE_POLICIES), args.profile or list(PROFILES)
    if args.out.exists() or args.runs < 1 or args.blocks < 1 or len(set(cases)) != len(cases) or len(set(profiles)) != len(profiles):
        parser.error("fresh output, positive runs/blocks, and distinct cases/profiles are required")
    try:
        backup_hash = matrix.backup_identity(args.backup)
        frozen = {case: _variant(args.artifacts / case, case) for case in cases}
        validate_matched_inputs(frozen)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.out.mkdir(parents=True)
    args.out.chmod(0o700)
    record = {"schema": 1, "kind": "event-services-scheduling", "contract_version": 1,
              "backup_sha256": backup_hash, "runs_per_profile": args.runs, "profiles": profiles,
              "blocks": args.blocks, "order": [], "failure": None,
              "restore_verified": False, "restore_error": None}
    with restored_session(record, args.out / "scheduling-matrix.json", args.backup,
                          restore=lambda: matrix.restore(args.flasher, args.port, args.backup, args.out),
                          identity=matrix.backup_identity):
        for block in range(args.blocks):
            for case in matrix.rotated_cases(cases, block):
                if matrix.backup_identity(args.backup) != backup_hash:
                    raise ValueError("firmware backup changed before flashing")
                variant = frozen[case]
                # Revalidate all pinned artifacts immediately before use.
                variant = _variant(args.artifacts / case, case)
                measurement_dir = f"{case}-block-{block:02d}"
                output = args.out / measurement_dir
                subprocess.run(command(case, variant["image"], output, args.port, args.flasher,
                                       args.runs, profiles), check=True)
                runs = _measurements(output, variant["image"], case, profiles, args.runs, variant)
                provenance = variant["provenance"]
                image_name, elf_name = provenance["image_file"], provenance["elf_file"]
                entry = {"case": case, "block": block, "measurement_dir": measurement_dir,
                         "platform": variant["platform"], "layout": "three",
                         "policy": variant["policy"], "work_mode": variant["work_mode"],
                         "image_sha256": hashlib.sha256(variant["image"].read_bytes()).hexdigest(),
                         "elf_sha256": hashlib.sha256(variant["elf"].read_bytes()).hexdigest(),
                         "artifact_bytes": {Path(k).name: v for k, v in variant["artifact_bytes"].items()},
                         "source_sha256": provenance["source_sha256"],
                         "configuration": variant["configuration"],
                         "section_accounting": provenance.get("section_accounting", {}), "runs": runs}
                for optional in ("kernel_config_identity", "resolved_configuration"):
                    if optional in provenance:
                        entry[optional] = provenance[optional]
                if Path(image_name).name != image_name or Path(elf_name).name != elf_name:
                    raise ValueError("artifact names must be leaf names in public metadata")
                record["order"].append(entry)
    print(f"EVENT_SERVICES_SCHEDULING_MATRIX_PASS restored=true entries={len(record['order'])}")


if __name__ == "__main__":
    main()
