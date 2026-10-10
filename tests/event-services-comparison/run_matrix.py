#!/usr/bin/env python3
"""Measure the frozen event-service image matrix locally, then restore firmware."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from rtos_harness.device import restore
from rtos_harness.matrix import backup_identity, restored_session, rotated_cases

MEASURE = HERE / "measure.py"
CASES = {
    "nuttx-c-three": ("nuttx-c", "three"),
    "nuttx-c-one": ("nuttx-c", "one"),
    "nuttx-rust-three": ("nuttx-rust", "three"),
    "nuttx-rust-one": ("nuttx-rust", "one"),
    "zephyr-c-three": ("zephyr-c", "three"),
    "zephyr-c-one": ("zephyr-c", "one"),
    "embassy-three": ("embassy", "three"),
    "embassy-one": ("embassy", "one"),
}
PROFILES = ("normal", "burst", "overload")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


measure = load("event_services_measure", MEASURE)


def case_directory(root, case):
    if case not in CASES:
        raise ValueError(f"unknown case: {case}")
    return root / case


def _safe_artifact_path(directory, name):
    if (not isinstance(name, str) or not name or "\\" in name or
            re.match(r"^[A-Za-z]:", name)):
        raise ValueError("artifact paths must be nonempty relative POSIX paths")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"unsafe artifact path: {name}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError(f"unsafe artifact path: {name}")
    base = directory.resolve(strict=True)
    path = directory.joinpath(*relative.parts)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"missing or invalid artifact: {name}") from exc
    if not resolved.is_relative_to(base):
        raise ValueError(f"artifact escapes case directory: {name}")
    if not resolved.is_file():
        raise ValueError(f"artifact is not a file: {name}")
    return path


def frozen_image(directory, case):
    """Validate provenance, path containment and every frozen-artifact hash."""
    expected_platform, expected_layout = CASES[case]
    provenance_path = directory / "build-provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid build provenance for {case}") from exc
    if (not isinstance(provenance, dict) or provenance.get("status") != "success" or
            "failure" not in provenance or provenance["failure"] is not None):
        raise ValueError(f"incomplete frozen build: {directory}")
    if provenance.get("platform") != expected_platform or provenance.get("layout") != expected_layout:
        raise ValueError(f"frozen build platform/layout mismatch: {case}")
    artifacts = provenance.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError(f"missing artifact hash mapping: {case}")
    image_name, elf_name = provenance.get("image_file"), provenance.get("elf_file")
    if not isinstance(image_name, str) or image_name not in artifacts:
        raise ValueError(f"image file is not hash-pinned: {case}")
    if not isinstance(elf_name, str) or elf_name not in artifacts:
        raise ValueError(f"ELF file is not hash-pinned: {case}")
    if any(not isinstance(name, str) or not isinstance(digest, str) or
           not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
           for name, digest in artifacts.items()):
        raise ValueError(f"invalid artifact SHA-256 mapping: {case}")
    resolved_artifacts = {}
    artifact_bytes = {}
    for name, expected_hash in artifacts.items():
        path = _safe_artifact_path(directory, name)
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash.lower() != expected_hash.lower():
            raise ValueError(f"changed frozen artifact: {path}")
        resolved_artifacts[name] = path
        artifact_bytes[name] = path.stat().st_size
    declared_bytes = provenance.get("artifact_bytes")
    if declared_bytes is not None and (
            not isinstance(declared_bytes, dict) or declared_bytes != artifact_bytes):
        raise ValueError(f"artifact byte sizes do not match frozen files: {case}")
    source_hashes = provenance.get("source_sha256")
    if (not isinstance(source_hashes, dict) or not source_hashes or
            any(not isinstance(name, str) or not isinstance(digest, str) or
                not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                for name, digest in source_hashes.items())):
        raise ValueError(f"invalid source SHA-256 mapping: {case}")
    kernel_identity = provenance.get("kernel_config_identity")
    if kernel_identity is not None and (not isinstance(kernel_identity, str) or
            not re.fullmatch(r"[0-9a-fA-F]{64}", kernel_identity)):
        raise ValueError(f"invalid kernel configuration identity: {case}")
    return {"image": resolved_artifacts[image_name], "elf": resolved_artifacts[elf_name],
            "provenance": provenance, "artifact_bytes": artifact_bytes}


def command(case, image, output, port, flasher, runs, profiles, nuttx_console="nsh"):
    platform, layout = CASES[case]
    result = [sys.executable, str(MEASURE), "--image", str(image),
              "--platform", platform, "--layout", layout,
              "--port", port, "--flasher", flasher,
              "--out", str(output), "--runs", str(runs)]
    if platform.startswith("nuttx-"):
        result += ["--nuttx-console", nuttx_console]
    for profile in profiles:
        result.extend(("--profile", profile))
    return result


def _measurement_rows(output, image, case, profiles, runs, nuttx_console="nsh"):
    record_path = output / "measurement.json"
    record = json.loads(record_path.read_text())
    platform, layout = CASES[case]
    checked = measure.verify_measurement_record(
        record, image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        platform=platform, layout=layout, profiles=profiles, runs=runs, nuttx_console=nuttx_console)
    return [{"profile": row["profile"], "result": row["result"],
             "services": row["services"], "resources": row["resources"],
             "memory": row["memory"], "free_memory": row["free_memory"],
             "raw_output_sha256": record["runs"][index]["raw_output_sha256"]}
            for index, row in enumerate(checked)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--port", required=True)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--blocks", type=int, default=2)
    parser.add_argument("--profile", choices=PROFILES, action="append")
    args = parser.parse_args()
    cases = args.case or list(CASES)
    profiles = args.profile or list(PROFILES)
    if (args.out.exists() or args.runs < 1 or args.blocks < 1 or
            len(set(cases)) != len(cases) or len(set(profiles)) != len(profiles)):
        parser.error("fresh output, positive runs/blocks and distinct cases/profiles are required")
    try:
        backup_hash = backup_identity(args.backup)
        frozen = {case: frozen_image(case_directory(args.artifacts, case), case) for case in cases}
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    args.out.mkdir(parents=True)
    args.out.chmod(0o700)
    record = {"schema": 1, "backup_sha256": backup_hash, "runs_per_profile": args.runs,
              "profiles": profiles, "blocks": args.blocks, "order": [],
              "failure": None, "restore_verified": False, "restore_error": None}
    with restored_session(record, args.out / "matrix.json", args.backup,
                          restore=lambda: restore(args.flasher, args.port, args.backup, args.out),
                          identity=backup_identity):
        for block in range(args.blocks):
            for case in rotated_cases(cases, block):
                # Recheck the private source before each image can be flashed.
                if backup_identity(args.backup) != backup_hash:
                    raise ValueError("firmware backup changed before flashing")
                output = args.out / f"{case}-block-{block:02d}"
                subprocess.run(command(case, frozen[case]["image"], output, args.port,
                                       args.flasher, args.runs, profiles,
                                       frozen[case]["provenance"].get("configuration", {}).get("console", "nsh")), check=True)
                console = frozen[case]["provenance"].get("configuration", {}).get("console", "nsh")
                runs = _measurement_rows(output, frozen[case]["image"], case, profiles, args.runs, console)
                provenance = frozen[case]["provenance"]
                entry = {"case": case, "block": block,
                         "platform": provenance["platform"], "layout": provenance["layout"],
                         "image_sha256": hashlib.sha256(frozen[case]["image"].read_bytes()).hexdigest(),
                         "elf_sha256": hashlib.sha256(frozen[case]["elf"].read_bytes()).hexdigest(),
                         "artifact_bytes": frozen[case]["artifact_bytes"],
                         "configuration": provenance.get("configuration"),
                         "kernel_config_identity": provenance.get("kernel_config_identity"),
                         "source_sha256": provenance["source_sha256"],
                         "runs": runs}
                if "resolved_configuration" in provenance:
                    entry["resolved_configuration"] = provenance["resolved_configuration"]
                if "section_accounting" in provenance:
                    entry["section_accounting"] = provenance["section_accounting"]
                record["order"].append(entry)
    print(f"EVENT_SERVICES_MATRIX_PASS restored=true cases={len(record['order'])}")


if __name__ == "__main__":
    main()
