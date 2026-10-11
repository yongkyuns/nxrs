#!/usr/bin/env python3
"""Enter BOOTSEL using an opt-in NuttX/Pico 1200-baud USB reset hook."""

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def in_bootloader(picotool, chip_serial):
    try:
        result = subprocess.run(
            [str(picotool), "info", "--ser", chip_serial],
            capture_output=True, text=True, timeout=2,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and "RP2350" in result.stdout


def enter_bootloader(port, picotool, chip_serial, timeout=10):
    # Check the exact ROM identity first; don't touch another serial device if
    # the requested board is already in BOOTSEL. Never invoke picotool -f.
    if in_bootloader(picotool, chip_serial):
        return
    import serial

    touch_error = None
    try:
        with serial.Serial(port, baudrate=1200, timeout=0.2, write_timeout=0.2) as device:
            device.dtr = False
    except serial.SerialException as error:
        # A successful reset can make the port disappear during open/close.
        # Only the exact ROM enumeration below can turn this into success.
        touch_error = error
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if in_bootloader(picotool, chip_serial):
            return
        time.sleep(0.2)
    raise RuntimeError("Requested Pico did not enter BOOTSEL; USB/timer handling "
                       "may be stuck or the reset hook may not be installed") from touch_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="Explicit Pico USB serial port")
    parser.add_argument("--serial", required=True, help="16-digit chip serial from picotool info -a")
    parser.add_argument("--picotool", type=Path,
                        default=shutil.which("picotool") or
                        ROOT / "target/radio-deps/picotool-build/picotool")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{16}", args.serial):
        parser.error("--serial must be a 16-digit hexadecimal Pico chip serial")
    if not args.picotool.is_file():
        parser.error("picotool not found; provide --picotool PATH")
    try:
        enter_bootloader(args.port, args.picotool, args.serial.upper())
    except (RuntimeError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print("BOOTSEL confirmed for the requested RP2350")


if __name__ == "__main__":
    main()
