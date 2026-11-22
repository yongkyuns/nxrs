#!/usr/bin/env python3
"""Run the full tracker against an independent local TLS collector over PPP."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import select
import shlex
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import tempfile
import threading
import time

from run import ADDRESS, Stamp, frame, listener, parser, run
from sensors import NeoM9n, check_live, check_progress

COLLECTOR = '192.0.2.3'
MAC = bytes.fromhex('525400000002')
ROUTER_MAC = bytes.fromhex('5255c0000203')


class Collector(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, context):
        super().__init__(('127.0.0.1', 0), Receive)
        self.context = context
        self.events = queue.Queue()
        self.connections = 0
        self.lock = threading.Lock()


class Receive(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        with self.server.lock:
            self.server.connections += 1
            connection = self.server.connections
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as secure:
                assert secure.version() == 'TLSv1.3'
                self.server.events.put((connection, 'secure', None))
                with secure.makefile('rb') as stream:
                    for raw in iter(stream.readline, b''):
                        assert len(raw) <= 768 and raw.endswith(b'\n'), raw
                        sample = json.loads(raw)
                        self.server.events.put((connection, 'frame', (raw, sample)))
                        # First collector connection closes after its first usable fix.
                        if connection == 1 and is_live(sample):
                            secure.shutdown(socket.SHUT_RDWR)
                            return
        except (ssl.SSLError, OSError) as error:
            self.server.events.put((connection, 'closed', str(error)))


@contextmanager
def collector(directory):
    key = directory / 'key.pem'
    certificate = directory / 'cert.pem'
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
        '-pkeyopt', 'ec_param_enc:named_curve', '-sha256', '-nodes', '-keyout', str(key), '-out', str(certificate), '-days', '1', '-subj', '/CN=tracker-test',
    ], check=True, capture_output=True)
    pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.read_text())).hexdigest()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(certificate, key)
    with Collector(context) as server:
        task = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
        task.start()
        try:
            yield server, pin
        finally:
            server.shutdown()
            task.join()


@contextmanager
def network(directory, qemu, port):
    """Use a restricted QEMU user network solely as the peer TCP stack."""
    path = directory / 'ethernet'
    nc = shlex.quote(shutil.which('nc') or 'nc')
    network = ('user,id=host,net=192.0.2.0/24,host=192.0.2.1,ipv6=off,restrict=on,'
               f'guestfwd=tcp:{COLLECTOR}:{port}-cmd:{nc} 127.0.0.1 {port}')
    with listener(path) as ethernet, (directory / 'network.log').open('wb') as log:
        process = subprocess.Popen([
            qemu, '-machine', 'none', '-nodefaults', '-display', 'none', '-monitor', 'none',
            '-serial', 'none', '-netdev', network,
            '-netdev', f'stream,id=ppp,server=off,addr.type=unix,addr.path={path}',
            '-netdev', 'hubport,id=tohost,hubid=0,netdev=host',
            '-netdev', 'hubport,id=toguest,hubid=0,netdev=ppp',
        ], stdout=log, stderr=subprocess.STDOUT)
        try:
            wire, _ = ethernet.accept()
            with wire:
                wire.settimeout(1)
                yield wire
        finally:
            process.terminate()
            process.wait(timeout=5)


class TcpStamp(Stamp):
    def __init__(self, modem, ethernet, port):
        super().__init__(modem)
        self.ethernet = ethernet
        self.port = port
        self.lost_dial = None
        self.packets = bytearray()

    def terminate_link(self):
        super().terminate_link()
        self.lost_dial = self.dials

    def send_ethernet(self, packet):
        self.ethernet.sendall(struct.pack('!I', len(packet)) + packet)

    def ipv4(self, body):
        assert body[0] == 0x45 and body[9] == socket.IPPROTO_TCP, body.hex()
        assert body[12:20] == ADDRESS + socket.inet_aton(COLLECTOR), body.hex()
        assert int.from_bytes(body[22:24], 'big') == self.port, body.hex()
        self.send_ethernet(ROUTER_MAC + MAC + b'\x08\x00' + body)

    def receive_ethernet(self, data):
        self.packets.extend(data)
        while len(self.packets) >= 4:
            size = int.from_bytes(self.packets[:4], 'big')
            assert 14 <= size <= 1518, size
            if len(self.packets) < 4 + size:
                return
            packet = bytes(self.packets[4:4 + size])
            del self.packets[:4 + size]
            protocol = packet[12:14]
            if protocol == b'\x08\x00':
                length = int.from_bytes(packet[16:18], 'big')
                if self.data_mode and self.ipcp and self.dials != self.lost_dial:
                    self.wire.sendall(frame(0x0021, packet[14:14 + length]))
            elif protocol == b'\x08\x06' and packet[20:22] == b'\0\1' and packet[38:42] == ADDRESS:
                source = packet[22:28]
                self.send_ethernet(source + MAC + b'\x08\x06' + packet[14:20] + b'\0\2'
                                   + MAC + ADDRESS + source + packet[28:32])


def is_live(sample):
    return sample['gnss']['state'] == 'live' and all(imu['state'] == 'live' for imu in sample['imu'])


def qualify(process, modem, gnss_wire, transcript, log, ethernet, server, pin, reject, timeout):
    stamp = TcpStamp(modem, ethernet, server.server_address[1])
    gnss = NeoM9n(gnss_wire)
    console = process.stdout.fileno()
    lines = bytearray()
    usb = []
    received = []
    records = []
    secure_connections = set()
    collector_closures = []
    reached_shell = False
    started = False
    dropped_at_usb = None
    offline_from = None
    redial_at_usb = None
    started_at = time.monotonic()
    for wire in (modem, gnss_wire):
        wire.settimeout(1)

    def send(command):
        process.stdin.write(command.encode() + b'\n')
        process.stdin.flush()

    while time.monotonic() - started_at < timeout and len(transcript) < 512 * 1024:
        ready, _, _ = select.select((console, modem, gnss_wire, ethernet), (), (), 0.025)
        if console in ready:
            chunk = os.read(console, 8192)
            assert chunk, 'QEMU console closed'
            chunk = chunk.replace(b'\r', b'')
            transcript.extend(chunk)
            log.write(chunk)
            log.flush()
            lines.extend(chunk)
            while b'\n' in lines:
                line, _, rest = lines.partition(b'\n')
                lines[:] = rest
                if line.startswith(b'{"v":1,'):
                    usb.append(bytes(line) + b'\n')
                    if gnss.rate == 1 and len(usb) % 4 == 1:
                        gnss_wire.sendall(gnss.fix_packet(len(usb) * 200))
            if not started and b'nsh> ' in transcript:
                send(f'motion_tracker --frames 160 --stream-usb --cellular-apn nxrs.test '
                     f'--collector {COLLECTOR}:{stamp.port} --collector-pin {pin}')
                started = True
            if not reached_shell and b'nsh> ' in transcript[transcript.find(b'nsh> ') + 5:]:
                reached_shell = True
                send('echo NXRS_STATUS_$?')
        for wire, receive in ((modem, stamp.receive), (gnss_wire, gnss.receive),
                              (ethernet, stamp.receive_ethernet)):
            if wire in ready:
                data = wire.recv(4096)
                assert data, 'simulated device connection closed'
                receive(data)
                if wire is modem and stamp.terminate_ack:
                    if offline_from is None:
                        offline_from = len(usb)
                    if stamp.dials > stamp.lost_dial and redial_at_usb is None:
                        redial_at_usb = len(usb)
        while not server.events.empty():
            connection, event, value = server.events.get_nowait()
            if event == 'secure':
                secure_connections.add(connection)
            elif event == 'closed':
                collector_closures.append((connection, value))
            elif event == 'frame':
                assert not reject, 'a wrong pin let telemetry reach the collector'
                raw, sample = value
                records.append(raw)
                if is_live(sample):
                    check_live(sample)
                    if not any(previous == connection for previous, _ in received):
                        received.append((connection, raw))
                        if connection == 2:
                            assert stamp.dials == 2, stamp.commands
                            dropped_at_usb = len(usb)
                            stamp.terminate_link()
                        elif connection == 3:
                            assert stamp.dials == 3 and stamp.terminate_ack, stamp.negotiation
                            assert dropped_at_usb is not None and len(usb) > dropped_at_usb + 2
        if re.search(rb'^NXRS_STATUS_\d+\n', transcript, re.M):
            break

    assert b'\nNXRS_STATUS_0\n' in transcript, transcript[-2000:]
    assert not re.search(rb'PANIC|panicked at|Assertion failed', transcript), transcript[-2000:]
    assert len(usb) == 160 and gnss.writes == [1, 7], (len(usb), gnss.writes)
    assert transcript.count(b"cellular Failed(Rejected)") == 1
    check_live(json.loads(usb[-1]))
    if reject:
        assert len(secure_connections) >= 2, secure_connections
        assert b'collector TLS: Authentication' in transcript
        assert b'collector TLS online' not in transcript
        assert stamp.dials == 2, stamp.commands
        print('PASS: wrong pin rejected across concurrent sensor updates and repeated TLS attempts')
    else:
        assert [connection for connection, _ in received] == [1, 2, 3], (received, secure_connections, collector_closures)
        assert set(records) <= set(usb), "collector received a frame absent from USB"
        positions = [usb.index(raw) for _, raw in received]
        assert positions[0] < positions[1] < positions[2], positions
        assert offline_from is not None and redial_at_usb is not None
        assert dropped_at_usb <= offline_from and positions[2] >= redial_at_usb, positions
        offline = [json.loads(raw) for raw in usb[offline_from:redial_at_usb]]
        # The model sends a GNSS fix every four reports. Observe at least one
        # new fix, with both IMUs live, while the modem is still offline.
        assert len(offline) >= 5, f'only {len(offline)} USB reports while PPP was down'
        for sample in offline:
            check_live(sample)
        check_progress([offline[0], offline[-1], json.loads(received[2][1])])
        assert transcript.count(b'motion-tracker collector TLS online') == 3
        print(f'PASS: {len(offline)} live USB reports while PPP was down, with advancing IMU and GNSS samples')
        print(f'PASS: TLS 1.3 uploads matched console frames {positions} before and after collector/PPP loss '
              f'({time.monotonic() - started_at:.2f} s host time)')


def main():
    cli = parser(__doc__)
    cli.add_argument('--network-qemu', default='qemu-system-xtensa', help='host QEMU with user networking')
    cli.add_argument('--timeout', type=float, default=90, help='host seconds allowed per boot')
    cli.set_defaults(shift=5, log=Path('target/tracker-sim/upload.log'))
    args = cli.parse_args()
    if args.wall_clock:
        args.shift = None
    log = args.log
    for reject in (False, True):
        with tempfile.TemporaryDirectory(prefix='nxrs-upload-', dir='/tmp') as folder:
            directory = Path(folder)
            with collector(directory) as (server, pin), network(directory, args.network_qemu,
                                                                 server.server_address[1]) as ethernet:
                selected_pin = 'ab' * 32 if reject else pin
                args.log = log.with_name(f'{log.stem}-bad-pin{log.suffix}') if reject else log

                def scenario(process, modem, gnss, transcript, shift, console_log):
                    qualify(process, modem, gnss, transcript, console_log, ethernet, server,
                            selected_pin, reject, args.timeout)

                try:
                    run(args, scenario)
                except Exception:
                    print((directory / 'network.log').read_text())
                    raise


if __name__ == '__main__':
    main()
