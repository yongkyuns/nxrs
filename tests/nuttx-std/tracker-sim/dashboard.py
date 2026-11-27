"""Run the production dashboard with NuttX drivers and an independent BLE peer."""
import argparse
import asyncio
import base64
from collections import deque
from dataclasses import replace
import json
import math
from pathlib import Path
import re
import ssl
import struct
import subprocess
import tempfile
import time

from bumble.att import ATT_Error, ErrorCode
from bumble.controller import Controller
from bumble.core import AdvertisingData, PhysicalTransport, UUID
from bumble.device import Device, Peer
from bumble.gatt import Characteristic
from bumble.hci import (
    HCI_Command_Complete_Event, HCI_Command_Status_Event,
    HCI_LE_Advertising_Report_Event, HCI_LE_Extended_Advertising_Report_Event,
    HCI_Number_Of_Completed_Packets_Event,
)
from bumble.hci import Address
from bumble.link import LocalLink
from bumble.transport.common import ParserSource
from PIL import Image
import zxingcpp

from run import listener
import tile_fixture
from sensors import NeoM9n, check_imus, check_live, check_stale
from tracker_protocol import NoFix, decode_batch, preferred_version

WIDTH, HEIGHT = 320, 240
SERVICE = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3400')
RX = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3401')
TX = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3402')
EXPORT_SERVICE = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3420')
EXPORT_RX = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3421')
EXPORT_TX = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3422')
TRACKER = Address('F0:F1:F2:F3:F4:01', Address.PUBLIC_DEVICE_ADDRESS)

def luminance(rgb):
    red = (rgb >> 11) * 255 // 31
    green = ((rgb >> 5) & 63) * 255 // 63
    blue = (rgb & 31) * 255 // 31
    return (54 * red + 183 * green + 19 * blue) // 256


class Stream:
    def __init__(self, writer):
        self.writer = writer

    def sendall(self, data):
        self.writer.write(data)

    on_packet = sendall


class BluetoothLink(LocalLink):
    notifications_to_drop = 0
    acknowledgements_to_drop = 0
    drop_ingress_at = None
    notification_rate = 0
    paced_notifications = 0
    next_notification = 0.0
    notification_end = 0
    replayed_bytes = 0
    spaced_losses_remaining = 0
    next_spaced_loss = 0

    def send_acl_data(self, sender, destination, transport, data):
        # Bumble 0.0.236 otherwise uses the sender's random address for all LE ACL.
        if transport != PhysicalTransport.LE:
            return super().send_acl_data(sender, destination, transport, data)
        if self.notifications_to_drop and data[2:5] == b'\x04\x00\x1b':
            self.notifications_to_drop -= 1
            return
        if self.acknowledgements_to_drop and data[2:8] == b'\x04\x00\x52\x08\x00\x01':
            self.acknowledgements_to_drop -= 1
            return
        if (self.drop_ingress_at is not None and data[2:8] == b'\x04\x00\x52\x08\x00\x03'
                and int.from_bytes(data[8:12], 'little') == self.drop_ingress_at):
            self.drop_ingress_at = None
            return
        peer = self.find_le_controller(destination)
        if peer is not None:
            source = sender.le_connections[destination].self_address
            loop = asyncio.get_running_loop()
            if self.notification_rate and data[2:5] == b'\x04\x00\x1b':
                # Pace the ciphertext payload, excluding the L2CAP, ATT and
                # replay headers. Keep emitting link credits and ACKs normally.
                count = len(data) - 11
                self.next_notification = max(loop.time(), self.next_notification) + count / self.notification_rate
                self.paced_notifications += 1
                start = int.from_bytes(data[7:11], 'little')
                end = start + count
                self.replayed_bytes += max(0, min(end, self.notification_end) - start)
                self.notification_end = max(self.notification_end, end)
                if self.spaced_losses_remaining and start >= self.next_spaced_loss:
                    self.next_spaced_loss = end + 5_500
                    self.spaced_losses_remaining -= 1
                    return
                loop.call_at(self.next_notification, peer.on_link_acl_data, source, transport, data)
            else:
                loop.call_soon(peer.on_link_acl_data, source, transport, data)


class H4Controller(Controller):
    withheld_completions = None

    def __init__(self, *args, **kwargs):
        self.boot_hci = deque(maxlen=16)
        super().__init__(*args, **kwargs)

    def on_hci_command_packet(self, command):
        if self.boot_hci is not None:
            self.boot_hci.append(f'command {command.op_code:#06x}')
        super().on_hci_command_packet(command)

    def send_hci_packet(self, packet):
        if self.boot_hci is not None and isinstance(packet, (HCI_Command_Complete_Event, HCI_Command_Status_Event)):
            result = 'complete' if isinstance(packet, HCI_Command_Complete_Event) else f'status {packet.status:#04x}'
            self.boot_hci.append(f'{result} {packet.command_opcode:#06x}')
        if self.withheld_completions is not None and isinstance(packet, HCI_Number_Of_Completed_Packets_Event):
            for handle, count in zip(packet.connection_handles, packet.num_completed_packets):
                self.withheld_completions[handle] = self.withheld_completions.get(handle, 0) + count
        else:
            super().send_hci_packet(packet)

    def hold_acl_credits(self):
        assert self.withheld_completions is None
        self.withheld_completions = {}

    def release_acl_credits(self, individual=False):
        counts = self.withheld_completions
        self.withheld_completions = None
        if counts:
            if individual:
                # A burst of independent events must fit the receiver's
                # control path even when the host I/O pool is nearly full.
                for handle, count in counts.items():
                    for _ in range(count):
                        self.send_hci_packet(HCI_Number_Of_Completed_Packets_Event(
                            connection_handles=[handle], num_completed_packets=[1]))
            else:
                self.send_hci_packet(HCI_Number_Of_Completed_Packets_Event(
                    connection_handles=list(counts), num_completed_packets=list(counts.values())))
        return sum(counts.values())

    def on_hci_host_number_of_completed_packets_command(self, _command):
        # The virtual controller queues no host-bound ACL credits. The HCI
        # acknowledgement has no response event.
        return None


class PhoneController(Controller):
    def send_hci_packet(self, packet):
        # Bumble 0.0.236 synthesizes a scan response from the advertising
        # payload. Use the actual scan response configured by the NuttX host.
        scan_types = {
            HCI_LE_Advertising_Report_Event: HCI_LE_Advertising_Report_Event.EventType.SCAN_RSP,
            HCI_LE_Extended_Advertising_Report_Event:
                HCI_LE_Extended_Advertising_Report_Event.EventType.SCAN_RESPONSE,
        }
        if (kind := scan_types.get(type(packet))) is not None:
            report, = packet.reports
            if report.event_type == kind:
                if not self.le_scan_type:
                    return
                advertiser = self.link.find_le_controller(report.address)
                response = advertiser.le_legacy_advertiser.scan_response_data
                packet = type(packet)([replace(report, data=response)])
        super().send_hci_packet(packet)


class TrackerStartupError(RuntimeError):
    pass


class Guest:
    def __init__(self, process, log):
        self.process = process
        self.log = log
        self.buffer = bytearray()
        self.controller = None
        self.samples = []
        self.gnss = None
        self.publish_fix = True
        self.moving_fix = {}
        self.tow_ms = 0

    async def read_console(self):
        line = bytearray()
        while data := await self.process.stdout.read(4096):
            self.log.write(data)
            self.log.flush()
            self.buffer.extend(data.replace(b'\r', b''))
            line.extend(data.replace(b'\r', b''))
            if self.controller is not None and self.controller.boot_hci is not None and b'nsh>' in line:
                self.controller.boot_hci = None
            while b'\n' in line:
                end = line.index(b'\n')
                text = bytes(line[:end])
                del line[:end + 1]
                if text.startswith(b'tracker simulator initialization failed: '):
                    recent = self.controller.boot_hci if self.controller is not None else None
                    progress = ', '.join(recent or ()) or 'no HCI commands received'
                    raise TrackerStartupError(f'{text.decode()}\nH4 startup: {progress}')
                start = text.find(b'{"v":1,')
                if start < 0:
                    continue
                self.samples.append(json.loads(text[start:]))
                if self.gnss and self.gnss.rate == 1 and self.publish_fix:
                    self.tow_ms += 500
                    self.gnss.wire.sendall(self.gnss.fix_packet(self.tow_ms, **self.moving_fix))

    def command(self, command):
        self.process.stdin.write(f'{command}\n'.encode())

    async def wait_text(self, marker, offset=0, timeout=10):
        await wait_until(lambda: marker in self.buffer[offset:], marker.decode(), timeout)


async def pump(reader, receive):
    while data := await reader.read(4096):
        receive(data)


async def wait_until(predicate, label, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(label)
        await asyncio.sleep(0.003)


class Panel:
    def __init__(self, reader, writer, addresses):
        self.reader = reader
        self.writer = writer
        self.addresses = addresses
        self.command_lock = asyncio.Lock()

    async def query(self, command):
        async with self.command_lock:
            self.writer.write((command + '\n').encode())
            response = await asyncio.wait_for(self.reader.readline(), 2)
        assert response.startswith(b'OK'), response[:128]
        return response[2:].strip()

    async def word(self, symbol):
        return int(await self.query(f'readl {self.addresses[symbol]:#x}'), 16)

    async def read(self, symbol, count):
        response = await self.query(f'b64read {self.addresses[symbol]:#x} {count}')
        return base64.b64decode(response, validate=True)

    async def capture(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            sequence = await self.word('tracker_frame_sequence')
            if sequence and sequence % 2 == 0:
                packed = await self.read('tracker_pixels', WIDTH * HEIGHT // 2)
                colors = await self.read('tracker_colors', 32)
                if sequence == await self.word('tracker_frame_sequence'):
                    assert await self.word('tracker_model_error') == 0
                    grayscale = [luminance(color) for color, in struct.iter_unpack('<H', colors)]
                    pixels = bytes(grayscale[index] for byte in packed for index in (byte & 15, byte >> 4))
                    return Image.frombytes('L', (WIDTH, HEIGHT), pixels)
            await asyncio.sleep(0.002)
        raise TimeoutError('complete panel frame')

    async def touch(self, x, y, event):
        value = x | y << 16 | event << 30
        await self.query(f'writel {self.addresses["tracker_touch_input"]:#x} {value:#x}')
        reads = await self.word('tracker_touch_reads')
        deadline = time.monotonic() + 2
        while await self.word('tracker_touch_reads') - reads < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError('touch controller poll')
            await asyncio.sleep(0.002)

    async def tap(self, x, y):
        await self.touch(x, y, 0)
        await self.touch(x, y, 1)

    async def reveal_menu(self):
        await self.touch(160, 6, 0)
        await self.touch(160, 65, 2)
        await self.touch(160, 65, 1)

    async def wait_pixel(self, x, y, rgb, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            image = await self.capture()
            if image.getpixel((x, y)) == luminance(rgb):
                return image
        raise TimeoutError(f'dashboard pixel ({x}, {y}) never became {rgb:#06x}')

    async def pairing_key(self, *, previous=None):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            image = await self.capture()
            code = zxingcpp.read_barcode(image.crop((0, 72, WIDTH, HEIGHT)))
            if code is not None:
                assert re.fullmatch(r'NXRS1:[0-9A-F]{64}', code.text), 'invalid pairing payload'
                key = bytes.fromhex(code.text[6:])
                if key != previous:
                    return key
            await asyncio.sleep(0.003)
        raise TimeoutError('decodable dashboard QR')


async def connect(device, *, export=False):
    seen = asyncio.Event()
    schema = EXPORT_SERVICE if export else SERVICE

    def advertisement(event):
        lists = event.data.get_all(AdvertisingData.INCOMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS)
        if (event.address == TRACKER and any(schema in services for services in lists)
                and (event.is_connectable if export else event.is_scan_response)):
            seen.set()

    device.on('advertisement', advertisement)
    try:
        await device.start_scanning(legacy=True, active=not export, filter_duplicates=False)
        try:
            await asyncio.wait_for(seen.wait(), 5)
        finally:
            await device.stop_scanning()
    finally:
        device.remove_listener('advertisement', advertisement)
    async with asyncio.timeout(5):
        connection = await device.connect(TRACKER)
        services = await Peer(connection).discover_service(schema)
        assert len(services) == 1, 'tracker service missing'
        characteristics = await services[0].discover_characteristics()
        rx, tx = (next(c for c in characteristics if c.uuid == uuid)
                  for uuid in ((EXPORT_RX, EXPORT_TX) if export else (RX, TX)))
        assert (rx.handle, tx.handle) == ((8, 10) if export else (3, 5))
        expected_rx = Characteristic.Properties.WRITE
        if export:
            expected_rx |= Characteristic.Properties.WRITE_WITHOUT_RESPONSE
        assert rx.properties == expected_rx
        assert tx.properties == (Characteristic.Properties.NOTIFY if export else Characteristic.Properties.READ)
        return connection, rx, tx


class SecureStream:
    def __init__(self, rx, tx, key, *, retain_out_of_order=True):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers('PSK-AES128-GCM-SHA256')
        context.set_psk_client_callback(lambda _: ('nxrs-tracker-v1', key))
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.tls = context.wrap_bio(self.incoming, self.outgoing, server_hostname=None)
        self.rx = rx
        self.tx = tx
        self.buffer = bytearray()
        self.no_fix_count = 0
        self.last_no_fix = None
        self.notifications = None
        self.notification_overflow = False
        self.offset = 0
        self.last_acknowledged = 0
        self.out_of_order = 0
        self.retain_out_of_order = retain_out_of_order
        self.buffered = {}
        self.acknowledgement_pending = False
        self.acknowledge_at = 0.0
        self.ingress_payload = 19
        self.windowed_writes = False
        self.ingress_offset = 0
        self.acknowledged_writes = 0
        self.write_retries = 0
        self.scales = (None, None)
        self.sample_counts = [0] * 3
        self.sample_sequences = [None] * 3
        self.sample_ticks = [None] * 3
        self.last_batch_received = None
        self.max_batch_gap = 0

    async def subscribe(self):
        self.notifications = asyncio.Event()

        def receive(packet):
            if not 4 < len(packet) <= 244 or self.incoming.pending + len(self.buffered) + len(packet) - 4 > 8192:
                self.notification_overflow = True
                self.notifications.set()
                return
            offset = int.from_bytes(packet[:4], 'little')
            skip = (self.offset - offset) & 0xffffffff
            if skip < len(packet) - 4:
                data = bytearray(packet[4 + skip:])
                if self.buffered:
                    for index in range(len(data)):
                        self.buffered.pop((self.offset + index) & 0xffffffff, None)
                self.offset = (self.offset + len(data)) & 0xffffffff
                while self.offset in self.buffered:
                    data.append(self.buffered.pop(self.offset))
                    self.offset = (self.offset + 1) & 0xffffffff
                self.incoming.write(data)
            elif skip > 0x7fffffff:
                self.out_of_order += 1
                lead = (offset - self.offset) & 0xffffffff
                if self.retain_out_of_order and lead + len(packet) - 4 <= 2048:
                    for index, byte in enumerate(packet[4:]):
                        self.buffered.setdefault((offset + index) & 0xffffffff, byte)
            self.acknowledgement_pending = True
            self.notifications.set()

        await self.tx.subscribe(receive)

    async def flush(self):
        framed = self.notifications is not None
        while self.outgoing.pending:
            limit = self.ingress_payload * (4 if self.windowed_writes else 1) if framed else 20
            chunk = self.outgoing.read(limit)
            packets = [b'\0' + chunk if framed else chunk]
            if self.windowed_writes:
                size = self.ingress_payload
                packets = [b'\x03' + ((self.ingress_offset + offset) & 0xffffffff).to_bytes(4, 'little')
                           + chunk[offset:offset + size] for offset in range(0, len(chunk), size)]
            for attempt in range(33):
                try:
                    for packet in packets[:-1]:
                        await self.rx.write_value(packet, with_response=False)
                    await self.rx.write_value(packets[-1], with_response=True)
                    self.acknowledged_writes += 1
                    self.ingress_offset = (self.ingress_offset + len(chunk)) & 0xffffffff
                    break
                except ATT_Error as error:
                    if error.error_code != ErrorCode.UNLIKELY_ERROR or attempt == 32:
                        raise
                    self.write_retries += 1
                    if framed:
                        await self.acknowledge()
                    else:
                        self.incoming.write(await self.tx.read_value(no_long_read=True))
                    await asyncio.sleep(0.015)
            if framed:
                await self.acknowledge()

    async def acknowledge(self):
        now = asyncio.get_running_loop().time()
        if self.acknowledgement_pending and now >= self.acknowledge_at:
            self.acknowledgement_pending = False
            self.acknowledge_at = now + 0.015
            offset = self.offset
            await self.rx.write_value(b'\x01' + offset.to_bytes(4, 'little'), with_response=False)
            self.last_acknowledged = offset

    async def poll(self):
        await self.flush()
        if self.notifications is None:
            data = await self.tx.read_value(no_long_read=True)
            if data:
                self.incoming.write(data)
            else:
                await asyncio.sleep(0.003)
            return
        # Use the same cumulative acknowledgment interval as the iPhone.
        while True:
            assert not self.notification_overflow, 'test peer notification storage overflowed or received an invalid frame'
            await self.acknowledge()
            if self.notifications.is_set():
                self.notifications.clear()
                return
            if not self.acknowledgement_pending:
                await self.notifications.wait()
                continue
            try:
                async with asyncio.timeout_at(self.acknowledge_at):
                    await self.notifications.wait()
            except TimeoutError:
                pass

    async def handshake(self):
        while True:
            try:
                self.tls.do_handshake()
                assert self.tls.version() == 'TLSv1.2'
                assert self.tls.cipher()[0] == 'PSK-AES128-GCM-SHA256'
                return
            except ssl.SSLWantReadError:
                await self.poll()

    def export_ready(self):
        while True:
            if self.buffer[:1] == b'\xa5' and len(self.buffer) >= 4:
                size = 4 + int.from_bytes(self.buffer[2:4], 'little')
                version = self.buffer[1]
                assert version in (1, 2, 3, 4), 'unsupported tracker export version'
                terminal = (version == 1 and size == 4) or (version >= 2 and size == 9) or (version == 4 and size == 6)
                assert terminal or 33 <= size <= (604 if version >= 3 else 532), 'invalid tracker frame length'
                if len(self.buffer) >= size:
                    batch = bytes(self.buffer[4:size])
                    del self.buffer[:size]
                    return decode_batch(batch, version, self.scales), len(batch)
            elif self.buffer[:1] == b'{' and b'\n' in self.buffer:
                end = self.buffer.index(b'\n')
                return self.take_snapshot(end)
            try:
                self.buffer.extend(self.tls.read(768))
            except ssl.SSLWantReadError:
                return None

    async def export_record(self):
        while (record := self.export_ready()) is None:
            await self.poll()
        return record

    def count_export(self, record):
        now = time.monotonic()
        if self.last_batch_received is not None:
            self.max_batch_gap = max(self.max_batch_gap, now - self.last_batch_received)
        self.last_batch_received = now
        measurements, _ = record
        for measurement in measurements:
            if isinstance(measurement, NoFix):
                self.observe_no_fix(measurement)
                continue
            source, seq, tick, _, _ = measurement
            index = source - 1
            previous = self.sample_sequences[index]
            assert previous is None or seq == ((previous + 1) & 0xffffffff), (source, previous, seq)
            self.sample_counts[index] += 1
            self.sample_sequences[index] = seq
            self.sample_ticks[index] = (tick if previous is None else self.sample_ticks[index][0], tick)

    def observe_no_fix(self, status):
        self.no_fix_count += 1
        self.last_no_fix = status

    def export_rates(self):
        return [(count - 1) * 1000 / ((end - start) & 0xffffffff)
                for count, (start, end) in zip(self.sample_counts, self.sample_ticks)]

    async def send_tile(self, zoom, x, y, color=0x001f, pixels=None):
        pixels = tile_fixture.solid(color) if pixels is None else pixels
        assert len(pixels) == tile_fixture.BODY_BYTES, 'unexpected tile size'
        packet = tile_fixture.REQUEST.pack(tile_fixture.MAGIC, zoom, x, y) + bytes([0]) + tile_fixture.pack(pixels)
        for offset in range(0, len(packet), 512):
            self.tls.write(packet[offset:offset + 512])
            await self.poll()
            if self.notifications is not None:
                while (record := self.export_ready()) is not None:
                    if isinstance(record, tuple):
                        self.count_export(record)
        return len(packet)

    async def sample(self):
        if self.notifications is not None:
            while isinstance(record := await self.export_record(), tuple):
                self.count_export(record)
            return record
        while b'\n' not in self.buffer:
            try:
                self.buffer.extend(self.tls.read(768))
            except ssl.SSLWantReadError:
                await self.poll()
        end = self.buffer.index(b'\n')
        return self.take_snapshot(end)

    def take_snapshot(self, end):
        value = json.loads(self.buffer[:end])
        del self.buffer[:end + 1]
        if 'imu_scale_bits' in value:
            self.scales = tuple(
                None if bits is None else struct.unpack('<ff', struct.pack('<II', *bits))
                for bits in value['imu_scale_bits']
            )
        return value


async def connect_map_export(phone, key):
    connection, rx, tx = await connect(phone, export=True)
    mtu = await Peer(connection).request_mtu(247)
    stream = SecureStream(rx, tx, key)
    await stream.subscribe()
    limit = min(244, mtu - 3)
    if limit > 20:
        for probe, overhead in ((4, 5), (2, 1)):
            try:
                await rx.write_value(bytes([probe]) + bytes(limit - 1), with_response=True)
            except ATT_Error as error:
                assert error.error_code == ErrorCode.UNLIKELY_ERROR, error
            else:
                stream.windowed_writes = probe == 4
                stream.ingress_payload = limit - overhead
                break
    await stream.handshake()
    capabilities = await stream.sample()
    stream.tls.write(b'NXRS' + bytes([preferred_version(capabilities)]))
    await stream.flush()
    return connection, stream


async def scenario(guest, panel, phone, args):
    await guest.wait_text(b'nsh>')
    offset = len(guest.buffer)
    guest.command('motion_tracker --battery')
    await guest.wait_text(b'Battery 73% (Charging, external power present)', offset)
    await guest.wait_text(b'nsh>', offset)
    offset = len(guest.buffer)
    started = time.monotonic()
    guest.command(f'motion_tracker --frames {args.frames} --interval-ms 1000 --stream-usb')
    def all_live():
        if not guest.samples:
            return False
        snapshot = guest.samples[-1]
        return all(source['state'] in ('live', 'received') for source in [*snapshot['imu'], snapshot['gnss']])

    await wait_until(all_live, 'live sensor readings')
    check_live(guest.samples[-1])
    await panel.reveal_menu()
    await panel.wait_pixel(40, 33, 0x57f4)  # selected IMU 1 underline
    await panel.tap(118, 20)
    imu2 = await panel.wait_pixel(0, 0, 0x0883)
    await panel.reveal_menu()
    await panel.wait_pixel(118, 33, 0x57f4)  # selected IMU 2 underline
    await panel.tap(195, 20)
    guest.gnss.wire.sendall(guest.gnss.satellite_packet(guest.tow_ms, [(0, 3, 45), (2, 11, 25)]))
    await panel.wait_pixel(100, 196, 0x57f4)  # GPS bar
    await panel.wait_pixel(238, 196, 0x661f)  # shorter Galileo bar
    await panel.reveal_menu()
    await panel.wait_pixel(195, 33, 0x57f4)  # selected GNSS underline
    await panel.tap(155, 20)
    gnss = await panel.wait_pixel(0, 0, 0x0883)
    assert gnss.crop((0, 40, WIDTH, 140)).tobytes() != imu2.crop((0, 40, WIDTH, 140)).tobytes()
    await panel.reveal_menu()
    await panel.wait_pixel(155, 33, 0x57f4)  # selected map underline
    await panel.tap(275, 20)
    cancelled_key = await panel.pairing_key()
    await panel.tap(275, 20)
    await panel.wait_pixel(154, 85, 0x1126)  # GNSS plot background replaces QR
    await panel.reveal_menu()
    await panel.wait_pixel(275, 33, 0x57f4)
    await panel.tap(275, 20)
    key = await panel.pairing_key()
    assert key != cancelled_key, 'reopening pairing must issue a fresh key'
    print('PASS: dashboard gesture, tabs, cancel and fresh QR decoded from SPI pixels', flush=True)

    conn, rx, tx = await connect(phone)
    disconnected = asyncio.Event()
    conn.once(conn.EVENT_DISCONNECTION, lambda _: disconnected.set())
    wrong = SecureStream(rx, tx, cancelled_key)
    handshake = asyncio.create_task(wrong.handshake())
    try:
        await asyncio.wait_for(disconnected.wait(), 5)
        if handshake.done():
            assert handshake.exception() is not None, 'invalid key authenticated'
    finally:
        handshake.cancel()
        await asyncio.gather(handshake, return_exceptions=True)
    print('PASS: wrong pairing key rejected', flush=True)

    map_fix = tile_fixture.tile_center(18, 48.1173, -11.5166667)
    guest.moving_fix = map_fix
    last = None
    for attempt in range(2):
        conn, rx, tx = await connect(phone)
        secure = SecureStream(rx, tx, key)
        await asyncio.wait_for(secure.handshake(), 5)
        sample = await asyncio.wait_for(secure.sample(), 5)
        if attempt == 0:
            while abs(sample['gnss']['lon_deg'] - map_fix['longitude_deg']) >= 1e-6:
                sample = await asyncio.wait_for(secure.sample(), 5)
        check_live(sample, **(map_fix if attempt == 0 else {}))
        assert last is None or sample['imu'][0]['seq'] > last
        last = sample['imu'][0]['seq']
        linked = await panel.capture()
        assert zxingcpp.read_barcode(linked.crop((0, 72, WIDTH, HEIGHT))) is None, 'QR still visible'
        if attempt == 0:
            async with asyncio.timeout(12):
                while 'tile' not in sample:
                    sample = await secure.sample()
                # Independent projection for the fixed GNSS fixture.
                lat = math.radians(map_fix['latitude_deg'])
                zoom = 18
                world = 1 << zoom
                longitude = map_fix['longitude_deg']
                expected = [zoom, int((180 + longitude) / 360 * world),
                            int((1 - math.asinh(math.tan(lat)) / math.pi) / 2 * world)]
                assert sample['tile'] == expected
                await secure.send_tile(*expected)
                await panel.wait_pixel(90, 50, 0x001f, timeout=5)  # blue test tile on the SPI panel
                last = sample['imu'][0]['seq']
                sample = await secure.sample()
                check_live(sample, **map_fix)
                assert sample['imu'][0]['seq'] > last
                while "tile" in sample:
                    sample = await secure.sample()
                check_live(sample, **map_fix)
            print('PASS: one location tile renders while authenticated BLE telemetry advances', flush=True)
            async with asyncio.timeout(15):
                boundary = (expected[1] + 1) * 360 / world - 180
                distance = min(0.0003, (boundary - longitude) / 2)
                guest.moving_fix = {**map_fix, 'longitude_deg': boundary - distance, 'speed_mps': 15}
                ahead = [zoom, expected[1] + 1, expected[2]]
                while sample.get('tile') != ahead:
                    sample = await secure.sample()
                assert guest.samples[-1]['gnss']['lon_deg'] < boundary
                await secure.send_tile(*ahead, color=0xf800)
                before = await panel.wait_pixel(220, 50, 0xf800)
                assert before.getpixel((160, 50)) == luminance(0x001f)

                guest.moving_fix = {**map_fix, 'longitude_deg': boundary + 0.00015, 'speed_mps': 15}
                after = await panel.wait_pixel(160, 50, 0xf800)
                assert after.getpixel((90, 50)) == luminance(0x001f)
                for frame in (before, after):
                    assert frame.getpixel((160, 123)) == luminance(0xfda6)
                guest.moving_fix = {}
                while True:
                    sample = await secure.sample()
                    fix = sample['gnss']
                    assert fix['state'] == 'live'
                    if abs(fix['lon_deg'] + 11.5166667) <= 1e-6:
                        break
            print('PASS: visible tile requested before crossing and both tiles rendered across the seam', flush=True)
            count = len(guest.samples)
            await wait_until(lambda: len(guest.samples) >= count + 3, 'paused reader must not stop the tracker')
            check_live(guest.samples[-1])
            resumed = await asyncio.wait_for(secure.sample(), 5)
            check_live(resumed)
            assert resumed['imu'][0]['seq'] > sample['imu'][0]['seq']
            print('PASS: dashboard and sensors continue while the BLE peer pauses reads', flush=True)
            guest.publish_fix = False
            try:
                async with asyncio.timeout(5):
                    while (sample := await secure.sample())['gnss']['state'] != 'stale':
                        check_imus(sample)
                    check_stale(sample)
                    await panel.wait_pixel(90, 50, 0x1126)  # fallback plot
            finally:
                guest.publish_fix = True
            async with asyncio.timeout(5):
                while (sample := await secure.sample())['gnss']['state'] != 'live':
                    check_imus(sample)
            check_live(sample)
            print('PASS: GNSS expires and recovers on the same authenticated link', flush=True)
        await conn.disconnect()
    print('PASS: authenticated TLS telemetry, QR dismissed, reconnect with same key', flush=True)

    summary = f'{args.frames} frames, interval=1000 ms, IMU 1 live, IMU 2 live, GNSS live'
    await guest.wait_text(summary.encode(), offset, args.timeout)
    elapsed = time.monotonic() - started
    print(f'{args.frames - 1} guest seconds of reports in {elapsed:.2f} host seconds', flush=True)
    await guest.wait_text(b'nsh>', offset)
    assert guest.gnss.writes == [1, 7], guest.gnss.writes
    offset = len(guest.buffer)
    guest.command('echo tracker-exit:$?')
    await guest.wait_text(b'tracker-exit:0', offset)

    offset = len(guest.buffer)
    guest.command('motion_tracker --frames 4 --stream-usb')
    await guest.wait_text(b'4 frames, interval=1000 ms, IMU 1 live, IMU 2 live, GNSS live', offset)
    await guest.wait_text(b'nsh>', offset)
    assert guest.gnss.writes == [1, 7, 1, 7], guest.gnss.writes
    guest.command('echo tracker-exit:$?')
    await guest.wait_text(b'tracker-exit:0', offset)
    print('PASS: orderly release, restart and persisted report interval', flush=True)


async def resume_guest(qmp):
    async with asyncio.timeout(5):
        reader, writer = await asyncio.open_unix_connection(qmp)
        try:
            assert 'QMP' in json.loads(await reader.readline()), 'missing QMP greeting'
            for command in ('qmp_capabilities', 'cont'):
                writer.write(json.dumps({'execute': command}).encode() + b'\n')
                await writer.drain()
                while True:
                    reply = json.loads(await reader.readline())
                    if 'event' not in reply:
                        assert 'return' in reply, (command, reply)
                        break
        finally:
            writer.close()
            await writer.wait_closed()


async def run(args, log, exercise=scenario, attach_controller=None):
    output = subprocess.check_output([args.nm, args.image.with_name('nuttx')], text=True)
    addresses = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2].startswith('tracker_'):
            addresses[fields[2]] = int(fields[0], 16)
    with tempfile.TemporaryDirectory(prefix='nxrs-dashboard-', dir='/tmp') as folder:
        path = Path(folder)
        peer_path = path / ('hci' if attach_controller is None else 'modem')
        with listener(path / 'gps') as gps, listener(peer_path) as peer:
            command = [args.qemu, '-machine', 'esp32s3', '-smp', '2', '-display', 'none',
                       '-serial', 'stdio', '-serial', f'unix:{path / "gps"}',
                       '-serial', f'unix:{peer_path}', '-monitor', 'none', '-nic', 'none',
                       '-no-reboot', '-snapshot', '-S', '-qmp', f'unix:{path / "qmp"},server=on,wait=off',
                       '-qtest-log', '/dev/null',
                       '-qtest', f'unix:{path / "qtest"},server=on,wait=off',
                       '-drive', f'file={args.image},if=mtd,format=raw']
            if args.qemu_data:
                command[1:1] = ['-L', args.qemu_data]
            if not args.wall_clock:
                command += ['-icount', f'shift={args.shift},sleep=off']
            process = await asyncio.create_subprocess_exec(
                *command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            guest = Guest(process, log)
            tasks = [asyncio.create_task(guest.read_console())]
            try:
                loop = asyncio.get_running_loop()
                gps.setblocking(False)
                peer.setblocking(False)
                (gps_socket, _), (peer_socket, _) = await asyncio.wait_for(
                    asyncio.gather(loop.sock_accept(gps), loop.sock_accept(peer)), 8)
                gps_reader, gps_writer = await asyncio.open_connection(sock=gps_socket)
                peer_reader, peer_writer = await asyncio.open_connection(sock=peer_socket)
                q_reader, q_writer = await asyncio.open_unix_connection(path / 'qtest')
                panel = Panel(q_reader, q_writer, addresses)
                guest.gnss = NeoM9n(Stream(gps_writer))
                tasks.append(asyncio.create_task(pump(gps_reader, guest.gnss.receive)))
                link = guest.ble_link = BluetoothLink()
                source = ParserSource()
                if attach_controller is None:
                    controller_wire = Stream(peer_writer)
                    tasks.append(asyncio.create_task(pump(peer_reader, source.parser.feed_data)))
                else:
                    controller_wire = await attach_controller(
                        panel, peer_reader, peer_writer, source.parser.feed_data, tasks)
                guest.controller = H4Controller('NuttX H4', source, controller_wire, link=link, public_address=TRACKER)
                central = PhoneController('test phone', link=link, public_address='F0:F1:F2:F3:F4:02')
                phone = Device.with_hci('test phone', Address('F0:F1:F2:F3:F4:A2'), central, central)
                await phone.power_on()
                # NuttX sends HCI commands during boot. Start its timeout only
                # once the host has installed the controller and its pumps.
                await resume_guest(path / 'qmp')
                start = time.monotonic()
                test = asyncio.create_task(exercise(guest, panel, phone, args))
                tasks.append(test)
                done, _ = await asyncio.wait(tasks, timeout=args.timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise TimeoutError('tracker scenario')
                for task in done:
                    task.result()
                assert test in done, 'guest console or UART terminated during the test'
                print(f'PASS: full tracker in {time.monotonic() - start:.2f} host seconds')
            except TrackerStartupError as error:
                if attach_controller is not None:
                    try:
                        error.add_note(await asyncio.wait_for(controller_wire.describe_rings(), 1))
                    except (OSError, AssertionError, ValueError, struct.error):
                        error.add_note('Could not read RAM UART ring offsets')
                raise
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if process.returncode is None:
                    process.kill()
                await process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--qemu', default='qemu-system-xtensa')
    parser.add_argument('--qemu-data')
    parser.add_argument('--nm', default='xtensa-esp32s3-elf-nm')
    parser.add_argument('--shift', default=5, type=int)
    parser.add_argument('--wall-clock', action='store_true')
    parser.add_argument('--frames', default=100, type=int)
    parser.add_argument('--timeout', default=45, type=float)
    parser.add_argument('--log', type=Path, default=Path('target/tracker-dashboard-sim/dashboard.log'))
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('wb') as log:
        asyncio.run(run(args, log))


if __name__ == '__main__':
    main()
