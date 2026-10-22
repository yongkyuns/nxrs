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


def run(qemu, image, output, negative=False):
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
        send("csikernel" + (" fail" if negative else ""))
        result = prompt()
        send("echo CSI_NSH_STATUS_$?")
        status = prompt()
        verify(result, status, negative)
        return {
            "command": command,
            "negative": negative,
            "checks": 0 if negative else EXPECTED_CHECKS,
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
    report = {
        "scope": "kernel runtime only; PHY calls are recording stubs; synthetic inputs",
        "hardware_qualified": False,
        "emulator": version,
        "pins": receipt["pins"],
        "runs": results,
    }
    (build / "csi-qemu.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        "PASS: three fresh boots x 818 checks, deliberate-failure control; RF remains unqualified"
    )


if __name__ == "__main__":
    main()
