#!/usr/bin/env python3
"""Local exact GATT read/write and simultaneous UDP/TCP qualification.

Requires pyserial and bleak on a host with a Bluetooth adapter. No raw console
output is printed. Build/flash are deliberately separate from this runner.
"""
import argparse
import asyncio
import importlib.util
from pathlib import Path
import re
import socket
import statistics
import threading
import time

WIFI_PATH = Path(__file__).resolve().parents[1] / 'wifi/run.py'
SPEC = importlib.util.spec_from_file_location('wifi_runner', WIFI_PATH)
WIFI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WIFI)
SERVICE = '4e585253-0002-4000-8000-000000000001'
LEGACY_SERVICE = '4e585253-0001-4000-8000-000000000001'
VALUE = '4e585253-0001-4000-8000-000000000002'
FIXTURE_NAME = 'nxrs-gatt'
ROUNDS = 100
CONTROL_PACKETS = 1000


def gatt_packet(sequence):
    return sequence.to_bytes(4, 'big') + bytes((sequence + offset) % 256 for offset in range(16))


def join_worker(worker, errors=()):
    worker.join(timeout=2)
    if worker.is_alive():
        raise RuntimeError('host radio worker did not stop')
    if errors:
        raise RuntimeError(f'host radio worker failed: {errors[0]}') from errors[0]


def finish_worker(worker, errors, connections):
    try:
        join_worker(worker, errors)
    except RuntimeError:
        # Forced shutdown is failure cleanup only. It can never create a PASS.
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        worker.join(timeout=2)
        raise


def recv_exact(connection, length, deadline, allow_eof=False):
    data = bytearray()
    while len(data) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('TCP frame timed out')
        connection.settimeout(remaining)
        chunk = connection.recv(length - len(data))
        if not chunk:
            if allow_eof and not data:
                return None
            raise RuntimeError('TCP EOF in partial frame')
        data.extend(chunk)
    return bytes(data)


def serve_tcp_connection(connection, served, natural_eof):
    sequence = 0
    while True:
        deadline = time.monotonic() + 2
        header = recv_exact(connection, 4, deadline, allow_eof=True)
        if header is None:
            natural_eof.set()
            return
        actual_sequence = int.from_bytes(header, 'big')
        if actual_sequence != sequence:
            raise RuntimeError(f'TCP sequence mismatch: expected {sequence}')
        expected = WIFI.expected_packet(sequence)
        frame = header + recv_exact(connection, len(expected) - 4, deadline)
        if frame != expected:
            raise RuntimeError(f'TCP payload mismatch at sequence {sequence}')
        connection.sendall(frame)
        served.add(sequence)
        sequence += 1


def serve_tcp(listener, stopped, served, natural_eof, errors, connections=None):
    try:
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            if connections is not None:
                connections.append(connection)
            with connection:
                serve_tcp_connection(connection, served, natural_eof)
            return
    except Exception:
        errors.append(RuntimeError('echo worker failed'))


class EchoPeer:
    """Lifecycle and evidence for one local UDP or TCP radio echo peer."""

    def __init__(self, host_ip, transport):
        if transport not in ('udp', 'tcp'):
            raise ValueError('unsupported echo transport')
        self.host_ip = host_ip
        self.transport = transport
        self.served = set()
        self.invalid = 0
        self.natural_eof = threading.Event()
        self.errors = []
        self._stopped = threading.Event()
        self._connections = []
        self._socket = None
        self._worker = None

    @property
    def port(self):
        if self._socket is None:
            raise RuntimeError('echo peer is not running')
        return self._socket.getsockname()[1]

    def __enter__(self):
        kind = socket.SOCK_STREAM if self.transport == 'tcp' else socket.SOCK_DGRAM
        try:
            self._socket = socket.socket(socket.AF_INET, kind)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind((self.host_ip, 0))
            self._socket.settimeout(0.2)
            if self.transport == 'tcp':
                self._socket.listen(1)
            target = self._serve_tcp if self.transport == 'tcp' else self._serve_udp
            self._worker = threading.Thread(target=target, name='radio-echo')
            self._worker.start()
            return self
        except Exception:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            raise RuntimeError('echo peer could not start') from None

    def _serve_tcp(self):
        worker_errors = []
        serve_tcp(self._socket, self._stopped, self.served, self.natural_eof,
                  worker_errors, self._connections)
        if worker_errors:
            self.errors.append(RuntimeError('echo worker failed'))

    def _serve_udp(self):
        try:
            while not self._stopped.is_set():
                try:
                    packet, peer = self._socket.recvfrom(513)
                except socket.timeout:
                    continue
                if len(packet) < 4:
                    self.invalid += 1
                    continue
                sequence = int.from_bytes(packet[:4], 'big')
                if packet != WIFI.expected_packet(sequence) or sequence in self.served:
                    self.invalid += 1
                    continue
                self.served.add(sequence)
                self._socket.sendto(packet, peer)
        except Exception:
            if not self._stopped.is_set():
                self.errors.append(RuntimeError('echo worker failed'))

    def __exit__(self, error_type, error, traceback):
        self._stopped.set()
        try:
            if self._worker is not None:
                finish_worker(self._worker, self.errors, self._connections)
        finally:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
        return False

    def validate(self, expected_count=None, minimum=7):
        if self._worker is None or self._worker.is_alive():
            raise RuntimeError('echo worker shutdown not confirmed')
        count = len(self.served)
        if self.invalid or self.errors:
            raise RuntimeError(f'{self.transport.upper()} evidence invalid')
        if expected_count is not None and count != expected_count:
            raise RuntimeError(f'{self.transport.upper()} evidence count mismatch')
        if count < minimum or self.served != set(range(count)):
            raise RuntimeError(f'{self.transport.upper()} evidence incomplete')
        if self.transport == 'tcp' and not self.natural_eof.is_set():
            raise RuntimeError('TCP peer did not close naturally')


def take_marker(pending, marker, password):
    if password and password in pending:
        raise RuntimeError('credential echo detected; output withheld')
    if b'NXRS_RADIO_FAIL' in pending:
        error = re.search(rb'NXRS_RADIO_FAIL error=([A-Za-z]+)', pending)
        raise RuntimeError('device failure: ' + (error[1].decode() if error else 'unknown'))
    if marker in pending:
        end = pending.index(marker) + len(marker)
        result = bytes(pending[:end])
        del pending[:end]
        return result
    return None


def until(console, pending, marker, password, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = take_marker(pending, marker, password)
        if result is not None:
            return result
        pending.extend(console.read(512))
        if len(pending) > 8192:
            del pending[:-4096]
    raise RuntimeError('device stage timed out')


def open_console(port):
    import serial

    console = serial.Serial()
    console.port, console.baudrate, console.timeout = port, 115200, 0.1
    console.dtr = console.rts = False
    console.open()
    return console


def require_zero_status(console, pending, password):
    console.write(b'echo $?\n')
    status = until(console, pending, b'nsh> ', password, 5)
    values = [line.strip() for line in status.splitlines()
              if re.fullmatch(rb'-?[0-9]+', line.strip())]
    if values != [b'0']:
        raise RuntimeError('NSH exit status was not zero')


class GattProgress:
    """Failure-only phase record; never emit backend errors or identifiers."""

    def __init__(self):
        self.stage = 'scan'
        self.sequence = 0

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        if error_type is not None:
            print(f'NXRS_HOST_GATT_OPERATION stage={self.stage} sequence={self.sequence}',
                  flush=True)
        return False


async def discover_fixture(*, scan_started=None):
    """Keep service matches across separate advertisement/scan-response reports."""
    from bleak import BleakScanner

    devices = {}
    reports = dict(service_reports=0, named_reports=0, named_without_service=0,
                   os_name_reports=0, os_name_without_service=0)

    def detected(device, advertisement):
        service = SERVICE.lower() in [value.lower() for value in advertisement.service_uuids]
        named = advertisement.local_name == FIXTURE_NAME
        os_name = device.name == FIXTURE_NAME
        reports['service_reports'] += int(service)
        reports['named_reports'] += int(named)
        reports['named_without_service'] += int(named and not service)
        reports['os_name_reports'] += int(os_name)
        reports['os_name_without_service'] += int(os_name and not service)
        if service:
            devices[device.address] = device

    # One unchanged five-second window. Match the advertised UUID ourselves;
    # names are diagnostic hints, never a fallback selector or proof of identity.
    try:
        async with BleakScanner(detection_callback=detected):
            delay = 5
            if scan_started is not None:
                # Charge observer setup to the window rather than extending it.
                started = time.monotonic()
                scan_started(started + 5)
                delay = max(0, 5 - (time.monotonic() - started))
            await asyncio.sleep(delay)
        if len(devices) != 1:
            raise RuntimeError(f'expected one fixture advertiser, found {len(devices)}')
    except BaseException:
        # Original-window counts only: no extra scan, identifiers or raw reports.
        print('NXRS_HOST_GATT_DISCOVERY ' + ' '.join(
            f'{key}={value}' for key, value in reports.items()) +
            f' matched_devices={len(devices)}', flush=True)
        raise
    return next(iter(devices.values()))


async def gatt(study_done=None, *, scan_started=None):
    from bleak import BleakClient

    with GattProgress() as progress:
        discovery = (discover_fixture() if scan_started is None
                     else discover_fixture(scan_started=scan_started))
        device = await asyncio.wait_for(discovery, timeout=12)
        samples = []
        progress.stage = 'connect-discover'
        async with BleakClient(device, services=[SERVICE]) as client:
            progress.stage = 'characteristic'
            characteristic = client.services.get_characteristic(VALUE)
            if characteristic is None or not {'read', 'write'} <= set(characteristic.properties):
                raise RuntimeError('fixture characteristic missing read/write support')
            for sequence in range(ROUNDS):
                progress.sequence = sequence
                expected = gatt_packet(sequence)
                start = time.monotonic()
                progress.stage = 'write'
                await client.write_gatt_char(characteristic, expected, response=True)
                progress.stage = 'read'
                actual = await client.read_gatt_char(characteristic, use_cached=False)
                if actual != expected:
                    raise RuntimeError(f'GATT readback mismatch at sequence {sequence}')
                samples.append((time.monotonic() - start) * 1000)
            if study_done is not None:
                extra_reads = 0
                final_value = gatt_packet(ROUNDS - 1)
                while True:
                    progress.stage = 'study-read'
                    actual = await client.read_gatt_char(characteristic, use_cached=False)
                    if actual != final_value:
                        raise RuntimeError('GATT study readback mismatch')
                    extra_reads += 1
                    progress.stage = 'study-completion'
                    if study_done():
                        break
                print(f'GATT study: {extra_reads} additional verified reads before disconnect.', flush=True)
            if not client.is_connected:
                raise RuntimeError('unexpected disconnect during transfer')
            progress.stage = 'disconnect'
        if client.is_connected:
            raise RuntimeError('host disconnect was not confirmed')
    samples.sort()
    print(f'GATT: {ROUNDS} exact writes/readbacks, disconnect confirmed; '
          f'round-trip median={statistics.median(samples):.1f}ms p95={samples[94]:.1f}ms.', flush=True)


def device_transport(transport, socket_implementation):
    if transport not in ('udp', 'tcp') or socket_implementation not in ('rust', 'c'):
        raise ValueError('unsupported transport or TCP implementation')
    return f'{transport}-c' if socket_implementation == 'c' else transport


def study_transport(study):
    if study is None:
        return None
    if study not in ('wifi-only', 'coexist'):
        raise ValueError('study must be wifi-only or coexist')
    return 'coexist-control' if study == 'coexist' else 'wifi-only'


def qualify_ble(port):
    """Run the same exact GATT exchange without starting Wi-Fi or a socket."""
    with open_console(port) as console:
        pending = bytearray()
        console.write(b'\n\n\n')
        until(console, pending, b'nsh> ', b'', 5)
        console.write(b'radio_test ble-only\n')
        until(console, pending, b'NXRS_RADIO_GATT_READY', b'', 5)
        asyncio.run(asyncio.wait_for(gatt(), timeout=50))
        until(console, pending, b'NXRS_RADIO_GATT_DONE', b'', 5)
        console.write(b'done\n')
        until(console, pending, b'NXRS_RADIO_PASS', b'', 15)
        until(console, pending, b'nsh> ', b'', 5)
        require_zero_status(console, pending, b'')
    print(f'PASS: {ROUNDS} exact GATT exchanges, BLE disconnect, Wi-Fi not started, NSH zero.')


def qualify(port, host_ip, ssid, password, transport='udp', socket_implementation='rust', study=None):
    command_transport = device_transport(transport, socket_implementation)
    control_mode = study_transport(study)
    with EchoPeer(host_ip, transport) as echo:
        try:
            with open_console(port) as console:
                pending = bytearray()
                console.write(b'\n\n\n')
                until(console, pending, b'nsh> ', password, 5)
                command = f'radio_test {host_ip}:{echo.port} {command_transport}'
                if control_mode is not None:
                    command += f' {control_mode}'
                console.write(f'{command}\n'.encode())
                until(console, pending, b'NXRS_WIFI_CREDENTIALS_READY', password, 5)
                console.write(bytes((len(ssid), len(password))) + ssid + password)
                until(console, pending, b'NXRS_WIFI_ASSOCIATED_DHCP', password, 35)
                if study == 'wifi-only':
                    until(console, pending, b'NXRS_RADIO_CONTROL_READY', password, 5)
                    until(console, pending, b'NXRS_RADIO_CONTROL_TRAFFIC_DONE', password, 60)
                elif study == 'coexist':
                    until(console, pending, b'NXRS_RADIO_GATT_READY', password, 5)
                    deadline = time.monotonic() + 60

                    def traffic_done():
                        if time.monotonic() >= deadline:
                            raise RuntimeError('device study traffic timed out')
                        pending.extend(console.read(512))
                        if len(pending) > 8192:
                            del pending[:-4096]
                        return take_marker(
                            pending, b'NXRS_RADIO_CONTROL_TRAFFIC_DONE', password) is not None

                    console.timeout = 0
                    try:
                        asyncio.run(asyncio.wait_for(gatt(traffic_done), timeout=60))
                    finally:
                        console.timeout = 0.1
                else:
                    until(console, pending, b'NXRS_RADIO_GATT_READY', password, 5)
                    asyncio.run(asyncio.wait_for(gatt(), timeout=50))
                if study != 'wifi-only':
                    until(console, pending, b'NXRS_RADIO_GATT_DONE', password, 5)
                    console.write(b'done\n')
                until(console, pending, b'NXRS_RADIO_PASS', password, 35)
                until(console, pending, b'nsh> ', password, 5)
                require_zero_status(console, pending, password)
        finally:
            print(f'{transport.upper()}: exact={len(echo.served)}, '
                  f'invalid_or_duplicate={echo.invalid}.', flush=True)
    echo.validate(expected_count=CONTROL_PACKETS if study is not None else None)
    if study == 'wifi-only':
        print(f'PASS: {CONTROL_PACKETS} exact {command_transport.upper()} exchanges, GATT not started, '
              'Wi-Fi disconnect, worker joins, NSH zero.')
    else:
        print(f'PASS: exact GATT + overlapping {command_transport.upper()}, host BLE disconnect, '
              'Wi-Fi disconnect, worker joins, NSH zero.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--peer-port',
                        help='use a second board as GATT peripheral; no host Bluetooth required')
    parser.add_argument('--ble-only', action='store_true',
                        help='same GATT workload without Wi-Fi association or traffic')
    parser.add_argument('--host-ip')
    parser.add_argument('--ssid')
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('--transport', choices=('udp', 'tcp'), default='udp')
    parser.add_argument('--socket-implementation', '--tcp-implementation',
                        dest='socket_implementation', choices=('rust', 'c'), default='rust',
                        help='socket control only; C uses the same worker and payload loop')
    parser.add_argument('--study', choices=('wifi-only', 'coexist'))
    args = parser.parse_args()
    if args.peer_port and (args.ble_only or args.study is not None
                          or args.socket_implementation != 'rust'):
        parser.error('--peer-port requires portable-client mode without study/C-socket controls')
    wifi_args = (args.host_ip, args.ssid, args.password_file)
    if args.ble_only:
        if (any(value is not None for value in wifi_args) or args.study is not None
                or args.transport != 'udp' or args.socket_implementation != 'rust'):
            parser.error('--ble-only cannot be combined with Wi-Fi study or transport options')
    elif any(value is None for value in wifi_args):
        parser.error('--host-ip, --ssid and --password-file are required unless --ble-only')
    try:
        if args.ble_only:
            qualify_ble(args.port)
            return
        ssid = args.ssid.encode()
        if not 1 <= len(ssid) <= 32:
            raise ValueError('SSID must encode to 1–32 bytes')
        if args.peer_port:
            spec = importlib.util.spec_from_file_location(
                'portable_radio_client', Path(__file__).with_name('client.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            status = module.qualify(
                args.port, args.peer_port, args.host_ip, ssid,
                WIFI.password_bytes(args.password_file), args.transport, echo_type=EchoPeer)
            if status:
                parser.exit(status)
            return
        qualify(args.port, args.host_ip, ssid, WIFI.password_bytes(args.password_file),
                args.transport, args.socket_implementation, args.study)
    except Exception as error:
        # Native BLE errors can contain peripheral identifiers but never the
        # credential frame; serial errors are sanitized by until().
        parser.exit(1, f'FAIL: {error}\n')


if __name__ == '__main__':
    main()
