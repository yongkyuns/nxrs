#!/usr/bin/env python3
"""Execute the kernel witness in ESP32-S3 QEMU; no radio or device access."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import struct
import subprocess
import time

HERE = Path(__file__).resolve().parent
EXPECTED_CHECKS = 818
NUMERICAL_CHECKS = 398


def verify_numerical(result, status):
    result = result.replace("\r", "")
    if result.count("CSI_PROBE_ENTERED") != 1 or re.search(
        r"CSI_CHECK_FAIL|PANIC|panic:|Assertion failed", result
    ):
        raise ValueError("numerical witness missing, duplicated or failed")
    if re.findall(
        r"^CSI_NUMERICAL_REPORT checks=(\d+) failures=(\d+) guards=(\d+) live=(\d+)$",
        result,
        re.M,
    ) != [(str(NUMERICAL_CHECKS), "0", "0", "0")]:
        raise ValueError("numerical witness report incomplete")
    cases = re.findall(
        r"^CSI_NUMERIC_CASE scale=(1|1over16) split=(\d+),(\d+) repeat=(\d+) status=(-?\d+) allocations=(\d+) live=(\d+) guards=(\d+)$",
        result,
        re.M,
    )
    expected = [
        (scale, str(rows), str(10 - rows), str(repeat), code, "4", "0", "0")
        for scale, code in (("1", "-1"), ("1over16", "0"))
        for rows in (10, 7, 3, 1)
        for repeat in range(3)
    ]
    if cases != expected:
        raise ValueError(
            "native numerical results, split/repeat coverage or cleanup mismatch"
        )
    iterations = re.findall(r"iterate: (\d+), itrs_max: (\d+)", result)
    if iterations[:12] != [("30", "30")] * 12 or len(iterations) != 25:
        raise ValueError("native iteration-limit evidence missing")
    if any(
        limit != "30" or not 0 < int(count) < 30 for count, limit in iterations[12:]
    ):
        raise ValueError("scaled and control cases did not converge below the limit")
    if re.findall(r"^CSI_NSH_STATUS_(\d+)$", status.replace("\r", ""), re.M) != ["0"]:
        raise ValueError("numerical witness exit status")


def verify_nonfinite(result, status):
    result = result.replace("\r", "")
    if result.count("CSI_PROBE_ENTERED") != 1 or re.search(
        r"CSI_CHECK_FAIL|PANIC|panic:|Assertion failed", result
    ):
        raise ValueError("nonfinite witness missing, duplicated or failed")
    if re.findall(
        r"^CSI_NONFINITE_REPORT checks=(\d+) failures=(\d+) guards=(\d+) live=(\d+)$",
        result,
        re.M,
    ) != [("48", "0", "0", "0")]:
        raise ValueError("nonfinite witness report incomplete")
    cases = re.findall(
        r"^CSI_NONFINITE_CASE input=(zero|underflow|finite_control) split=(\d+),(\d+) repeat=(\d+) status=(-?\d+) finite=(\d+) allocations=(\d+) live=(\d+) guards=(\d+)$",
        result,
        re.M,
    )
    expected = [
        (
            name,
            str(rows),
            str(10 - rows),
            str(repeat),
            "0",
            "13" if name == "finite_control" else "0",
            "4",
            "0",
            "0",
        )
        for name in ("zero", "underflow", "finite_control")
        for rows in (10, 7)
        for repeat in range(2)
    ]
    if cases != expected:
        raise ValueError("nonfinite native cases/cleanup mismatch")
    if re.findall(r"^CSI_NSH_STATUS_(\d+)$", status.replace("\r", ""), re.M) != ["0"]:
        raise ValueError("nonfinite witness exit status")


def verify(result, status, negative=False):
    result = result.replace("\r", "")
    status = status.replace("\r", "")
    if result.count("CSI_PROBE_ENTERED") != 1 or re.search(
        r"CSI_CHECK_FAIL|PANIC|panic:|Assertion failed", result
    ):
        raise ValueError("missing entry, duplicate entry, check failure or panic")
    if negative:
        if result.count("CSI_DELIBERATE_FAILURE") != 1 or "CSI_REPORT" in result:
            raise ValueError("negative control produced a success report")
    else:
        reports = re.findall(
            r"^CSI_REPORT checks=(\d+) failures=(\d+) guards=(\d+) live=(\d+)$",
            result,
            re.M,
        )
        if reports != [(str(EXPECTED_CHECKS), "0", "0", "0")]:
            raise ValueError("wrong kernel report")
        if re.findall(
            r"^CSI_THREADS rounds=(\d+),(\d+) errors=(\d+),(\d+)$", result, re.M
        ) != [("100", "100", "0", "0")]:
            raise ValueError("thread workload incomplete")
        if re.findall(
            r"^CSI_FAULT allocation=(\d+) result=(\d+) live=(\d+) guards=(\d+)$",
            result,
            re.M,
        ) != [(str(i), "257", "0", "0") for i in range(1, 5)]:
            raise ValueError("allocation failure coverage incomplete")
        values = {
            k: struct.unpack("!f", bytes.fromhex(v))[0]
            for k, v in re.findall(r"^CSI_VALUE (\S+) ([0-9a-f]{8})$", result, re.M)
        }
        for key, expected in {
            "gain_plus6": 10 ** (-6 / 20),
            "gain_minus6": 10 ** (6 / 20),
            "wander": 0.6,
        }.items():
            if key not in values or not math.isclose(
                values[key], expected, rel_tol=1e-6, abs_tol=1e-6
            ):
                raise ValueError("host numerical oracle mismatch: " + key)
        for c in range(1, 4):
            if not math.isclose(
                values[f"motion{c}"], values["motion0"] * (c + 1), rel_tol=1e-6
            ):
                raise ValueError("motion subcarrier ratio")
        if not math.isclose(
            values["52carrier_last"], 52 * values["52carrier_first"], rel_tol=1e-6
        ):
            raise ValueError("52-carrier ratio")
        if (
            "invalid args to get_rx_gain" not in result
            or "agc_gain(median): 20, fft_gain@median: 4" not in result
        ):
            raise ValueError("actual SDK logging missing")
    if re.findall(r"^CSI_NSH_STATUS_(\d+)$", status, re.M) != [
        "1" if negative else "0"
    ]:
        raise ValueError("wrong NSH status")


def run(qemu, image, output, negative=False, numerical=False, nonfinite=False):
    if sum((negative, numerical, nonfinite)) > 1:
        raise ValueError("select one witness case")
    command = [
        str(qemu),
        "-machine",
        "esp32s3",
        "-smp",
        "2",
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-no-reboot",
        "-snapshot",
        "-drive",
        f"file={image},if=mtd,format=raw",
    ]
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    transcript = bytearray()

    def prompt(timeout=45):
        start = len(transcript)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError(f"QEMU console EOF: {process.poll()}")
                transcript.extend(chunk)
                if len(transcript) > 1024 * 1024:
                    raise RuntimeError("QEMU transcript exceeded bound")
                if transcript[start:].endswith((b"nsh> ", b"nsh> \x1b[K")):
                    return transcript[start:].decode(errors="replace")
        raise TimeoutError("QEMU console deadline exceeded")

    def send(text):
        process.stdin.write((text + "\n").encode())
        process.stdin.flush()

    try:
        prompt()
        send(
            "csikernel"
            + (
                " fail"
                if negative
                else " numerics" if numerical else " nonfinite" if nonfinite else ""
            )
        )
        result = prompt()
        send("echo CSI_NSH_STATUS_$?")
        status = prompt()
        if nonfinite:
            verify_nonfinite(result, status)
        elif numerical:
            verify_numerical(result, status)
        else:
            verify(result, status, negative)
        return {
            "command": command,
            "negative": negative,
            "numerical_failure_witness": numerical,
            "nonfinite_output_witness": nonfinite,
            "checks": (
                0
                if negative
                else (
                    48
                    if nonfinite
                    else NUMERICAL_CHECKS if numerical else EXPECTED_CHECKS
                )
            ),
            "passed": True,
            "image_sha256": before,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        output.write_bytes(transcript)
        if hashlib.sha256(image.read_bytes()).hexdigest() != before:
            raise RuntimeError("input flash changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--qemu", required=True, type=Path)
    args = parser.parse_args()
    build = args.build.resolve()
    qemu = args.qemu.resolve()
    receipt = json.loads((build / "csi-build.json").read_text())
    if receipt["mode"] != "kernel" or receipt["pins"] != json.loads(
        (HERE / "pins.json").read_text()
    ):
        parser.error("requires this pinned kernel build receipt")
    if (
        receipt["fixture_sha256"]
        != hashlib.sha256((HERE / "kernel.c").read_bytes()).hexdigest()
    ):
        parser.error("kernel source differs from build receipt")
    for relative, digest in receipt["source_sha256"].items():
        if (
            hashlib.sha256((HERE.parents[1] / relative).read_bytes()).hexdigest()
            != digest
        ):
            parser.error("kernel witness input differs from build receipt")
    image = build / "nuttx/nuttx.merged.bin"
    if receipt["image_sha256"] != hashlib.sha256(image.read_bytes()).hexdigest():
        parser.error("image hash differs from receipt")
    version = subprocess.check_output([str(qemu), "--version"], text=True).splitlines()[
        0
    ]
    if "esp_develop_9.0.0_20240606" not in version:
        parser.error("use the pinned Espressif QEMU 9.0.0_20240606 bundle")
    results = []
    for i in range(3):
        results.append(run(qemu, image, build / f"kernel-pass-{i+1}.log"))
    results.append(run(qemu, image, build / "kernel-negative.log", True))
    for i in range(3):
        results.append(
            run(qemu, image, build / f"numerical-pass-{i+1}.log", numerical=True)
        )
    for i in range(3):
        results.append(
            run(qemu, image, build / f"nonfinite-pass-{i+1}.log", nonfinite=True)
        )
    report = {
        "scope": "kernel runtime only; PHY calls are recording stubs; synthetic inputs",
        "hardware_qualified": False,
        "emulator": version,
        "pins": receipt["pins"],
        "runs": results,
    }
    (build / "csi-qemu.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        "PASS: three fresh boots x 818 legacy checks, negative control, three x 398 numerical checks, and three x 48 nonfinite-output checks; RF remains unqualified"
    )


if __name__ == "__main__":
    main()
