"""Qualify one IMU, GNSS and optional log rotation without a display."""
import argparse
import json
import os
from pathlib import Path
import re
from select import select
import subprocess
import tempfile
import time

from run import listener
from sensors import NeoM9n, check_fix


class Guest:
    def __init__(self, process, receiver):
        self.process = process
        self.receiver = receiver
        self.console = bytearray()

    def wait_for(self, text, offset=0):
        deadline = time.monotonic() + 15
        while (position := self.console.find(text, offset)) < 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select([self.process.stdout, self.receiver.wire], [], [], remaining)
            for stream in readable:
                chunk = os.read(stream.fileno(), 4096)
                assert chunk, self.console[-4000:].decode(errors='replace')
                if stream is self.process.stdout:
                    self.console.extend(chunk)
                else:
                    self.receiver.receive(chunk)
        assert position >= 0, f'missing {text!r}\n{self.console[-4000:].decode(errors="replace")}'
        return position

    def command(self, command):
        start = len(self.console)
        self.process.stdin.write(command + b'\n')
        self.process.stdin.flush()
        return start

    def shell(self, command):
        start = self.command(command)
        end = self.wait_for(b'nsh> ', start)
        return self.console[start:end]

    def tracker(self, archive):
        command = b'motion_tracker --frames 5 --stream-usb'
        if archive:
            command += b' --archive /logs/m 1024'
        start = self.command(command)
        self.wait_for(b'"gnss":{"state":"waiting"}', start)
        self.receiver.fix_sent = False
        self.receiver.send_fix()
        summary = self.wait_for(b'IMU 1 live, IMU 2 unavailable, GNSS live', start)
        self.wait_for(b'nsh> ', summary)
        assert b'tracker-status:0' in self.shell(b'echo tracker-status:$?').splitlines()
        records = [line for line in self.console[start:summary].splitlines()
                   if line.startswith(b'{"v":2,')]
        samples = [json.loads(line) for line in records]
        assert len(samples) == 5 and all(len(frame['imu']) == 1 for frame in samples), samples
        assert samples[0]['gnss'] == {'state': 'waiting'}, samples
        check_fix(samples[-1])
        live = [frame['imu'][0] for frame in samples if frame['imu'][0]['state'] == 'live']
        sequences = [reading['seq'] for reading in live]
        assert len(live) >= 3 and sequences == sorted(set(sequences)), samples
        assert all(reading['accel_mps2'][0] < -9 for reading in live), samples
        return live, b'\n'.join(records) + b'\n'

    def read_archive(self, path):
        data = self.shell(f'hexdump {path}'.encode())
        rows = re.findall(rb'^[\da-f]{4}: ([\da-f ]{48})', data, re.M | re.I)
        return bytes.fromhex(b''.join(rows).decode())

    def check_archive(self):
        self.shell(b'usleep 10000')
        log = self.read_archive('/logs/system.log')
        assert log.count(b'log witness: BMI270 configured') == 2, log
        assert log.count(b'[INFO navigation::observations] configured 2 IMU channels, GNSS enabled') == 2, log
        lines = [f'route-{index:02d}-{"x" * 25}'.encode() for index in range(40)]
        for line in lines:
            self.shell(b'echo ' + line + b' >/dev/log')
        self.shell(b'usleep 10000')
        previous = self.read_archive('/logs/system.log.1')
        active = self.read_archive('/logs/system.log')
        assert len(previous) == 512 and 0 < len(active) <= 512
        retained = (previous + active).replace(b'\r', b'')
        assert retained.endswith(b'\n'.join(lines[-12:]) + b'\n') and lines[0] not in retained, retained
        return {'bootMarker': True, 'sensorMarkers': 2, 'serviceMarkers': 2, 'emittedLines': len(lines),
                'previousBytes': len(previous), 'activeBytes': len(active)}

    def check_measurements(self, published):
        previous = self.read_archive('/logs/m.1')
        active = self.read_archive('/logs/m')
        assert len(previous) == 1024 and 0 < len(active) <= 1024
        retained = previous + active
        assert retained == published[-len(retained):]
        return {'previousBytes': len(previous), 'activeBytes': len(active)}


def run(image, qemu, has_archive):
    with tempfile.TemporaryDirectory(prefix='nxrs-gnss-', dir='/tmp') as directory:
        uart = Path(directory) / 'gnss'
        command = [qemu, '-machine', 'esp32s3', '-smp', '2', '-display', 'none',
                   '-serial', 'stdio', '-serial', f'unix:{uart}', '-monitor', 'none',
                   '-nic', 'none', '-no-reboot', '-snapshot',
                   '-drive', f'file={image.resolve()},if=mtd,format=raw']
        with listener(uart) as server, subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ) as process:
            try:
                wire, _ = server.accept()
                with wire:
                    guest = Guest(process, NeoM9n(wire))
                    guest.wait_for(b'nsh> ')
                    if has_archive:
                        guest.shell(b'usleep 10000')
                        boot = guest.read_archive('/logs/system.log')
                        assert b'log witness: before tracker and archive task' in boot, boot
                    first, first_bytes = guest.tracker(has_archive)
                    restarted, restarted_bytes = guest.tracker(has_archive)
                    result = {'first': first, 'restarted': restarted}
                    assert guest.receiver.writes == [1, 7, 1, 7], guest.receiver.writes
                    if has_archive:
                        result['measurements'] = guest.check_measurements(first_bytes + restarted_bytes)
                        result['archive'] = guest.check_archive()
                    return result
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--qemu', default='qemu-system-xtensa')
    args = parser.parse_args()
    nuttx = args.image.parent
    config = set((nuttx / '.config').read_text().splitlines())
    receipt = json.loads((nuttx.parent / 'provider-selection.json').read_text())
    assert {'CONFIG_I2C_DRIVER=y', 'CONFIG_ESP32S3_UART1=y'} <= config
    assert not any(f'CONFIG_{name}=y' in config for name in ('ESP32S3_UART2', 'INPUT', 'SPI', 'MTD', 'NET_BLUETOOTH'))
    assert {provider['capability'] for provider in receipt['providers']} == {'imu', 'gnss'}
    devices = receipt['resolved-composition']['graph']['devices']
    assert sorted(device['compatible'] for device in devices.values()) == ['bosch,bmi270', 'u-blox,neo-m9n']
    has_archive = receipt['platform'] == 'esp32s3-qemu-tracker-logging'
    if has_archive:
        assert {'CONFIG_RAMLOG_SYSLOG=y', '# CONFIG_RAMLOG_NONBLOCKING is not set', 'CONFIG_FS_TMPFS=y'} <= config
        assert 'CONFIG_SYSLOG_FILE=y' not in config
    result = run(args.image, args.qemu, has_archive)
    args.report.write_text(json.dumps({'image': str(args.image), 'devices': list(devices), **result}, indent=2) + '\n')
    print('PASS: one live IMU, GNSS fix acquisition and two clean exits without a display')
    if has_archive:
        print('PASS: rotating measurement archive matches the published IMU and GNSS bytes')
        print('PASS: boot, driver and observation service logs reached the archive and rotated')


if __name__ == '__main__':
    main()
