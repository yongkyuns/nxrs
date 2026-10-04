#!/usr/bin/env python3
"""Run frozen images locally, rotating OS order, then restore a private backup."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

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


def case_directory(root, case):
    os_name, folder, _, _ = CASES[case]
    return root / "nuttx-matched" / folder if os_name == "nuttx" else root / folder


def frozen_image(directory, case):
    """Reject unfinished or changed artifacts before touching the device."""
    os_name, _, language, _ = CASES[case]
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


def rotated_cases(cases, block):
    offset = block % len(cases)
    return cases[offset:] + cases[:offset]


def command(case, directory, output, port, flasher, runs):
    os_name, _, language, mode = CASES[case]
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


def restore(flasher, port, backup, out):
    # Preserve the backed-up image header, not esptool's auto-detected defaults.
    for name, args in (("restore.log", ["write_flash", "--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep"]),
                       ("restore-verify.log", ["verify_flash"])):
        result = subprocess.run([flasher, "--chip", "esp32s3", "--port", port, "--baud", "460800",
                                 *args, "0x0", str(backup)], capture_output=True, text=True, timeout=600)
        (out / name).write_text(result.stdout + result.stderr)
        result.check_returncode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--flasher", required=True)
    parser.add_argument("--case", choices=CASES, action="append", required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--blocks", type=int, default=1)
    args = parser.parse_args()
    if (args.out.exists() or args.runs < 1 or args.blocks < 1 or
            len(set(args.case)) != len(args.case)):
        parser.error("fresh output, positive runs/blocks and distinct cases are required")
    if not args.backup.is_file() or args.backup.stat().st_size != 16777216:
        parser.error("a complete 16 MiB local backup is required before any flashing")
    if args.backup.stat().st_mode & 0o077:
        parser.error("firmware backup must be private (chmod 600)")
    for case in args.case:
        directory = case_directory(args.artifacts, case)
        frozen_image(directory, case)
    args.out.mkdir(parents=True)
    record = {"schema": 1, "backup_sha256": hashlib.sha256(args.backup.read_bytes()).hexdigest(),
              "runs_per_block": args.runs, "blocks": args.blocks, "order": [],
              "failure": None, "restore_verified": False}
    try:
        for block in range(args.blocks):
            cases = rotated_cases(args.case, block)
            for case in cases:
                output = args.out / f"{case}-block-{block}"
                subprocess.run(command(case, case_directory(args.artifacts, case), output,
                                       args.port, args.flasher, args.runs), check=True)
                record["order"].append({"case": case, "block": block})
    except Exception as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            restore(args.flasher, args.port, args.backup, args.out)
            record["restore_verified"] = True
        finally:
            (args.out / "matrix.json").write_text(json.dumps(record, indent=2) + "\n")
    print("COMPARISON_MATRIX_PASS firmware_restored=true")


if __name__ == "__main__":
    main()
