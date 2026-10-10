#!/usr/bin/env python3
"""Run control profiles across selected frozen event-service firmware images."""
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


matrix = load("event_services_matrix_helpers", HERE / "run_matrix.py")
control_measure = load("event_services_control_measure", HERE / "control_measure.py")
CASES = matrix.CASES
PROFILES = control_measure.CONTROL_PROFILES
MEASURE = HERE / "control_measure.py"


def command(case, image, output, port, flasher, runs, profiles, nuttx_console="nsh"):
    platform, layout = CASES[case]
    result = [
        sys.executable,
        str(MEASURE),
        "--image",
        str(image),
        "--platform",
        platform,
        "--layout",
        layout,
        "--port",
        port,
        "--flasher",
        flasher,
        "--out",
        str(output),
        "--runs",
        str(runs),
    ]
    if platform.startswith("nuttx-"):
        result += ["--nuttx-console", nuttx_console]
    for profile in profiles:
        result.extend(("--profile", profile))
    return result


def _numeric_rows(output, image, case, profiles, runs, timer_ms, nuttx_console="nsh"):
    record = json.loads((output / "measurement.json").read_text())
    platform, layout = CASES[case]
    checked = control_measure.verify_record(
        record,
        image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        platform=platform,
        layout=layout,
        profiles=profiles,
        runs=runs,
        nuttx_console=nuttx_console,
    )
    control_measure.validate_build_timer(
        checked, {"publication_timer_resolution_ms": timer_ms}
    )
    safe = []
    for row in checked:
        common = {
            "profile": row["profile"],
            "raw_output_sha256": row["raw_output_sha256"],
            "free_memory": row["free_memory"],
        }
        if row["profile"] == "saturation":
            common.update(
                result=row["result"], cycles=row["cycles"], resources=row["resources"]
            )
        else:
            common.update(
                result=row["result"],
                services=row["services"],
                resources=row["resources"],
                memory=row["memory"],
                control=row["control"],
            )
        safe.append(common)
    return safe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--port", required=True)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--blocks", type=int, default=2)
    parser.add_argument("--profile", choices=PROFILES, action="append")
    args = parser.parse_args()
    cases = args.case or list(CASES)
    profiles = args.profile or list(PROFILES)
    if (
        args.out.exists()
        or args.runs < 1
        or args.blocks < 1
        or len(set(cases)) != len(cases)
        or len(set(profiles)) != len(profiles)
    ):
        parser.error(
            "fresh output, positive runs/blocks and distinct cases/profiles are required"
        )
    try:
        backup_hash = matrix.backup_identity(args.backup)
        frozen = {
            case: matrix.frozen_image(matrix.case_directory(args.artifacts, case), case)
            for case in cases
        }
        timers = {}
        for case, image_record in frozen.items():
            configuration = image_record["provenance"].get("configuration")
            timer_ms = (
                configuration.get("publication_timer_resolution_ms")
                if isinstance(configuration, dict)
                else None
            )
            if timer_ms not in (1, 10):
                raise ValueError(
                    f"{case} build configuration lacks a supported publication timer"
                )
            timers[case] = timer_ms
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    args.out.mkdir(parents=True)
    args.out.chmod(0o700)
    record = {
        "schema": 1,
        "kind": "event-services-control",
        "contract_version": 1,
        "backup_sha256": backup_hash,
        "runs_per_profile": args.runs,
        "profiles": profiles,
        "blocks": args.blocks,
        "order": [],
        "failure": None,
        "restore_verified": False,
        "restore_error": None,
    }
    with restored_session(record, args.out / "control-matrix.json", args.backup,
                          restore=lambda: matrix.restore(args.flasher, args.port, args.backup, args.out),
                          identity=matrix.backup_identity):
        for block in range(args.blocks):
            for case in matrix.rotated_cases(cases, block):
                if matrix.backup_identity(args.backup) != backup_hash:
                    raise ValueError("firmware backup changed before flashing")
                measurement_dir = f"{case}-block-{block:02d}"
                output = args.out / measurement_dir
                timer_ms = timers[case]
                subprocess.run(
                    command(
                        case,
                        frozen[case]["image"],
                        output,
                        args.port,
                        args.flasher,
                        args.runs,
                        profiles,
                        frozen[case]["provenance"].get("configuration", {}).get("console", "nsh"),
                    ),
                    check=True,
                )
                provenance = frozen[case]["provenance"]
                runs = _numeric_rows(
                    output,
                    frozen[case]["image"],
                    case,
                    profiles,
                    args.runs,
                    timer_ms,
                    provenance.get("configuration", {}).get("console", "nsh"),
                )
                entry = {
                    "case": case,
                    "block": block,
                    "measurement_dir": measurement_dir,
                    "platform": provenance["platform"],
                    "layout": provenance["layout"],
                    "image_sha256": hashlib.sha256(
                        frozen[case]["image"].read_bytes()
                    ).hexdigest(),
                    "elf_sha256": hashlib.sha256(
                        frozen[case]["elf"].read_bytes()
                    ).hexdigest(),
                    "artifact_bytes": frozen[case]["artifact_bytes"],
                    "configuration": provenance.get("configuration"),
                    "kernel_config_identity": provenance.get("kernel_config_identity"),
                    "source_sha256": provenance["source_sha256"],
                    "runs": runs,
                }
                for key in ("resolved_configuration", "section_accounting"):
                    if key in provenance:
                        entry[key] = provenance[key]
                record["order"].append(entry)
    print(
        f"EVENT_SERVICES_CONTROL_MATRIX_PASS restored=true cases={len(record['order'])}"
    )


if __name__ == "__main__":
    main()
