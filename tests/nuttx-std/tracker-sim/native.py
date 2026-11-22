#!/usr/bin/env python3
"""Run both tracker IMUs and the GNSS receiver in native NuttX sim."""
import argparse
import errno
import json
import os
from pathlib import Path
import re
import select
import subprocess
import termios
import time
import tty

from sensors import NeoM9n, check_imus, check_live, check_stale


class Pty:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # The guest can send its first UBX command before the peer opens.
            tty.setraw(self.fd, termios.TCSANOW)
        except BaseException:
            os.close(self.fd)
            raise

    def sendall(self, data):
        assert os.write(self.fd, data) == len(data), 'short GNSS peer write'


class Guest:
    def __init__(self, process, log):
        self.process = process
        self.log = log
        self.console = bytearray()
        self.pending = bytearray()
        self.uart = None
        self.receiver = None
        self.samples = []
        self.live = False
        self.stale = False

    def record(self, sample):
        self.samples.append(sample)
        if not self.live and sample['gnss']['state'] == 'live':
            check_live(sample)
            self.live = True
        if self.live and not self.stale:
            # Pause fixes while both IMUs continue to produce samples.
            if sample['gnss']['state'] != 'stale':
                check_imus(sample)
                return
            check_stale(sample)
            self.stale = True
        if self.uart is not None and self.receiver.rate == 1:
            tow_ms = 500 * len(self.samples)
            self.uart.sendall(self.receiver.fix_packet(tow_ms))

    def read_console(self):
        chunk = os.read(self.process.stdout.fileno(), 8192)
        if not chunk:
            raise RuntimeError('NuttX sim console closed')
        self.log.write(chunk)
        self.log.flush()
        chunk = chunk.replace(b'\r', b'')
        self.console.extend(chunk)
        self.pending.extend(chunk)
        while b'\n' in self.pending:
            end = self.pending.index(b'\n')
            line = bytes(self.pending[:end])
            del self.pending[:end + 1]
            if match := re.search(rb'/dev/ttySIM0 connected to pseudotty: (\S+)$', line):
                path = match[1]
                assert re.fullmatch(rb'/dev/(?:pts/\d+|ttys[0-9a-z]+)', path), path
                assert self.receiver is None, 'GNSS UART reopened during one tracker run'
                self.uart = Pty(os.fsdecode(path))
                self.receiver = NeoM9n(self.uart)
            if (start := line.find(b'{"v":1,')) >= 0:
                self.record(json.loads(line[start:]))

    def until(self, marker, offset, timeout=10):
        deadline = time.monotonic() + timeout
        while marker not in self.console[offset:]:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(self.console) > 1024 * 1024:
                raise TimeoutError(f'NuttX sim did not produce {marker!r}')
            console = self.process.stdout.fileno()
            uart = self.uart.fd if self.uart else None
            fds = [console] if uart is None else [console, uart]
            ready, _, _ = select.select(fds, [], [], remaining)
            if console in ready:
                self.read_console()
            if uart in ready:
                try:
                    packet = os.read(uart, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    packet = b''
                if packet:
                    self.receiver.receive(packet)
                else:
                    self.close_uart()

    def command(self, command):
        offset = len(self.console)
        self.process.stdin.write(command + b'\n')
        self.process.stdin.flush()
        return offset

    def close_uart(self):
        if self.uart is not None:
            os.close(self.uart.fd)
            self.uart = None


def run(image, log):
    with subprocess.Popen([str(image.resolve())], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        guest = Guest(process, log)
        try:
            guest.until(b'nsh> ', 0)
            for _ in range(2):
                guest.samples.clear()
                guest.receiver = None
                guest.live = guest.stale = False
                start = guest.command(b'motion_tracker --frames 12 --interval-ms 500 --stream-usb')
                guest.until(b'nsh> ', start)
                status = guest.command(b'echo tracker-exit:$?')
                guest.until(b'nsh> ', status)
                assert b'tracker-exit:0\n' in guest.console[status:], guest.console[status:]
                assert guest.receiver is not None and guest.receiver.writes == [1, 7], (
                    None if guest.receiver is None else guest.receiver.writes
                )
                assert len(guest.samples) == 12 and guest.stale, guest.samples
                previous, latest = guest.samples[-2:]
                check_live(previous)
                check_live(latest)
                assert latest['gnss']['seq'] > previous['gnss']['seq']
                guest.close_uart()
            assert not re.search(rb'panicked at|Assertion failed|tracker simulator initialization failed', guest.console)
            guest.command(b'poweroff')
            assert process.wait(timeout=5) == 0
        finally:
            guest.close_uart()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print('PASS: two native tracker runs with both IMUs, GNSS timeout and recovery, and receiver settings restored')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--log', type=Path, default=Path('target/nuttx-sim-tracker/console.log'))
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('wb') as log:
        run(args.image, log)


if __name__ == '__main__':
    main()
