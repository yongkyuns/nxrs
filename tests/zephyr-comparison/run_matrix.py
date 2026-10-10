#!/usr/bin/env python3
"""Run frozen images locally, rotating OS order, then restore a private backup."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.device import restore
from rtos_harness.matrix import backup_identity, restored_session, rotated_cases

HERE = Path(__file__).resolve().parent
NUTTX = HERE.parent / "service-footprint"
CASES = {
    "c-wire-os": ("nuttx", "transport-c-wire-os-v2", "c", "wire"),
    "c-wire-2": ("nuttx", "transport-c-wire-2-v2", "c", "wire"),
    "rust-wire-2": ("nuttx", "transport-rust-borrowed-2-v2", "rust", "wire"),
    "c-packet-2": ("nuttx", "transport-c-packet-2-v1", "c", "packet"),
    "c-packet-matched-2": ("nuttx", "transport-c-packet-matched-2", "c", "packet"),
    "rust-packet-2": ("nuttx", "transport-rust-packet-2-v1", "rust", "packet"),
    "nuttx-baseline": ("nuttx", "baseline", "c", "baseline"),
    "zephyr-wire-os": ("zephyr", "zephyr-matched-wire-size-v3", "zephyr", "wire"),
    "zephyr-wire-2": ("zephyr", "zephyr-matched-wire-speed-v3", "zephyr", "wire"),
    "zephyr-packet-os": ("zephyr", "zephyr-matched-packet-size-v3", "zephyr", "packet"),
    "zephyr-packet-2": ("zephyr", "zephyr-matched-packet-2", "zephyr", "packet"),
    "zephyr-gcc14-packet-2": ("zephyr", "zephyr-matched-gcc14-packet-2", "zephyr", "packet"),
    "zephyr-native80-packet-2": ("zephyr", "zephyr-native80-packet-2", "zephyr", "packet"),
    "zephyr-lean80-packet-2": ("zephyr", "zephyr-lean80-packet-2", "zephyr", "packet"),
    "zephyr-baseline": ("zephyr", "zephyr-matched-baseline-size-v3", "zephyr", "baseline"),
}


def case_directory(root, case, cases=None):
    os_name, folder, _, _ = (CASES if cases is None else cases)[case]
    return root / "nuttx-matched" / folder if os_name == "nuttx" else root / folder


def frozen_image(directory, case, cases=None):
    """Reject unfinished or changed artifacts before touching the device."""
    os_name, _, language, _ = (CASES if cases is None else cases)[case]
    if os_name == "zephyr":
        provenance = json.loads((directory / "build-provenance.json").read_text())
        if provenance.get("status") != "success" or provenance.get("failure") is not None:
            raise ValueError(f"incomplete Zephyr build: {directory}")
        hashes = {"zephyr.bin": provenance["zephyr_bin_sha256"],
                  "zephyr/zephyr.elf": provenance["elf_sha256"],
                  "resolved.config": provenance["resolved_config_sha256"]}
        image = directory / "zephyr.bin"
    else:
        name = "c-build-provenance.json" if language == "c" else "relink-provenance.json"
        provenance = json.loads((directory / name).read_text())
        hashes = provenance["artifacts"]
        image = directory / f"{language}.merged.bin"
        if image.name not in hashes or f"{language}.elf" not in hashes or "resolved.config" not in hashes:
            raise ValueError(f"incomplete NuttX build: {directory}")
    for name, digest in hashes.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"changed frozen artifact: {directory / name}")
    return image


def command(case, directory, output, port, flasher, runs, cases=None):
    os_name, _, language, mode = (CASES if cases is None else cases)[case]
    if os_name == "zephyr":
        return [sys.executable, str(HERE / "measure.py"), "--image", str(directory / "zephyr.bin"),
                "--mode", mode, "--command", "baseline" if mode == "baseline" else "large",
                "--port", port, "--flasher", flasher, "--out", str(output), "--runs", str(runs)]
    app = "cq_c_scale" if language == "c" else "cq_scale"
    prefix = "NUTTX_BASELINE_PASS" if mode == "baseline" else (
        "CQ_C_SCALE_PASS mode=large" if language == "c" else "CQ_SCALE_PASS mode=large")
    return [sys.executable, str(NUTTX / "measure_device.py"), "--image", str(directory / f"{language}.merged.bin"),
            "--command", app + (" baseline" if mode == "baseline" else " large"),
            "--port", port, "--flasher", flasher, "--out", str(output), "--runs", str(runs),
            "--expected-prefix", prefix, "--command-timeout", "30"]


def main(argv=None, *, cases=None, image_validator=None, command_builder=None):
    cases = CASES if cases is None else cases
    image_validator = frozen_image if image_validator is None else image_validator
    command_builder = command if command_builder is None else command_builder
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--case", choices=cases, action="append", required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--blocks", type=int, default=1)
    args = parser.parse_args(argv)
    if (args.out.exists() or args.runs < 1 or args.blocks < 1 or
            len(set(args.case)) != len(args.case)):
        parser.error("fresh output, positive runs/blocks and distinct cases are required")
    try:
        backup_hash = backup_identity(args.backup)
    except ValueError as exc:
        parser.error(str(exc))
    for case in args.case:
        directory = case_directory(args.artifacts, case, cases)
        image_validator(directory, case)
    args.out.mkdir(parents=True)
    record = {"schema": 1, "backup_sha256": backup_hash,
              "runs_per_block": args.runs, "blocks": args.blocks, "order": [],
              "failure": None, "restore_verified": False, "restore_error": None}
    with restored_session(record, args.out / "matrix.json", args.backup,
                          restore=lambda: restore(args.flasher, args.port, args.backup, args.out)):
        for block in range(args.blocks):
            block_cases = rotated_cases(args.case, block)
            for case in block_cases:
                output = args.out / f"{case}-block-{block}"
                subprocess.run(command_builder(case, case_directory(args.artifacts, case, cases), output,
                                               args.port, args.flasher, args.runs), check=True)
                record["order"].append({"case": case, "block": block})
    print("COMPARISON_MATRIX_PASS firmware_restored=true")


if __name__ == "__main__":
    main()
