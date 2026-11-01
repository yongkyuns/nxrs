#!/usr/bin/env python3
"""One-time USB commissioning; the Rust gateway owns enrollment/time afterwards."""
import argparse
import os
from pathlib import Path
import stat
import struct
import time

from serve import RUN, Session
from tls_provision import _line


def provision(console, packet):
    if not 77 <= len(packet) <= 269 or packet[:4] != b'NXC1':
        raise ValueError('invalid commissioning packet')
    pending = bytearray()
    deadline = time.monotonic() + 120
    while not _line(console, deadline, pending).startswith('NXRS_BOOTSTRAP_PROVISION_READY '):
        pass
    deadline = time.monotonic() + 25
    if console.write(struct.pack('>H', len(packet))) != 2:
        raise RuntimeError('incomplete commissioning header')
    for offset in range(0, len(packet) + 32, 32):
        offset = min(offset, len(packet))
        expected = f'NXRS_BOOTSTRAP_INPUT_ACK offset={offset}'
        if _line(console, deadline, pending) != expected:
            raise RuntimeError('commissioning acknowledgement failed')
        if offset == len(packet):
            break
        chunk = packet[offset:offset + 32]
        if console.write(chunk) != len(chunk):
            raise RuntimeError('incomplete commissioning write')
        console.flush()


def main():
    import serial
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--commission', type=Path, required=True)
    parser.add_argument('--wifi-file', required=True)
    args = parser.parse_args()
    credentials = RUN.read_wifi_file(args.wifi_file)
    with os.fdopen(os.open(args.commission, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
            raise ValueError('commissioning packet must be a private regular file')
        packet = file.read(270)
    console = serial.Serial()
    console.port, console.baudrate, console.timeout = args.port, 115200, .1
    console.write_timeout = 5
    console.dtr = console.rts = False
    console.exclusive = True
    console.open()
    with console:
        provision(console, packet)
        Session(console, credentials, None, lambda *_args, **_kwargs: None, tls=True).start(autostart=True)
    print('Commissioning stored; further enrollment and time use the Rust gateway.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Commissioning failed; credential and device output withheld.') from None
