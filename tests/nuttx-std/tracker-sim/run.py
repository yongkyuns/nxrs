#!/usr/bin/env python3
"""Run the tracker device qualification firmware in accelerated NuttX/QEMU."""
import argparse
import os
from pathlib import Path
import re
import select
import socket
import struct
import subprocess
import tempfile
import time

from sensors import NeoM9n

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
        self.dials = 0
        self.terminated = False
        self.terminate_ack = False

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
            self.dials += 1
            if self.dials == 1:
                self.wire.sendall(b'\r\nNO CARRIER\r\n')
                return
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
            self.ipv4(body)
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
        elif protocol == 0xc021 and code == 6:
            assert self.terminated and identifier == TERMINATE_ID and not self.terminate_ack
            self.terminate_ack = True
        else:
            assert code == 2 and protocol in (0xc021, 0x8021), packet.hex()

    def terminate_link(self):
        assert self.data_mode and not self.terminated
        self.terminated = True
        self.wire.sendall(control(0xc021, 5, TERMINATE_ID))

    def ipv4(self, body):
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


def run(args, scenario=None):
    transcript = bytearray()
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
                            (scenario or run_guest)(process, modem_wire, gnss_wire, transcript, args.shift, log)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def run_guest(process, modem_wire, gnss_wire, transcript, shift, log):
    stamp = Stamp(modem_wire)
    gnss = NeoM9n(gnss_wire)
    console = process.stdout.fileno()
    cursor = 0
    for wire in (modem_wire, gnss_wire):
        wire.settimeout(1)

    def until(pattern):
        nonlocal cursor
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and len(transcript) <= 1024 * 1024:
            if match := re.search(pattern, transcript[cursor:], re.M):
                cursor += match.end()
                return match.groups()
            ready, _, _ = select.select((console, modem_wire, gnss_wire), (), (), 0.05)
            if console in ready:
                chunk = os.read(console, 8192)
                if not chunk:
                    raise RuntimeError('QEMU console closed')
                chunk = chunk.replace(b'\r', b'')
                transcript.extend(chunk)
                log.write(chunk)
                log.flush()
            for wire, model in ((modem_wire, stamp), (gnss_wire, gnss)):
                if wire in ready:
                    data = wire.recv(4096)
                    if not data:
                        raise RuntimeError('QEMU UART closed')
                    model.receive(data)
            if not stamp.terminated and b'\nCELLULAR_UDP 1 PASS\n' in transcript:
                assert stamp.echoes == [b'nxrs\x01']
                stamp.terminate_link()
            # The provider consumes UART bytes while reading back its setup.
            # Send the test fix once the guest has opened the event stream.
            if not gnss.fix_sent and b'\nTRACKER_GNSS_READY\n' in transcript:
                gnss.send_fix()
        raise TimeoutError(f'NuttX console did not produce {pattern!r}')

    def send(command):
        process.stdin.write(command + b'\n')
        process.stdin.flush()

    until(rb'nsh> ')
    start = time.monotonic()
    send(b'tracker_sim_qualification')
    guest_ms, = until(rb'^CELLULAR_CLOCK (\d+)\n')
    elapsed = time.monotonic() - start
    until(rb'nsh> ')
    send(b'echo NXRS_STATUS_$?')
    status, = until(rb'^NXRS_STATUS_(\d+)\n')
    suite_elapsed = time.monotonic() - start
    assert status == b'0', (transcript.decode(errors='replace'), gnss.writes, stamp.commands, stamp.negotiation)
    assert not re.search(rb'PANIC|panicked at|Assertion failed', transcript), transcript
    assert re.findall(rb'^TRACKER_DEVICES PASS$', transcript, re.M) == [b'TRACKER_DEVICES PASS']
    assert re.findall(rb'^CELLULAR_UDP (\d) PASS$', transcript, re.M) == [b'1', b'2']
    assert re.findall(rb'^CELLULAR (.+) PASS$', transcript, re.M) == [b'REJECTED', b'LINK LOST']
    assert stamp.retried and stamp.terminate_ack, stamp.negotiation
    assert stamp.echoes == [b'nxrs\x01', b'nxrs\x02'], stamp.echoes
    expected = [b'AT', b'ATE0', b'AT+CGDCONT=1,"IP","nxrs.test"', b'ATD*99#', b'ATH']
    assert stamp.commands == expected[:-1] + expected * 2, stamp.commands
    assert gnss.fix_sent and gnss.writes == [1, 7], gnss.writes
    if shift is not None:
        assert elapsed < int(guest_ms) / 1000, (guest_ms, elapsed)
    print(f'PASS: tracker devices, refused dial, PPP link loss and reconnection in {suite_elapsed:.3f} s on host '
          f'({int(guest_ms)} ms NuttX sleep in {elapsed:.3f} s)')


def parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('image', type=Path)
    parser.add_argument('--qemu', default='qemu-system-xtensa')
    parser.add_argument('--qemu-data', type=Path, help='QEMU firmware directory for a source build')
    clock = parser.add_mutually_exclusive_group()
    clock.add_argument('--shift', type=int, choices=range(11), default=7)
    clock.add_argument('--wall-clock', action='store_true')
    parser.add_argument('--log', type=Path, default=Path('target/tracker-sim/console.log'))
    return parser


def main():
    args = parser(__doc__).parse_args()
    if args.wall_clock:
        args.shift = None
    run(args)


if __name__ == '__main__':
    main()
