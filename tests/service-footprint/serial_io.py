#!/usr/bin/env python3
"""Small POSIX serial helpers shared by local NSH measurements."""
import os
import select
import termios
import time


PROMPT = b'nsh> '


def set_serial(fd):
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


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
