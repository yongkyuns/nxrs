"""Keep one authenticated BLE stream open across loss of the tracker's PPP link."""
import argparse
import asyncio
import base64
from collections import Counter
from pathlib import Path
import re
import struct
import sys
import tempfile
import time

from bumble.device import Peer
from bumble.gatt import GATT_CLIENT_CHARACTERISTIC_CONFIGURATION_DESCRIPTOR

from dashboard import SecureStream, Stream, connect, pump, run, wait_until, connect_map_export, luminance
import tile_fixture
from sensors import check_fix, check_live
from upload import CELLULAR_INTERVAL_S, COLLECTOR, TcpStamp, collector, is_live, network

sys.path.append(str(Path(__file__).resolve().parents[2] / 'host'))
from test_nuttx_memory import parse_meminfo, validate_heap_cycles

RAM_UART_SIZE = 2048
FRAMES = 80
INTERVAL_MS = 500
BLE_READ_TIMEOUT = 5
MAX_WALL_BLE_GAP = 2.5
TCP_HOLD_MS = 3200
SPACED_NOTIFICATION_LOSSES = 10
EXPORT_SAMPLES_PER_MINUTE = 6_000
STACK_TASKS = {
    'motion_tracker': 1, 'tracker-links': 1, 'tracker-collector': 1,
    'imu-acquire': 2, 'gnss-acquire': 1, 'cellular': 1, 'touch': 1,
}
STACK_WARNING_PERCENT = 20
STACK_MINIMUM_PERCENT = 15
STACK_GUARD_BYTES = 256
EXPORT_HEAP_FLOOR = 20 * 1024
STACK_ROW = re.compile(rb'(?m)^[ \t]*(\d+)[ \t]+(\d+)[ \t]+(\d+)[ \t]+([\w-]+)[ \t]*$')
EXPORT_FAULTS = (
    (500, 'hold credits'),
    (1_400, 'slow link'),
    (2_500, 'hold individual credits'),
    (2_800, 'spaced losses'),
    (4_100, 'restore link rate'),
    (4_500, 'hold credits'),
    (5_850, 'check recovery'),
)


def check_stack_headroom(console, *, collector=True):
    expected = STACK_TASKS.copy()
    if not collector:
        del expected['tracker-collector']
    peaks = {}
    complete = False
    for snapshot in re.split(rb'(?m)^PID +SIZE +USED +THREAD NAME\n', console)[1:]:
        tasks = Counter()
        for pid, size, used, name in STACK_ROW.findall(snapshot):
            name = name.decode()
            if name not in STACK_TASKS:
                continue
            size, used = int(size), int(used)
            assert 0 < used <= size, f'{name}: invalid stack sample {used}/{size} B'
            key = (pid, name)
            capacity, peak = peaks.get(key, (size, 0))
            assert size == capacity, f'{name}: stack size changed for TID {int(pid)}'
            peaks[key] = size, max(peak, used)
            tasks[name] += 1
        complete |= tasks == expected
    assert complete, 'stack monitor never observed all tracker tasks in one sample'

    for (pid, name), (size, used) in peaks.items():
        free = size - used
        result = f'{name} (TID {int(pid)}) has {free}/{size} B free ({free / size:.1%})'
        assert 100 * free >= STACK_MINIMUM_PERCENT * size, (
            f'{result}, below the {STACK_MINIMUM_PERCENT}% stack headroom floor')
        if 100 * free < STACK_WARNING_PERCENT * size:
            print(f'WARN: {result}, below the {STACK_WARNING_PERCENT}% stack headroom target', flush=True)
        else:
            print(f'PASS: {result}', flush=True)


async def start_stack_monitor(guest):
    offset = len(guest.buffer)
    guest.command('stackmonitor_start')
    await guest.wait_text(b'Stack Monitor: Started:', offset)
    await guest.wait_text(b'\nnsh>', offset)


async def check_stack_guard(guest, panel):
    base = await panel.word('tracker_console_stack_base')
    assert base and base % 16 == 0, 'missing or unaligned console stack base'
    last_word = base + STACK_GUARD_BYTES - 4
    assert int(await panel.query(f'readl {last_word:#x}'), 16) == 0xdeadbeef, (
        'console stack margin was already touched before the control')
    offset = len(guest.buffer)
    await panel.query(f'writel {last_word:#x} 0')
    guest.command('echo stack-guard-control')
    await wait_until(
        lambda: re.search(rb'Assertion failed stack margin overwritten'
                          rb'[^\n]+task: tracker_sim_init\b', guest.buffer[offset:]),
        'scheduler guard assertion in the console task')
    print(f'PASS: scheduler asserted after a write inside the {STACK_GUARD_BYTES}-byte stack margin',
          flush=True)


async def authenticated_link(phone, key, *, retain_out_of_order=True):
    connection, rx, tx = await connect(phone, export=True)
    disconnected = asyncio.Event()
    connection.once(connection.EVENT_DISCONNECTION, lambda _: disconnected.set())
    assert await Peer(connection).request_mtu(247) >= 64
    await tx.discover_descriptors()
    ccc = tx.get_descriptor(GATT_CLIENT_CHARACTERISTIC_CONFIGURATION_DESCRIPTOR)
    assert ccc is not None and await ccc.read_value(no_long_read=True) == b'\0\0'
    stream = SecureStream(rx, tx, key, retain_out_of_order=retain_out_of_order)
    await stream.subscribe()
    await asyncio.wait_for(stream.handshake(), 8)
    return connection, stream, disconnected


async def tracker_exit(guest, offset, frames, timeout):
    await guest.wait_text(
        f'{frames} frames, interval={INTERVAL_MS} ms, IMU 1 live, IMU 2 live, GNSS live'.encode(),
        offset, timeout)
    await guest.wait_text(b'\nnsh>', offset)
    exit_offset = len(guest.buffer)
    guest.command('echo coexist-exit:$?')
    await guest.wait_text(b'\ncoexist-exit:0\n', exit_offset)


async def read_heap(guest):
    offset = len(guest.buffer)
    guest.command('cat /proc/meminfo')
    await guest.wait_text(b'\nnsh>', offset)
    return parse_meminfo(guest.buffer[offset:].decode(errors='replace'))


async def check_heap_recovery(guest, panel, phone, previous_key):
    offset = len(guest.buffer)
    guest.command('stackmonitor_stop')
    await wait_until(lambda: guest.buffer[offset:].count(b'Stack Monitor: Stopped:') >= 2,
                     'stack monitor exited')

    baseline = await read_heap(guest)
    snapshots = []
    for _ in range(4):
        frames = 16
        offset = len(guest.buffer)
        received = len(guest.samples)
        guest.command(f'motion_tracker --frames {frames} --interval-ms {INTERVAL_MS} --stream-usb --ble')
        await wait_until(lambda: len(guest.samples) > received, 'restarted tracker sensor report')
        # The panel can still show the last run's frame before the background
        # display task has painted the new pairing code.
        key = await panel.pairing_key(previous=previous_key)
        previous_key = key
        connection, stream, _ = await authenticated_link(phone, key)
        await asyncio.wait_for(stream.sample(), 5)
        stream.tls.write(b'NXRS\x01')
        await stream.flush()
        async with asyncio.timeout(6):
            while isinstance(await stream.export_record(), dict):
                pass
        await connection.disconnect()
        await guest.wait_text(b'motion-tracker BLE export: high_water=', offset)
        await tracker_exit(guest, offset, frames, 15)
        snapshots.append(await read_heap(guest))

    warm, *checked = snapshots
    validate_heap_cycles('motion-tracker', baseline, warm, checked)
    assert all(snapshot['Umem'].maxfree >= EXPORT_HEAP_FLOOR for snapshot in snapshots), (
        'largest free block cannot accommodate the next BLE export capture allocation')
    heap = checked[-1]['Umem']
    print(f'PASS: {len(checked)} tracker restart and BLE export cycles returned to '
          f'{heap.free} B free, largest free block {heap.maxfree} B', flush=True)


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

    async def describe_rings(self):
        tx, rx = await self.offsets(self.tx), await self.offsets(self.rx)
        return f'RAM UART offsets (write, read): guest TX {tx}, guest RX {rx}'

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
                check_fix(value[1])
                self.collector_samples[number] = value[1]
        return connection in self.tls_connections and connection in self.collector_samples

    async def sample_export(self, guest, panel, phone, key, seconds):
        panel_work = panel.addresses['tracker_panel_work_us']
        await panel.query(f'writel {panel_work:#x} 0x7d0')
        busy_before = await panel.word('tracker_panel_busy_rows')

        before = self.collector_samples[1]['imu1_received_ms']
        previous = None
        for minimum in (100 * seconds, 200):
            if previous is not None:
                _, rejected, retired = await authenticated_link(phone, key)
                rejected.tls.write(b'!')
                await rejected.flush()
                await asyncio.wait_for(retired.wait(), 5)
                print('PASS: tracker retires the link after an invalid export request', flush=True)
            connection, stream, disconnected = await authenticated_link(
                phone, key, retain_out_of_order=previous is None)
            latest = await self.export_window(guest, stream, minimum, previous, disconnected)
            after = len(guest.buffer)
            await connection.disconnect()
            metrics = re.compile(rb'motion-tracker BLE export: high_water=(\d+) samples max_service_gap=(\d+) us\n')
            await wait_until(lambda: metrics.search(guest.buffer[after:]), 'completed export metrics', timeout=5)
            peak, gap = map(int, metrics.search(guest.buffer[after:]).groups())
            assert 0 < peak < 512 and 0 < gap <= 100_000, (peak, gap)
            print(f'PASS: capture high-water {peak}/512 samples, maximum BLE service gap {gap} us',
                  flush=True)
            previous = latest
        assert guest.gnss.navigation == (500, 1)
        assert self.collector_live(1) and self.collector_samples[1]['imu1_received_ms'] > before
        assert self.tls_connections == {1} and not self.collector_closures
        assert await panel.word('tracker_panel_busy_rows') > busy_before
        await panel.query(f'writel {panel_work:#x} 0')
        print('PASS: full-rate export reconnected with the same pairing key and continuing cellular TLS', flush=True)
        await panel.wait_pixel(275, 33, 0x57f4)
        await panel.tap(275, 20)
        assert await panel.pairing_key() == key
        print('PASS: the tracker offers its pairing code again after disconnect', flush=True)

    async def export_window(self, guest, stream, minimum, previous, disconnected):
        version = 3 if previous is None else 1
        link = guest.ble_link
        if version == 3:
            capabilities = await asyncio.wait_for(stream.sample(), 5)
            assert capabilities['export_version'] == 2 and all(stream.scales)
            link.notification_rate = 5_500
            assert all(reading['utc_ms'] is not None for reading in
                       [*capabilities['imu'], capabilities['gnss']])
            offsets = {reading['utc_ms'] - reading['received_ms'] for reading in
                       [*capabilities['imu'], capabilities['gnss']]}
            assert len(offsets) == 1
            clock_offset, = offsets
        stream.tls.write(b'NXRS' + bytes([version]))
        fixed_bytes = 0
        wire_bytes = 0
        first = [None] * 3
        latest = [None] * 3
        count = [0] * 3
        started = time.monotonic()
        dashboard = []
        minutes = minimum // EXPORT_SAMPLES_PER_MINUTE if version == 3 else 0
        faults = iter(
            (minute * EXPORT_SAMPLES_PER_MINUTE + count, action)
            for minute in range(minutes)
            for count, action in EXPORT_FAULTS
        )
        if version == 1:
            faults = iter(((40, 'legacy losses'),))
        fault = next(faults, None)
        async with asyncio.timeout(15 + minimum / 92), asyncio.TaskGroup() as stalls:
            while min(count[:2]) < minimum or count[2] < minimum // 50 or len(dashboard) < 2:
                try:
                    record = await asyncio.wait_for(stream.export_record(), timeout=6)
                except TimeoutError as error:
                    raise AssertionError(f'BLE export stalled at ciphertext byte {stream.offset}, after {count} samples '
                                         f'and {len(dashboard)} dashboard snapshots. '
                                         f'Last ACK {stream.last_acknowledged}, sent through byte '
                                         f'{link.notification_end}, replayed {link.replayed_bytes} bytes, '
                                         f'{stream.out_of_order} notifications beyond a gap') from error
                assert not disconnected.is_set(), 'unexpected BLE disconnect during export'
                if isinstance(record, dict):
                    check_live(record)
                    dashboard.append(time.monotonic())
                    continue
                record, encoded = record
                wire_bytes += encoded
                for tag, seq, tick, utc, values in record:
                    index = tag - 1
                    fixed_bytes += 29 if tag == 3 else 33
                    if tag == 3:
                        lat, lon, _ = values
                        assert abs(lat - 48.1173) < 1e-6 and abs(lon + 11.5166667) < 1e-6
                    else:
                        assert values[0] < 0 if tag == 1 else values[0] > 0
                    if version == 3:
                        assert utc == tick + clock_offset, (tag, seq, tick, utc, clock_offset)
                    if latest[index] is None:
                        first[index] = tick
                        assert previous is None or seq > previous[index][0], (tag, seq, previous)
                    else:
                        assert seq == latest[index][0] + 1, (tag, seq, latest[index], count)
                        assert tick >= latest[index][1]
                    latest[index] = (seq, tick)
                    count[index] += 1
                if fault is None or count[0] < fault[0]:
                    continue
                _, action = fault
                match action:
                    case 'hold credits' | 'hold individual credits':
                        # Keep consuming packets the controller has already sent.
                        stalls.create_task(self.hold_acl_credits(guest, individual=action == 'hold individual credits'))
                    case 'slow link':
                        link.notification_rate = 4_800
                        link.notifications_to_drop = 2
                        link.acknowledgements_to_drop = 2
                    case 'spaced losses':
                        # A sparse missing ATT notification still consumes airtime.
                        # Later packets and controller completions keep arriving.
                        link.next_spaced_loss = link.notification_end
                        link.spaced_losses_remaining = SPACED_NOTIFICATION_LOSSES
                    case 'restore link rate':
                        link.notification_rate = 5_500
                    case 'check recovery':
                        assert not (link.notifications_to_drop or link.acknowledgements_to_drop
                                    or link.spaced_losses_remaining), 'the test did not inject every scheduled loss'
                        usb_tick = guest.samples[-1]['imu'][0]['received_ms']
                        assert usb_tick - latest[0][1] < 800, (usb_tick, latest[0][1])
                        print(f'PASS: export caught up after minute {1 + fault[0] // EXPORT_SAMPLES_PER_MINUTE}',
                              flush=True)
                    case 'legacy losses':
                        link.notifications_to_drop = 2
                fault = next(faults, None)
        assert fault is None, f'export finished before scheduled fault at {fault}'
        assert not link.notifications_to_drop and stream.out_of_order > 0
        if version == 3:
            link.notification_rate = 0
            assert link.paced_notifications > 100
            assert wire_bytes < 0.65 * fixed_bytes, (wire_bytes, fixed_bytes)
            usb_tick = guest.samples[-1]['imu'][0]['received_ms']
            assert usb_tick - latest[0][1] < 800, (usb_tick, latest[0][1])
            snapshot = guest.samples[-1]
            for reading in [*snapshot['imu'], snapshot['gnss'], self.collector_samples[1]['gnss']]:
                assert reading['utc_ms'] - reading['received_ms'] == clock_offset
            print('PASS: the 4.8–5.5 kB/s BLE limit retained all samples, with one UTC mapping on BLE, USB and TLS',
                  flush=True)
            print(f'PASS: compact export used {wire_bytes} payload bytes versus {fixed_bytes} fixed bytes',
                  flush=True)
            print(f'PASS: recovered {minutes * (SPACED_NOTIFICATION_LOSSES + 2)} lost notifications and {minutes * 2} lost '
                  f'acknowledgments, replaying {link.replayed_bytes} ciphertext bytes',
                  flush=True)
        else:
            print('PASS: a receiver that discards out-of-order packets still recovers', flush=True)
        rates = [(n - 1) * 1000 / (end[1] - start) for n, end, start in zip(count, latest, first)]
        assert all(92 <= rate <= 108 for rate in rates[:2]) and 1.8 <= rates[2] <= 2.2, rates
        times = [started, *dashboard, time.monotonic()]
        gap = max(end - start for start, end in zip(times, times[1:]))
        assert gap < 4, ('dashboard became stale during full-rate export', gap)
        print(f'PASS: BLE export counts {count}, rates {[round(rate, 2) for rate in rates]}, '
              f'zero sequence gaps and {len(dashboard)} dashboard snapshots with max gap {gap:.2f}s', flush=True)
        return latest

    async def hold_acl_credits(self, guest, individual=False):
        def source_time():
            return guest.samples[-1]['imu'][0].get('received_ms', 0)

        assert self.collector_live(1)
        before = self.collector_samples[1]['imu1_received_ms']
        start = source_time()
        guest.controller.hold_acl_credits()
        try:
            await wait_until(lambda: source_time() - start >= 1_800, 'ACL credit hold')
            assert self.collector_live(1) and self.collector_samples[1]['imu1_received_ms'] > before, (
                'cellular upload made no progress during the BLE credit hold')
        finally:
            withheld = guest.controller.release_acl_credits(individual)
        assert withheld > 0, 'the test did not withhold any controller credits'
        print(f'Withheld {withheld} ACL credits for {source_time() - start} ms guest time '
              f'and returned them in {"individual events" if individual else "one event"}', flush=True)

    async def exercise_tiles(self, guest, panel, phone, args):
        await guest.wait_text(b'nsh>')
        offset = len(guest.buffer)
        guest.command(f'motion_tracker --map-relay {COLLECTOR}:{self.stamp.port} --map-pin {self.pin}')
        await guest.wait_text(b'direct map relay is unavailable with cellular', offset)
        await guest.wait_text(b'nsh>', offset)
        await start_stack_monitor(guest)
        offset = len(guest.buffer)
        guest.command(
            f'motion_tracker --frames {FRAMES} --interval-ms {INTERVAL_MS} --stream-usb --ble '
            '--cellular-apn nxrs.test')
        key = await panel.pairing_key()
        await guest.wait_text(b"cellular Online", offset)
        connection, stream = await asyncio.wait_for(connect_map_export(phone, key), 8)
        assert stream.windowed_writes and stream.ingress_payload == 239, stream.ingress_payload

        async def consume_during(operation):
            async with asyncio.TaskGroup() as pending:
                operation = pending.create_task(operation)
                while not operation.done():
                    await sample()
            return operation.result()

        async def sample():
            try:
                return await asyncio.wait_for(stream.sample(), 6)
            except TimeoutError as error:
                raise AssertionError(f'no BLE snapshot after {stream.sample_counts} export samples '
                                     f'at ciphertext offset {stream.offset}') from error

        async def select_gnss():
            await panel.reveal_menu()
            await panel.tap(195, 20)

        map_fix = tile_fixture.tile_center(18, 48.1173, -11.5166667)
        guest.moving_fix = map_fix
        while abs((await sample())['gnss']['lon_deg'] - map_fix['longitude_deg']) >= 1e-6:
            pass
        await consume_during(select_gnss())
        async with asyncio.timeout(30):
            while 'tile' not in (snapshot := await sample()):
                pass
            zoom, x, y = snapshot['tile']
            assert zoom == 18, snapshot['tile']
            await stream.send_tile(zoom, x, y)
            try:
                await consume_during(panel.wait_pixel(90, 50, 0x001f, timeout=15))
            except ExceptionGroup:
                (await panel.capture()).save(args.log.with_suffix('.png'))
                raise
            # The next tile comes back over BLE while the export is still active.
            before = stream.sample_counts.copy()
            boundary = (x + 1) * 360 / (1 << zoom) - 180
            distance = min(0.0003, (boundary - snapshot['gnss']['lon_deg']) / 2)
            guest.moving_fix = {**map_fix, 'longitude_deg': boundary - distance, 'speed_mps': 15}
            ahead = [zoom, x + 1, y]
            while (await sample()).get('tile') != ahead:
                pass
            # Alternate two palette indexes with the same color. The wire
            # encoder emits full-width rows so this exercises the largest tile.
            pixels = bytes.fromhex('f800f800') + bytes(28) + bytes([0x01]) * (170 * 85)
            start_writes = stream.acknowledged_writes
            start_retries = stream.write_retries
            if args.tile_write_loss:
                # Deliver the first write but drop the next one. The tail
                # must reject the gap, then the replay must skip the first.
                guest.ble_link.drop_ingress_at = (stream.ingress_offset + stream.ingress_payload) & 0xffffffff
            start_tile = time.monotonic()
            transferred = await stream.send_tile(*ahead, pixels=pixels)
            writes = stream.acknowledged_writes - start_writes
            seconds = time.monotonic() - start_tile
            assert transferred == 14_666, transferred
            # TLS adds 29 bytes per 512-byte record in this fixture.
            # Each window has one acknowledged tail.
            record_sizes = [min(512, transferred - offset) + 29
                            for offset in range(0, transferred, 512)]
            payload = stream.ingress_payload
            window = payload * (4 if stream.windowed_writes else 1)
            assert writes == sum((size + window - 1) // window for size in record_sizes), writes
            if args.tile_write_loss:
                assert guest.ble_link.drop_ingress_at is None, 'no ingress write was dropped'
                assert stream.write_retries > start_retries, 'the missing bytes were not replayed'
                print('PASS: dropped ingress write recovered without duplicating TLS bytes', flush=True)
            print(f'Tile transfer: {transferred} bytes in {writes} acknowledged writes '
                  f'with {payload}-byte payloads, {seconds:.3f} s wall time', flush=True)
            before_crossing = await consume_during(panel.wait_pixel(220, 50, 0xf800))
            guest.moving_fix = {**map_fix, 'longitude_deg': boundary + 0.00015, 'speed_mps': 15}
            after_crossing = await consume_during(panel.wait_pixel(160, 50, 0xf800))
            for frame in (before_crossing, after_crossing):
                assert frame.getpixel((160, 123)) == luminance(0xfda6), 'GNSS marker moved off center'
            while min(current - previous for current, previous in zip(stream.sample_counts, before)) < 2:
                await sample()
            assert min(stream.sample_counts[:2]) >= 200 and stream.sample_counts[2] >= 4
            rates = stream.export_rates()
            assert all(92 <= rate <= 108 for rate in rates[:2]) and 1.8 <= rates[2] <= 2.2, rates
        assert self.server.connections == 0, 'tile traffic went to the PPP listener'
        print(f'PASS: BLE map replies with PPP online, tile crossing, export {stream.sample_counts} '
              f'at {[round(rate, 2) for rate in rates]} Hz without gaps', flush=True)
        await connection.disconnect()
        await guest.wait_text(b'nsh>', offset + 8, args.timeout)
        exit_offset = len(guest.buffer)
        guest.command('echo tile-exit:$?')
        await guest.wait_text(b'tile-exit:0', exit_offset)
        check_stack_headroom(bytes(guest.buffer[offset:]), collector=False)

    async def exercise(self, guest, panel, phone, args):
        stamp = self.stamp
        await guest.wait_text(b'nsh>')
        if args.export:
            await start_stack_monitor(guest)
        offset = len(guest.buffer)
        frames = 2 * (args.export_seconds + 30) if args.export else FRAMES
        guest.command(
            f'motion_tracker --frames {frames} '
            f'--interval-ms {INTERVAL_MS} --stream-usb --ble '
            f'--cellular-apn nxrs.test --collector {COLLECTOR}:{stamp.port} --collector-pin {self.pin} '
            f'--cellular-report-secs {CELLULAR_INTERVAL_S}')
        key = await panel.pairing_key()
        await wait_until(lambda: self.collector_live(1), 'initial cellular TLS telemetry', 12)
        if args.export:
            await self.sample_export(guest, panel, phone, key, args.export_seconds)
            await tracker_exit(guest, offset, frames, args.timeout)
            assert not re.search(rb'PANIC|panicked at|Assertion failed', guest.buffer)
            check_stack_headroom(bytes(guest.buffer[offset:]))
            await check_heap_recovery(guest, panel, phone, key)
            await check_stack_guard(guest, panel)
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
        await guest.wait_text(b'cellular Retrying(LinkLost)', outage_offset)
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
        assert self.collector_samples[2]['imu1_received_ms'] > floor
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
        assert self.collector_samples[2]['imu1_received_ms'] > hold.result()
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
    scenario_kind = parser.add_mutually_exclusive_group()
    scenario_kind.add_argument('--export', action='store_true', help='qualify authenticated full-rate BLE export')
    parser.add_argument('--export-seconds', type=int, default=60)
    scenario_kind.add_argument('--tiles', action='store_true', help='verify full-rate export during BLE and PPP tile delivery')
    parser.add_argument('--tile-write-loss', action='store_true', help='drop one unacknowledged tile write in --tiles')
    parser.add_argument('--timeout', default=100, type=float)
    parser.add_argument('--log', type=Path, default=Path('target/tracker-coexist-sim/coexist.log'))
    args = parser.parse_args()
    if args.tile_write_loss and not args.tiles:
        parser.error('--tile-write-loss requires --tiles')
    if args.export and args.export_seconds < 60:
        parser.error('--export-seconds must be at least 60 for the three credit-stall controls')
    args.wall_clock = args.shift is None
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nxrs-coexist-', dir='/tmp') as folder:
        directory = Path(folder)
        with collector(directory, close_first=False, idle_timeout=15) as (server, pin), network(
                directory, args.network_qemu, server.server_address[1]) as ethernet, args.log.open('wb') as log:
            scenario = Scenario(server, pin, ethernet)
            test = scenario.exercise_tiles if args.tiles else scenario.exercise
            asyncio.run(run(args, log, test, scenario.attach))


if __name__ == '__main__':
    main()
