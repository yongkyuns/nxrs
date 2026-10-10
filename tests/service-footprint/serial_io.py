#!/usr/bin/env python3
"""Small POSIX serial helpers shared by local NSH measurements."""
import os
import select
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.device import set_serial


PROMPT = b'nsh> '


def read_prompt(fd, timeout):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], min(0.1, deadline - time.monotonic()))
        if ready:
            try:
                data.extend(os.read(fd, 4096))
            except BlockingIOError:
                pass
            if PROMPT in data:
                return bytes(data)
    raise TimeoutError(f'timed out waiting for NSH prompt; received {data[-1000:]!r}')


def command(fd, text, timeout):
    os.write(fd, text.encode() + b'\r')
    return read_prompt(fd, timeout)
