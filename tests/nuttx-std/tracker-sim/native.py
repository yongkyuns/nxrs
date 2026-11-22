#!/usr/bin/env python3
"""Check tracker sensor lifetimes and failure recovery in native NuttX sim."""
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

from sensors import NeoM9n, check_fix, check_imus, check_live, check_progress, check_stale


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
        self.pause_fixes = False

    def record(self, sample):
        self.samples.append(sample)
        if self.pause_fixes:
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

    def tracker(self, scenario, frames, *, pause_fixes=False, status=0):
        self.samples.clear()
        self.receiver = None
        self.live = self.stale = False
        self.pause_fixes = pause_fixes
        if scenario:
            self.until(b'nsh> ', self.command(b'set TRACKER_SIM_CASE ' + scenario))
        else:
            self.until(b'nsh> ', self.command(b'unset TRACKER_SIM_CASE'))
        start = self.command(f'motion_tracker --frames {frames} --interval-ms 500 --stream-usb'.encode())
        self.until(b'nsh> ', start, timeout=20)
        result = self.command(b'echo tracker-exit:$?')
        self.until(b'nsh> ', result)
        assert f'tracker-exit:{status}\n'.encode() in self.console[result:], self.console[result:]
        assert len(self.samples) == frames, self.samples
        assert self.receiver is not None and self.receiver.writes == [1, 7], (
            None if self.receiver is None else self.receiver.writes
        )
        self.close_uart()
        return self.console[start:result]


def run(image, log):
    with subprocess.Popen([str(image.resolve())], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        guest = Guest(process, log)
        try:
            guest.until(b'nsh> ', 0)
            for scenario in (b'', b'imu', b'queue', b''):
                transcript = guest.tracker(scenario, 12 if not scenario else 9,
                                           pause_fixes=not scenario, status=int(scenario == b'imu'))
                tail = guest.samples[-3:] if scenario else guest.samples[-2:]
                if scenario == b'imu':
                    first_fault = next((i for i, sample in enumerate(guest.samples)
                                        if sample['imu'][0]['state'] == 'fault'), len(guest.samples))
                    assert first_fault <= len(guest.samples) - len(tail), guest.samples
                    assert any(sample['imu'][0]['state'] == 'live'
                               for sample in guest.samples[:first_fault]), guest.samples
                    assert b'motion-tracker IMU 1: Io\n' in transcript, transcript
                    for sample in guest.samples[first_fault:]:
                        assert sample['imu'][0] == {'state': 'fault'}, sample
                        assert sample['imu'][1]['state'] == 'live', sample
                        assert sample['imu'][1]['accel_mps2'][0] > 0, sample
                    for sample in tail:
                        check_fix(sample)
                    print('PASS: primary IMU fails while secondary IMU and GNSS keep reporting', flush=True)
                else:
                    if scenario == b'queue':
                        before, marker, after = transcript.partition(b'TRACKER_PANEL_STALL\n')
                        assert marker, transcript
                        drop_line = rb'motion-tracker (IMU [12]|GNSS) queue drops: ([1-9]\d*)\n'
                        baseline = {name: int(count) for name, count in re.findall(drop_line, before)}
                        drops = list(re.finditer(drop_line, after))
                        assert any(int(drop[2]) > baseline.get(drop[1], 0) for drop in drops), transcript
                        assert after[drops[-1].end():].count(b'{"v":1,') > len(tail), transcript
                        print('PASS: tracker inbox drops samples during a stalled paint and recovers', flush=True)
                    else:
                        assert guest.stale, guest.samples
                    for sample in tail:
                        check_live(sample)
                check_progress(tail)
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
