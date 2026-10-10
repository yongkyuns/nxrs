#!/usr/bin/env python3
"""Use the existing NuttX harness with byte-paced USB/NSH command input."""
import os
from pathlib import Path
import sys
import time

from measure import load

SERVICE = Path(__file__).resolve().parents[1] / 'service-footprint'
sys.path.insert(0, str(SERVICE))
native = load('embassy_native_measure', SERVICE / 'measure_device.py')


def paced_command(fd, text, timeout):
    # Host pacing is outside the app's CCOUNT timing. Do not retry app errors,
    # change firmware, discard result rows, or alter the payload workload.
    for byte in text.encode() + b'\r':
        if os.write(fd, bytes([byte])) != 1:
            raise OSError('short NSH command write')
        time.sleep(0.005)
    return native.read_prompt(fd, timeout)


native.command = paced_command


if __name__ == '__main__':
    native.main()
