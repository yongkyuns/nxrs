"""Shared host-side serial and complete firmware restore helpers."""
import os
from pathlib import Path
import re
import select
import subprocess
import termios
import time


def set_serial(fd):
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def marker_rows(transcript, marker, count):
    rows = []
    for line in transcript.replace('\r', '').splitlines():
        if line.startswith(marker + ' '):
            fields = dict(re.findall(r'([a-z0-9_]+)=([^\s]+)', line))
            rows.append({key: int(value) if value.isdecimal() else value
                         for key, value in fields.items()})
    if len(rows) != count:
        raise ValueError(f'{marker}: expected {count} rows, found {len(rows)}')
    return rows


def open_serial(port, *, set_serial_fn=None):
    """Open/configure a serial device with the established 40-second retry."""
    configure = set_serial if set_serial_fn is None else set_serial_fn
    deadline = time.monotonic() + 40
    while True:
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)
            continue
        try:
            configure(fd)
        except OSError:
            os.close(fd)
            raise
        return fd


def read_prompt(fd, timeout, prompt=b"zephyr> "):
    """Read until a prompt arrives, retaining the existing bounded wait behavior."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.1)
        if readable:
            try:
                data.extend(os.read(fd, 4096))
            except BlockingIOError:
                continue
            if prompt in data:
                return bytes(data)
    raise TimeoutError(f"{prompt!r} prompt missing: {data[-1200:]!r}")


def restore(flasher, port, backup, out):
    """Restore and verify the complete backed-up image, preserving its header."""
    for name, args in (("restore.log", ["write_flash", "--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep"]),
                       ("restore-verify.log", ["verify_flash"])):
        result = subprocess.run([flasher, "--chip", "esp32s3", "--port", port, "--baud", "460800",
                                 *args, "0x0", str(backup)], capture_output=True, text=True, timeout=600)
        (Path(out) / name).write_text(result.stdout + result.stderr)
        result.check_returncode()
