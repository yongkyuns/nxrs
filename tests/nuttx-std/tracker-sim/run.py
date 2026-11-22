#!/usr/bin/env python3
"""Run tracker device or app cellular qualification in NuttX/QEMU."""
import argparse
import json
import os
from pathlib import Path
import re
import select
import socket
import struct
import subprocess
import tempfile
import time

from sensors import NeoM9n, check_live, check_progress

FLAG = 0x7E
ADDRESS = bytes((192, 0, 2, 2))
PEER = bytes((192, 0, 2, 1))
TERMINATE_ID = 203


def fcs(data):
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x8408 if value & 1 else 0)
    return value


def frame(protocol, payload):
    body = b'\xff\x03' + struct.pack('!H', protocol) + payload
    encoded = bytearray((FLAG, 0xff))
    for byte in body[1:] + struct.pack('<H', fcs(body) ^ 0xffff):
        if byte < 32 or byte in (FLAG, 0x7d):
            encoded.extend((0x7d, byte ^ 0x20))
        else:
            encoded.append(byte)
    encoded.append(FLAG)
    return bytes(encoded)


def control(protocol, code, identifier, options=b''):
    return frame(protocol, struct.pack('!BBH', code, identifier, len(options) + 4) + options)


def checksum(data):
    value = sum(struct.unpack('!10H', data))
    return 0xffff ^ ((value & 0xffff) + (value >> 16))


class Stamp:
    def __init__(self, wire):
        self.wire = wire
        self.buffer = bytearray()
        self.data_mode = False
        self.escape = False
        self.lcp = False
        self.ipcp = False
        self.echoes = []
        self.commands = []
        self.negotiation = []
        self.dropped = None
        self.retried = False
        self.terminated = False

    def terminate_link(self):
        assert self.data_mode and self.lcp and self.ipcp
        self.wire.sendall(control(0xc021, 5, TERMINATE_ID))

    def receive(self, chunk):
        for byte in chunk:
            if len(self.buffer) >= 1600:
                raise ValueError('modem input exceeded bounded record')
            if not self.data_mode:
                if byte not in b'\r\n':
                    self.buffer.append(byte)
                elif self.buffer:
                    self.command(bytes(self.buffer))
                    self.buffer.clear()
                continue
            if byte == FLAG:
                if self.buffer.startswith(b'\xff\x03'):
                    self.packet(bytes(self.buffer))
                self.buffer.clear()
                self.escape = False
            elif byte == 0x7d and not self.escape:
                self.escape = True
            else:
                self.buffer.append(byte ^ 0x20 if self.escape else byte)
                self.escape = False
                if self.buffer == b'+++':
                    self.data_mode = False
                    self.buffer.clear()
                    self.wire.sendall(b'\r\nOK\r\n')

    def command(self, line):
        self.commands.append(line)
        if line == b'ATD*99#':
            self.lcp = self.ipcp = False
            self.data_mode = True
            self.wire.sendall(b'\r\nCONNECT\r\n')
        elif line in [b'AT', b'ATE0', b'AT+CGDCONT=1,"IP","nxrs.test"', b'ATH']:
            self.wire.sendall(b'\r\nOK\r\n')
        else:
            raise ValueError(f'unexpected modem command {line!r}')

    def packet(self, packet):
        assert len(packet) >= 6 and fcs(packet) == 0xf0b8, packet.hex()
        protocol = int.from_bytes(packet[2:4], 'big')
        body = packet[4:-2]
        if protocol == 0x0021:
            self.echo(body)
            return
        code, identifier, length = struct.unpack('!BBH', body[:4])
        assert length == len(body), body.hex()
        self.negotiation.append((hex(protocol), code, identifier))
        if code == 1 and protocol == 0xc021:
            if not self.lcp:
                self.wire.sendall(control(protocol, 1, 201))
                self.lcp = True
            self.wire.sendall(control(protocol, 2, identifier, body[4:]))
        elif code == 1 and protocol == 0x8021:
            if not self.ipcp:
                self.wire.sendall(control(protocol, 1, 202, b'\x03\x06' + PEER))
                self.ipcp = True
            options = body[4:]
            if options[:6] == b'\x03\x06' + ADDRESS:
                if self.dropped is None:
                    self.dropped = (identifier, options)
                    return
                if not self.echoes and (identifier, options) == self.dropped:
                    self.retried = True
                self.wire.sendall(control(protocol, 2, identifier, options))
            else:
                self.wire.sendall(control(protocol, 3, identifier, b'\x03\x06' + ADDRESS))
        elif (protocol, code, identifier) == (0xc021, 6, TERMINATE_ID):
            self.terminated = True
        else:
            assert code == 2 and protocol in (0xc021, 0x8021), packet.hex()

    def echo(self, body):
        assert body[0] == 0x45 and body[9] == 17 and body[12:20] == ADDRESS + PEER, body.hex()
        src, dst, size, _ = struct.unpack('!HHHH', body[20:28])
        payload = body[28:]
        assert dst == 9090 and size == 8 + len(payload), body.hex()
        self.echoes.append(payload)
        udp = struct.pack('!HHHH', dst, src, size, 0) + payload
        header = bytearray(struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + size, 0, 0, 64, 17, 0, PEER, ADDRESS))
        header[10:12] = struct.pack('!H', checksum(header))
        self.wire.sendall(frame(0x0021, header + udp))


def listener(path):
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(path))
        server.listen()
        server.settimeout(8)
    except BaseException:
        server.close()
        raise
    return server


def run(args):
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nxrs-sim-', dir='/tmp') as folder:
        modem_path = Path(folder) / 'modem'
        gnss_path = Path(folder) / 'gnss'
        with listener(modem_path) as modem_socket, listener(gnss_path) as gnss_socket:
            command = [args.qemu, '-machine', 'esp32s3', '-smp', '2', '-display', 'none',
                       '-serial', 'stdio', '-serial', f'unix:{gnss_path}', '-serial', f'unix:{modem_path}',
                       '-monitor', 'none', '-nic', 'none', '-no-reboot', '-snapshot',
                       '-drive', f'file={args.image.resolve()},if=mtd,format=raw']
            if args.qemu_data is not None:
                command.extend(('-L', str(args.qemu_data.resolve())))
            if args.shift is not None:
                command.extend(('-icount', f'shift={args.shift},sleep=off'))
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
            try:
                gnss_wire, _ = gnss_socket.accept()
                with gnss_wire:
                    modem_wire, _ = modem_socket.accept()
                    with modem_wire:
                        with args.log.open("wb") as log:
                            guest = Guest(process, modem_wire, gnss_wire, log)
                            if args.tracker:
                                run_tracker(guest)
                            else:
                                run_devices(guest, args.shift)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


class Guest:
    def __init__(self, process, modem_wire, gnss_wire, log):
        self.process = process
        self.stamp = Stamp(modem_wire)
        self.gnss = NeoM9n(gnss_wire)
        self.modem_wire = modem_wire
        self.gnss_wire = gnss_wire
        self.log = log
        self.transcript = bytearray()
        self.cursor = 0
        for wire in (modem_wire, gnss_wire):
            wire.settimeout(1)

    def until(self, pattern):
        console = self.process.stdout.fileno()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and len(self.transcript) <= 1024 * 1024:
            if match := re.search(pattern, self.transcript[self.cursor:], re.M):
                self.cursor += match.end()
                return match.groups()
            ready, _, _ = select.select((console, self.modem_wire, self.gnss_wire), (), (), 0.05)
            if console in ready:
                chunk = os.read(console, 8192)
                if not chunk:
                    raise RuntimeError('QEMU console closed')
                chunk = chunk.replace(b'\r', b'')
                self.transcript.extend(chunk)
                self.log.write(chunk)
                self.log.flush()
            for wire, model in ((self.modem_wire, self.stamp), (self.gnss_wire, self.gnss)):
                if wire in ready:
                    data = wire.recv(4096)
                    if not data:
                        raise RuntimeError('QEMU UART closed')
                    model.receive(data)
        raise TimeoutError(f'NuttX console did not produce {pattern!r}')

    def send(self, command):
        self.process.stdin.write(command + b'\n')
        self.process.stdin.flush()

    def exit_status(self):
        self.until(rb'nsh> ')
        self.send(b'echo NXRS_STATUS_$?')
        status, = self.until(rb'^NXRS_STATUS_(\d+)\n')
        assert not re.search(rb'PANIC|panicked at|Assertion failed', self.transcript), self.transcript
        return int(status)


AT_SESSION = [b'AT', b'ATE0', b'AT+CGDCONT=1,"IP","nxrs.test"', b'ATD*99#', b'ATH']


def run_devices(guest, shift):
    stamp, gnss, transcript = guest.stamp, guest.gnss, guest.transcript
    guest.until(rb'nsh> ')
    start = time.monotonic()
    guest.send(b'tracker_sim_qualification')
    guest_ms, = guest.until(rb'^CELLULAR_CLOCK (\d+)\n')
    elapsed = time.monotonic() - start
    # The receiver can consume fixes while reading back its setup.
    guest.until(rb'^TRACKER_GNSS_READY\n')
    gnss.send_fix()
    status = guest.exit_status()
    suite_elapsed = time.monotonic() - start
    assert status == 0, (transcript.decode(errors='replace'), gnss.writes, stamp.commands, stamp.negotiation)
    assert re.findall(rb'^TRACKER_DEVICES PASS$', transcript, re.M) == [b'TRACKER_DEVICES PASS']
    assert re.findall(rb'^CELLULAR_UDP (\d) PASS$', transcript, re.M) == [b'1', b'2']
    assert stamp.retried, stamp.negotiation
    assert stamp.echoes == [b'nxrs\x01', b'nxrs\x02'], stamp.echoes
    assert stamp.commands == AT_SESSION * 2, stamp.commands
    assert gnss.fix_sent and gnss.writes == [1, 7], gnss.writes
    if shift is not None:
        assert elapsed < int(guest_ms) / 1000, (guest_ms, elapsed)
    print(f'PASS: tracker devices and two PPP/UDP sessions in {suite_elapsed:.3f} s on host '
          f'({int(guest_ms)} ms NuttX sleep in {elapsed:.3f} s)')


def run_tracker(guest):
    guest.until(rb'nsh> ')
    for lose_link in (True, False):
        frames = 24
        guest.send(f'motion_tracker --cellular-apn nxrs.test --frames {frames} --interval-ms 500 --stream-usb'.encode())
        online = lost = injected = False
        samples = []
        after_loss = []
        while len(samples) < frames:
            report, status = guest.until(rb'^(?:(\{"v":1,.*\})|motion-tracker cellular (.*))\n')
            if status is not None:
                if status == b'Online { address: 192.0.2.2 }':
                    assert not online and not lost, status
                    online = True
                elif status == b'Failed(LinkLost)':
                    assert injected and not lost, status
                    lost = True
                else:
                    assert status == b'Connecting' and not online, status
                continue
            sample = json.loads(report)
            samples.append(sample)
            if lost:
                check_live(sample)
                after_loss.append(sample)
            elif lose_link and online and sample['gnss']['state'] == 'live' and not injected:
                check_live(sample)
                guest.stamp.terminate_link()
                injected = True
            if len(samples) < frames and guest.gnss.rate == 1:
                guest.gnss_wire.sendall(guest.gnss.fix_packet(500 * len(samples)))
        assert online and lost == lose_link, guest.transcript
        assert guest.exit_status() == int(lose_link), guest.transcript
        tail = after_loss if lose_link else samples[-3:]
        assert len(tail) >= 3, guest.transcript
        for sample in tail:
            check_live(sample)
        check_progress(tail)
    assert guest.stamp.terminated and guest.stamp.retried, guest.stamp.negotiation
    assert guest.stamp.commands == AT_SESSION * 2, guest.stamp.commands
    assert guest.gnss.writes == [1, 7, 1, 7], guest.gnss.writes
    print('PASS: tracker reports PPP loss, keeps both IMUs and GNSS live, and restores cellular on restart')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--qemu', default='qemu-system-xtensa')
    parser.add_argument('--qemu-data', type=Path, help='QEMU firmware directory for a source build')
    parser.add_argument('--tracker', action='store_true', help='exercise link loss in the tracker app')
    clock = parser.add_mutually_exclusive_group()
    clock.add_argument('--shift', type=int, choices=range(11), default=7)
    clock.add_argument('--wall-clock', action='store_true')
    parser.add_argument('--log', type=Path, default=Path('target/tracker-sim/console.log'))
    args = parser.parse_args()
    if args.wall_clock:
        args.shift = None
    run(args)


if __name__ == '__main__':
    main()
