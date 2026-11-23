"""Keep one authenticated BLE stream open across loss of the tracker's PPP link."""
import argparse
import asyncio
import base64
from pathlib import Path
import re
import struct
import tempfile
import time

from bumble.device import Peer
from bumble.gatt import GATT_CLIENT_CHARACTERISTIC_CONFIGURATION_DESCRIPTOR

from dashboard import SecureStream, Stream, connect, pump, run, wait_until
from sensors import check_live
from upload import COLLECTOR, TcpStamp, collector, is_live, network

RAM_UART_SIZE = 2048
FRAMES = 80
INTERVAL_MS = 500
BLE_READ_TIMEOUT = 5
MAX_WALL_BLE_GAP = 2.5
TCP_HOLD_MS = 3200


class RamUart:
    """Host end of two native NuttX RAM UART rings, accessed via QEMU qtest."""

    def __init__(self, panel):
        self.panel = panel
        self.tx = panel.addresses['tracker_hci_buffers']
        self.rx = self.tx + RAM_UART_SIZE + 8
        self.inbound = bytearray()

    def on_packet(self, packet):
        self.inbound.extend(packet)
        assert len(self.inbound) <= 2 * RAM_UART_SIZE, 'controller overran its staging buffer'

    async def read(self, address, length):
        return base64.b64decode(await self.panel.query(f'b64read {address:#x} {length}'), validate=True)

    async def offsets(self, ring):
        write, read = struct.unpack('<II', await self.read(ring + RAM_UART_SIZE, 8))
        assert write < RAM_UART_SIZE and read < RAM_UART_SIZE, (write, read)
        return write, read

    async def serve(self, receive):
        while True:
            write, read = await self.offsets(self.tx)
            count = min((write - read) % RAM_UART_SIZE, RAM_UART_SIZE - read)
            if count:
                packet = await self.read(self.tx + read, count)
                await self.panel.query(f'writel {self.tx + RAM_UART_SIZE + 4:#x} {(read + count) % RAM_UART_SIZE}')
                receive(packet)
            if self.inbound:
                write, read = await self.offsets(self.rx)
                if write == read:
                    length = min(len(self.inbound), RAM_UART_SIZE - 1, RAM_UART_SIZE - write)
                    encoded = base64.b64encode(self.inbound[:length]).decode()
                    await self.panel.query(f'b64write {self.rx + write:#x} {length} {encoded}')
                    await self.panel.query(f'writel {self.rx + RAM_UART_SIZE:#x} {(write + length) % RAM_UART_SIZE}')
                    del self.inbound[:length]
            if not count:
                await asyncio.sleep(0.0005)


class OutageStamp(TcpStamp):
    def __init__(self, modem, ethernet, port):
        super().__init__(modem, ethernet, port)
        self.hold = True
        self.denied = 0
        self.tcp_port = None
        self.hold_tcp = False
        self.held_tcp = None
        self.held_at = None
        self.released_at = None
        self.retransmissions = 0

    def command(self, line):
        if line == b'ATD*99#' and self.terminated and self.hold:
            self.commands.append(line)
            self.dials += 1
            self.denied += 1
            self.wire.sendall(b'\r\nNO CARRIER\r\n')
        else:
            super().command(line)

    def ipv4(self, body):
        ip_length = (body[0] & 0xf) * 4
        tcp_length = (body[ip_length + 12] >> 4) * 4
        src, _, sequence = struct.unpack_from('!HHI', body, ip_length)
        if body[ip_length + 13] & 0x12 == 0x02:  # SYN without ACK
            self.tcp_port = src
        if self.hold_tcp and src == self.tcp_port and len(body) > ip_length + tcp_length:
            if self.held_tcp is None:
                self.held_tcp = (sequence, body)
                self.held_at = time.monotonic()
                return
            if sequence == self.held_tcp[0]:
                self.retransmissions += 1
                return
        super().ipv4(body)

    def begin_hold(self):
        assert not self.hold_tcp
        self.held_tcp = self.held_at = self.released_at = None
        self.retransmissions = 0
        self.hold_tcp = True

    def release_tcp(self):
        _, body = self.held_tcp
        self.hold_tcp = False
        self.released_at = time.monotonic()
        super().ipv4(body)


class Scenario:
    def __init__(self, server, pin, ethernet):
        self.server = server
        self.pin = pin
        self.ethernet = ethernet
        self.stamp = None
        self.collector_samples = {}
        self.tls_connections = set()
        self.collector_closures = []

    async def attach(self, panel, modem_reader, modem_writer, receive_hci, tasks):
        host_reader, host_writer = await asyncio.open_connection(sock=self.ethernet)
        self.stamp = OutageStamp(Stream(modem_writer), Stream(host_writer), self.server.server_address[1])
        hci = RamUart(panel)

        async def receive_ethernet():
            try:
                await pump(host_reader, self.stamp.receive_ethernet)
            finally:
                host_writer.close()
                await host_writer.wait_closed()

        tasks.extend([
            asyncio.create_task(hci.serve(receive_hci)),
            asyncio.create_task(pump(modem_reader, self.stamp.receive)),
            asyncio.create_task(receive_ethernet()),
        ])
        return hci

    def collector_live(self, connection):
        while not self.server.events.empty():
            number, event, value = self.server.events.get_nowait()
            if event == 'secure':
                self.tls_connections.add(number)
            elif event == 'closed':
                self.collector_closures.append((number, value))
            elif event == 'frame' and is_live(value[1]):
                check_live(value[1])
                self.collector_samples[number] = value[1]
        return connection in self.tls_connections and connection in self.collector_samples

    async def sample_export(self, guest, panel, phone, key, seconds):
        async def authenticated_link():
            connection, rx, tx = await connect(phone, export=True)
            disconnected = asyncio.Event()
            connection.once(connection.EVENT_DISCONNECTION, lambda _: disconnected.set())
            assert await Peer(connection).request_mtu(247) >= 64
            await tx.discover_descriptors()
            ccc = tx.get_descriptor(GATT_CLIENT_CHARACTERISTIC_CONFIGURATION_DESCRIPTOR)
            assert ccc is not None and await ccc.read_value(no_long_read=True) == b'\0\0'
            stream = SecureStream(rx, tx, key)
            await stream.subscribe()
            await asyncio.wait_for(stream.handshake(), 8)
            return connection, stream, disconnected

        before = self.collector_samples[1]['imu'][0]['seq']
        previous = None
        for minimum in (100 * seconds, 200):
            if previous is not None:
                _, rejected, retired = await authenticated_link()
                rejected.tls.write(b'!')
                await rejected.flush()
                await asyncio.wait_for(retired.wait(), 5)
                print('PASS: tracker retires the link after an invalid export request', flush=True)
            connection, stream, disconnected = await authenticated_link()
            latest = await self.export_window(guest, stream, minimum, previous, disconnected)
            await connection.disconnect()
            previous = latest
        assert guest.gnss.navigation == (500, 1)
        assert self.collector_live(1) and self.collector_samples[1]['imu'][0]['seq'] > before
        assert self.tls_connections == {1} and not self.collector_closures
        print('PASS: full-rate export reconnected with the same pairing key and continuing cellular TLS', flush=True)
        await panel.wait_pixel(275, 33, 0x57f4)
        await panel.tap(275, 20)
        assert await panel.pairing_key() == key
        print('PASS: the tracker offers its pairing code again after disconnect', flush=True)

    async def export_window(self, guest, stream, minimum, previous, disconnected):
        stream.tls.write(b'NXRS\x01')
        first = [None] * 3
        latest = [None] * 3
        count = [0] * 3
        started = time.monotonic()
        dashboard = []
        hold_points = iter((500, 2_500, 4_500)) if previous is None else iter(())
        next_hold = next(hold_points, None)
        loss_injected = False
        async with asyncio.timeout(15 + minimum / 92), asyncio.TaskGroup() as stalls:
            while min(count[:2]) < minimum or count[2] < minimum // 50 or len(dashboard) < 2:
                try:
                    record = await asyncio.wait_for(stream.export_record(), timeout=6)
                except TimeoutError as error:
                    raise AssertionError(f'BLE export stalled at ciphertext byte {stream.offset}, after {count} samples '
                                         f'and {len(dashboard)} dashboard snapshots') from error
                assert not disconnected.is_set(), 'unexpected BLE disconnect during export'
                if isinstance(record, dict):
                    check_live(record)
                    dashboard.append(time.monotonic())
                    continue
                pos = 0
                while pos < len(record):
                    tag, seq, tick = struct.unpack_from('<BII', record, pos)
                    assert 1 <= tag <= 3
                    index = tag - 1
                    if tag == 3:
                        lat, lon, _ = struct.unpack_from('<ddf', record, pos + 9)
                        assert abs(lat - 48.1173) < 1e-6 and abs(lon + 11.5166667) < 1e-6
                    else:
                        x, = struct.unpack_from('<f', record, pos + 9)
                        assert x < 0 if tag == 1 else x > 0
                    pos += 29 if tag == 3 else 33
                    if latest[index] is None:
                        first[index] = tick
                        assert previous is None or seq > previous[index][0], (tag, seq, previous)
                    else:
                        assert seq == latest[index][0] + 1, (tag, seq, latest[index], count)
                    latest[index] = (seq, tick)
                    count[index] += 1
                if next_hold is not None and count[0] >= next_hold:
                    # Keep consuming packets the controller has already sent.
                    stalls.create_task(self.hold_acl_credits(guest))
                    next_hold = next(hold_points, None)
                if previous is None and not loss_injected and count[0] >= 1_400:
                    guest.ble_link.notifications_to_drop = 2
                    guest.ble_link.acknowledgements_to_drop = 2
                    loss_injected = True
        if previous is None:
            assert loss_injected and stream.out_of_order > 0, 'the peer never witnessed the notification gap'
            assert not guest.ble_link.notifications_to_drop and not guest.ble_link.acknowledgements_to_drop
            print('PASS: replay recovered two lost notifications and two lost acknowledgments', flush=True)
        rates = [(n - 1) * 1000 / (end[1] - start) for n, end, start in zip(count, latest, first)]
        assert all(92 <= rate <= 108 for rate in rates[:2]) and 1.8 <= rates[2] <= 2.2, rates
        times = [started, *dashboard, time.monotonic()]
        gap = max(end - start for start, end in zip(times, times[1:]))
        assert gap < 4, ('dashboard became stale during full-rate export', gap)
        print(f'PASS: BLE export counts {count}, rates {[round(rate, 2) for rate in rates]}, '
              f'zero sequence gaps and {len(dashboard)} dashboard snapshots with max gap {gap:.2f}s', flush=True)
        return latest

    async def hold_acl_credits(self, guest):
        def source_time():
            return guest.samples[-1]['imu'][0].get('received_ms', 0)

        assert self.collector_live(1)
        before = self.collector_samples[1]['imu'][0]['seq']
        start = source_time()
        guest.controller.hold_acl_credits()
        try:
            await wait_until(lambda: source_time() - start >= 1_800, 'ACL credit hold')
            assert self.collector_live(1) and self.collector_samples[1]['imu'][0]['seq'] > before, (
                'cellular upload made no progress during the BLE credit hold')
        finally:
            withheld = guest.controller.release_acl_credits()
        assert withheld > 0, 'the test did not withhold any controller credits'
        print(f'Withheld {withheld} ACL credits for {source_time() - start} ms guest time', flush=True)

    async def exercise(self, guest, panel, phone, args):
        stamp = self.stamp
        await guest.wait_text(b'nsh>')
        offset = len(guest.buffer)
        guest.command(
            f'motion_tracker --frames {2 * (args.export_seconds + 30) if args.export else FRAMES} '
            f'--interval-ms {INTERVAL_MS} --stream-usb --ble '
            f'--cellular-apn nxrs.test --collector {COLLECTOR}:{stamp.port} --collector-pin {self.pin}')
        key = await panel.pairing_key()
        await wait_until(lambda: self.collector_live(1), 'initial cellular TLS telemetry', 12)
        if args.export:
            await self.sample_export(guest, panel, phone, key, args.export_seconds)
            return
        stamp.begin_hold()
        await wait_until(lambda: stamp.held_tcp, 'cellular TLS data to stall before pairing')
        connection, rx, tx = await connect(phone)
        disconnected = asyncio.Event()
        connection.once(connection.EVENT_DISCONNECTION, lambda _: disconnected.set())
        stream = SecureStream(rx, tx, key)
        await asyncio.wait_for(stream.handshake(), 5)

        timestamps = []
        arrivals = []
        previous = None

        async def sample():
            nonlocal previous
            reading = await asyncio.wait_for(stream.sample(), BLE_READ_TIMEOUT)
            assert not disconnected.is_set(), 'BLE disconnected during the measurement'
            check_live(reading)
            if previous is not None:
                for current, prior in zip(reading['imu'], previous['imu']):
                    assert current['seq'] > prior['seq'] and current['received_ms'] > prior['received_ms']
            previous = reading
            timestamps.append(reading['imu'][0]['received_ms'])
            arrivals.append(time.monotonic())

        await sample()
        assert stamp.held_at < arrivals[-1] and stamp.released_at is None
        stamp.release_tcp()
        print('PASS: BLE paired and received live telemetry while cellular data was held', flush=True)
        for _ in range(3):
            await sample()
        assert stamp.dials == 2 and stamp.data_mode and stamp.ipcp
        before = len(timestamps)
        outage_offset = len(guest.buffer)
        stamp.terminate_link()
        await guest.wait_text(b'cellular Failed(LinkLost)', outage_offset)
        lost = timestamps[-1]
        # Exclude a record already buffered by the BLE client at the moment of loss.
        floor = guest.samples[-1]['imu'][0].get('received_ms', lost)
        during = 0
        while during < 4 or not stamp.denied:
            await sample()
            if timestamps[-1] > floor:
                during += 1
            assert not self.collector_live(2), 'collector resumed while the modem still refused calls'
        assert stamp.terminate_ack and during >= 4
        stamp.hold = False

        releasing = len(timestamps)
        while not self.collector_live(2):
            await sample()
            assert len(timestamps) - releasing < 15, 'collector TLS did not recover after redial'
        assert stamp.dials >= 4 and stamp.ipcp and stamp.data_mode
        assert self.collector_samples[2]['imu'][0]['received_ms'] > floor
        await guest.wait_text(b'collector TLS online', outage_offset)
        recovered = len(timestamps)
        stamp.begin_hold()

        def source_time():
            return guest.samples[-1]['imu'][0].get('received_ms', 0)

        async def hold_and_release():
            # Release on the independently observed USB clock. An ACK-blocked
            # BLE task must not be able to end the impairment early.
            await wait_until(lambda: stamp.held_tcp, 'first TLS data to hold')
            deadline = source_time() + TCP_HOLD_MS
            await wait_until(lambda: source_time() >= deadline, 'TCP hold interval')
            release_floor = source_time()
            stamp.release_tcp()
            return release_floor

        async with asyncio.TaskGroup() as group:
            hold = group.create_task(hold_and_release())
            while stamp.released_at is None:
                await sample()
        released = len(timestamps)
        for _ in range(4):
            await sample()
        assert not disconnected.is_set(), 'BLE link did not survive PPP reconnect'
        assert self.collector_live(2)
        assert self.collector_samples[2]['imu'][0]['received_ms'] > hold.result()
        assert stamp.retransmissions > 0, 'NuttX did not retry the held TLS data'
        retry_interval = stamp.released_at - stamp.held_at
        during_retry = sum(stamp.held_at < arrival < stamp.released_at for arrival in arrivals)
        host_gap = max(b - a for a, b in zip(arrivals, arrivals[1:]))
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        phase_gaps = {
            'before': max(gaps[:before - 1]),
            'offline': max(gaps[before - 1:releasing - 1]),
            'redial and TLS': max(gaps[releasing - 1:recovered], default=0),
            'TCP hold': max(gaps[recovered - 1:released], default=0),
            'after': max(gaps[released:]),
        }
        print(f'BLE IMU-1 timestamp gaps by phase (guest ms): {phase_gaps}', flush=True)
        print(f'TLS data was held for {retry_interval:.3f} host seconds. NuttX retried '
              f'{stamp.retransmissions} times and {during_retry} BLE records arrived.', flush=True)
        if args.wall_clock:
            assert host_gap <= MAX_WALL_BLE_GAP, f'BLE paused for {host_gap:.3f}s during the test'
            assert during_retry > 0, 'BLE did not deliver while the TCP acknowledgment was withheld'
        print(f'PASS: one BLE/TLS session delivered {len(timestamps)} live records '
              f'({before} before loss, {during} while redial was refused, 4 after upload resumed). '
              f'Largest gap {max(gaps)} ms guest time, '
              f'{host_gap:.3f} s between host reads.', flush=True)
        print(f'PASS: TLS upload resumed on the redialed PPP link after {stamp.denied} refused redials',
              flush=True)
        if self.collector_closures:
            print(f'Collector closures {self.collector_closures}', flush=True)

        await connection.disconnect()
        await guest.wait_text(f'{FRAMES} frames, interval={INTERVAL_MS} ms, IMU 1 live, IMU 2 live, GNSS live'.encode(),
                              offset, args.timeout)
        await guest.wait_text(b'nsh>', offset + 8)
        exit_offset = len(guest.buffer)
        guest.command('echo coexist-exit:$?')
        await guest.wait_text(b'coexist-exit:0', exit_offset)
        assert guest.gnss.writes == [1, 7], guest.gnss.writes
        assert not re.search(rb'PANIC|panicked at|Assertion failed', guest.buffer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--qemu', default='qemu-system-xtensa')
    parser.add_argument('--network-qemu', default='qemu-system-xtensa')
    parser.add_argument('--qemu-data')
    parser.add_argument('--nm', default='xtensa-esp32s3-elf-nm')
    clock = parser.add_mutually_exclusive_group()
    clock.add_argument('--shift', type=int, choices=range(11), help='optional QEMU clock acceleration')
    clock.add_argument('--wall-clock', action='store_true', help='use wall-clock timing (default)')
    parser.add_argument('--export', action='store_true', help='qualify authenticated full-rate BLE export')
    parser.add_argument('--export-seconds', type=int, default=60)
    parser.add_argument('--timeout', default=100, type=float)
    parser.add_argument('--log', type=Path, default=Path('target/tracker-coexist-sim/coexist.log'))
    args = parser.parse_args()
    if args.export and args.export_seconds < 60:
        parser.error('--export-seconds must be at least 60 for the three credit-stall controls')
    args.wall_clock = args.shift is None
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nxrs-coexist-', dir='/tmp') as folder:
        directory = Path(folder)
        with collector(directory, close_first=False, idle_timeout=15) as (server, pin), network(
                directory, args.network_qemu, server.server_address[1]) as ethernet, args.log.open('wb') as log:
            scenario = Scenario(server, pin, ethernet)
            asyncio.run(run(args, log, scenario.exercise, scenario.attach))


if __name__ == '__main__':
    main()
