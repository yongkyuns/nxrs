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
import tempfile
import termios
import time
import tty

import replay
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
    def __init__(self, process, log, fixture):
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
        self.fail_imu = fixture / 'fail-imu'
        self.stall_panel = fixture / 'stall-panel'
        self.fault_trigger = None

    def record(self, sample):
        self.samples.append(sample)
        if self.fault_trigger is not None and sample['gnss']['state'] in ('live', 'received') and all(
            source['state'] in ('live', 'received') for source in sample['imu']
        ):
            check_live(sample)
            self.fault_trigger.touch()
            self.fault_trigger = None
        if self.pause_fixes:
            if not self.live and sample['gnss']['state'] in ('live', 'received'):
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
            self.on_line(line)

    def on_line(self, line):
        if match := re.search(rb'/dev/ttySIM0 connected to pseudotty: (\S+)$', line):
            path = match[1]
            assert re.fullmatch(rb'/dev/(?:pts/\d+|ttys[0-9a-z]+)', path), path
            assert self.receiver is None, 'GNSS UART reopened during one tracker run'
            self.uart = Pty(os.fsdecode(path))
            self.receiver = NeoM9n(self.uart)
        if (start := line.find(b'{"v":1,')) >= 0:
            self.record(json.loads(line[start:]))

    def pump(self, timeout):
        console = self.process.stdout.fileno()
        uart = self.uart.fd if self.uart else None
        fds = [console] if uart is None else [console, uart]
        ready, _, _ = select.select(fds, [], [], timeout)
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

    def until(self, marker, offset, timeout=10):
        deadline = time.monotonic() + timeout
        while marker not in self.console[offset:]:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(self.console) > 1024 * 1024:
                raise TimeoutError(f'NuttX sim did not produce {marker!r}')
            self.pump(remaining)

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
        self.fail_imu.unlink(missing_ok=True)
        self.stall_panel.unlink(missing_ok=True)
        self.fault_trigger = {b'imu': self.fail_imu, b'paint': self.stall_panel}.get(scenario)
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


class ReplayGuest(Guest):
    def __init__(self, process, log, fixture, recording):
        super().__init__(process, log, fixture)
        self.fixture = fixture
        self.recording = recording
        self.epoch = None
        self.imu_totals = {}

    def record(self, sample):
        # The original USB reports remain in the console log.
        pass

    def on_line(self, line):
        if line.endswith(b'TRACKER_REPLAY_START'):
            assert self.epoch is None, 'replay started twice'
            self.epoch = time.monotonic()
        if match := re.search(rb'TRACKER_REPLAY_IMU_DONE ([01]) (\d+)$', line):
            source = ('imu_secondary', 'imu_primary')[int(match[1])]
            self.imu_totals[source] = int(match[2])
        if b'panicked at' in line or b'Assertion failed' in line or b'tracker simulator initialization failed' in line:
            raise RuntimeError(line.decode(errors='replace'))
        super().on_line(line)

    def play(self):
        self.until(b'nsh> ', self.command(b'set TRACKER_SIM_CASE replay'))
        self.command(b'motion_tracker --interval-ms 100 --stream-usb')
        deadline = time.monotonic() + 10
        ready = self.fixture / 'replay-ready'
        while self.epoch is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('the simulated IMUs and GNSS never became ready for replay')
            self.pump(remaining)
            if self.receiver and self.receiver.rate == 1 and not ready.exists():
                ready.touch()

        origin = self.recording.origin_ms
        end = self.epoch + self.recording.duration_ms / 1000
        remaining_fixes = replay.fixes(self.fixture / replay.SOURCES['gnss'])
        next_fix = next(remaining_fixes, None)
        sent = 0
        try:
            while True:
                now = time.monotonic()
                while next_fix is not None:
                    stamp, latitude, longitude, speed, utc = next_fix
                    elapsed = stamp - origin
                    next_deadline = self.epoch + elapsed / 1000
                    if now < next_deadline:
                        break
                    if self.uart is None or self.receiver.rate != 1:
                        raise RuntimeError('the GNSS receiver stopped during replay')
                    self.uart.sendall(self.receiver.fix_packet(
                        (elapsed + 1000) % 604_800_000, longitude, speed, latitude, utc=utc, replay=True))
                    sent += 1
                    next_fix = next(remaining_fixes, None)
                if next_fix is None and len(self.imu_totals) == 2 and now >= end + 0.2:
                    break
                if now >= end + 5:
                    raise TimeoutError('the simulated IMUs did not drain the recording')
                wake = next_deadline if next_fix is not None else end + (5 if now >= end + 0.2 else 0.2)
                self.pump(max(0, wake - now))
                if len(self.console) > 8192:
                    del self.console[:-4096]
        finally:
            remaining_fixes.close()
        counts = {**self.imu_totals, 'gnss': sent}
        if counts != self.recording.counts:
            raise RuntimeError(f'replay count mismatch {counts}, expected {self.recording.counts}')
        print(f'Replayed {counts["imu_primary"]} primary IMU samples, '
              f'{counts["imu_secondary"]} secondary IMU samples, and {sent} GNSS fixes.')


def run(image, log, recording=None):
    # The guest sees only this temporary directory through its fixture mount.
    with (
        tempfile.TemporaryDirectory(prefix='nxrs-tracker-') as fixture,
        subprocess.Popen([str(image.resolve())], cwd=fixture, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process,
    ):
        fixture = Path(fixture)
        guest = Guest(process, log, fixture)
        try:
            if recording is not None:
                staged = replay.stage(recording, fixture)
                if staged.archive_version == 1:
                    print('Replaying the historical v1 arrival schedule; original capture timing is unknown.', flush=True)
                if not staged.complete:
                    print('Replaying an incomplete recording with its original gaps.', flush=True)
                guest = ReplayGuest(process, log, fixture, staged)
            guest.until(b'nsh> ', 0)
            guest.until(b'nsh> ', guest.command(b'mount -t hostfs -o fs=. /tracker-fixture'))
            status = guest.command(b'echo fixture-mount:$?')
            guest.until(b'nsh> ', status)
            assert b'fixture-mount:0\n' in guest.console[status:], guest.console[status:]
            if recording is not None:
                guest.play()
                return
            for scenario in (b'', b'imu', b'paint', b''):
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
                    if scenario == b'paint':
                        before, marker, after = transcript.partition(b'TRACKER_PANEL_STALL\n')
                        assert marker, transcript
                        first_after = before.count(b'{"v":1,')
                        assert first_after > 0 and after.count(b'{"v":1,') > len(tail), transcript
                        previous, resumed = guest.samples[first_after - 1:first_after + 1]
                        check_live(previous)
                        check_live(resumed)
                        for old, new in zip(previous['imu'], resumed['imu']):
                            assert new['seq'] - old['seq'] >= 80, (old, new)
                            assert new['received_ms'] - old['received_ms'] >= 800, (old, new)
                        assert not re.search(rb'motion-tracker (IMU [12]|GNSS) queue drops:', transcript), transcript
                        print('PASS: both IMUs keep sampling during stalled paint and telemetry resumes fresh',
                              flush=True)
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
    parser.add_argument('--replay', type=Path, help='replay a Motion Tracker NDJSON recording')
    parser.add_argument('--log', type=Path, default=Path('target/nuttx-sim-tracker/console.log'))
    args = parser.parse_args()
    for source in (args.image, args.replay):
        if source is None:
            continue
        if not source.is_file():
            parser.error(f'input file does not exist: {source}')
        if args.log.exists() and args.log.samefile(source):
            parser.error(f'--log refers to an input file: {source}')
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('wb') as log:
        run(args.image, log, args.replay)


if __name__ == '__main__':
    main()
