"""Keep one authenticated BLE stream open across loss of the tracker's PPP link."""
import argparse
import asyncio
import base64
from pathlib import Path
import re
import struct
import tempfile
import time

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
        self.recovery_port = None
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
        if self.lost_dial is not None and self.dials > self.lost_dial:
            ip_length = (body[0] & 0xf) * 4
            tcp_length = (body[ip_length + 12] >> 4) * 4
            src, _, sequence = struct.unpack_from('!HHI', body, ip_length)
            if body[ip_length + 13] & 0x12 == 0x02:  # SYN without ACK
                self.recovery_port = src
            if self.hold_tcp and src == self.recovery_port and len(body) > ip_length + tcp_length:
                if self.held_tcp is None:
                    self.held_tcp = (sequence, body)
                    self.held_at = time.monotonic()
                    return
                if sequence == self.held_tcp[0]:
                    self.retransmissions += 1
                    return
        super().ipv4(body)

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

    async def exercise(self, guest, panel, phone, args):
        stamp = self.stamp
        await guest.wait_text(b'nsh>')
        offset = len(guest.buffer)
        guest.command(
            f'motion_tracker --frames {FRAMES} --interval-ms {INTERVAL_MS} --stream-usb --ble '
            f'--cellular-apn nxrs.test --collector {COLLECTOR}:{stamp.port} --collector-pin {self.pin}')
        key = await panel.pairing_key()
        await wait_until(lambda: self.collector_live(1), 'initial cellular TLS telemetry', 12)
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

        for _ in range(4):
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
        stamp.hold_tcp = True

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
    parser.add_argument('--timeout', default=100, type=float)
    parser.add_argument('--log', type=Path, default=Path('target/tracker-coexist-sim/coexist.log'))
    args = parser.parse_args()
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
