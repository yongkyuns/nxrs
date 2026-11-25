#!/usr/bin/env python3
"""Exercise the real NuttX IMU provider against the emulated BMI270."""
import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run import run_cases


REPORT = "IMU_QEMU PASS samples=12 overflow=1 reopens=2"


def verify(text, status, _mode):
    assert not re.search(r"panic|assertion failed", text, re.IGNORECASE), text
    reports = [line for line in text.splitlines() if line.startswith("IMU_QEMU ")]
    assert reports == [REPORT], text
    assert re.findall(r"^NXRS_STATUS_([0-9]+)\s*$", status, re.M) == ["0"], status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    kernel = args.output / "nuttx/nuttx"
    flash = kernel.with_name("nuttx.merged.bin")
    if not kernel.is_file() or not flash.is_file():
        parser.error("output must contain the built kernel and merged flash image")
    run_cases(kernel, args.output, [("bmi270", "imu_qemu")], verify, esp32_image=flash)
