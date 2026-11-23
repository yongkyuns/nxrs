"""Run the production dashboard with NuttX drivers and an independent BLE peer."""
import argparse
import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import re
import ssl
import struct
import subprocess
import tempfile
import time

from bumble.controller import Controller
from bumble.core import AdvertisingData, PhysicalTransport, UUID
from bumble.device import Device, Peer
from bumble.gatt import Characteristic
from bumble.hci import HCI_LE_Advertising_Report_Event, HCI_LE_Extended_Advertising_Report_Event
from bumble.hci import Address
from bumble.link import LocalLink
from bumble.transport.common import ParserSource
from PIL import Image
import zxingcpp

from run import listener
from sensors import NeoM9n, check_imus, check_live, check_stale

WIDTH, HEIGHT = 320, 240
SERVICE = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3400')
RX = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3401')
TX = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3402')
EXPORT_SERVICE = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3410')
EXPORT_RX = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3411')
EXPORT_TX = UUID('3CB1DE6E-A955-49B7-BE4F-C4572D3F3412')
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
    def send_acl_data(self, sender, destination, transport, data):
        # Bumble 0.0.236 otherwise uses the sender's random address for all LE ACL.
        if transport != PhysicalTransport.LE:
            return super().send_acl_data(sender, destination, transport, data)
        peer = self.find_le_controller(destination)
        if peer is not None:
            source = sender.le_connections[destination].self_address
            asyncio.get_running_loop().call_soon(peer.on_link_acl_data, source, transport, data)


class H4Controller(Controller):
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


class Guest:
    def __init__(self, process, log):
        self.process = process
        self.log = log
        self.buffer = bytearray()
        self.samples = []
        self.gnss = None
        self.publish_fix = True
        self.tow_ms = 0

    async def read_console(self):
        line = bytearray()
        while data := await self.process.stdout.read(4096):
            self.log.write(data)
            self.log.flush()
            self.buffer.extend(data.replace(b'\r', b''))
            failure = re.search(rb'tracker simulator initialization failed: -?\d+', self.buffer)
            if failure:
                raise RuntimeError(failure[0].decode())
            line.extend(data.replace(b'\r', b''))
            while b'\n' in line:
                end = line.index(b'\n')
                text = bytes(line[:end])
                del line[:end + 1]
                start = text.find(b'{"v":1,')
                if start < 0:
                    continue
                self.samples.append(json.loads(text[start:]))
                if self.gnss and self.gnss.rate == 1 and self.publish_fix:
                    self.tow_ms += 500
                    self.gnss.wire.sendall(self.gnss.fix_packet(self.tow_ms))

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

    async def wait_pixel(self, x, y, rgb):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            image = await self.capture()
            if image.getpixel((x, y)) == luminance(rgb):
                return image
        raise TimeoutError(f'dashboard pixel ({x}, {y}) never became {rgb:#06x}')

    async def pairing_key(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            image = await self.capture()
            code = zxingcpp.read_barcode(image.crop((0, 72, WIDTH, HEIGHT)))
            if code is not None:
                assert re.fullmatch(r'NXRS1:[0-9A-F]{64}', code.text), 'invalid pairing payload'
                return bytes.fromhex(code.text[6:])
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
        assert tx.properties == (Characteristic.Properties.NOTIFY if export else Characteristic.Properties.READ)
        return connection, rx, tx


class SecureStream:
    def __init__(self, rx, tx, key):
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
        self.notifications = None

    async def subscribe(self):
        self.notifications = asyncio.Queue(maxsize=64)
        await self.tx.subscribe(self.notifications.put_nowait)

    async def poll(self):
        while self.outgoing.pending:
            chunk = self.outgoing.read(20)
            await self.rx.write_value(chunk, with_response=True)
        if self.notifications is None:
            data = await self.tx.read_value(no_long_read=True)
        else:
            data = await self.notifications.get()
        if data:
            self.incoming.write(data)
        else:
            await asyncio.sleep(0.003)

    async def handshake(self):
        while True:
            try:
                self.tls.do_handshake()
                assert self.tls.version() == 'TLSv1.2'
                assert self.tls.cipher()[0] == 'PSK-AES128-GCM-SHA256'
                return
            except ssl.SSLWantReadError:
                await self.poll()

    async def export_record(self):
        while True:
            if self.buffer[:1] == b'\xa5' and len(self.buffer) >= 4:
                size = 4 + int.from_bytes(self.buffer[2:4], 'little')
                assert self.buffer[1] == 1 and 33 <= size <= 532
                if len(self.buffer) >= size:
                    batch = bytes(self.buffer[4:size])
                    del self.buffer[:size]
                    return batch
            elif self.buffer[:1] == b'{' and b'\n' in self.buffer:
                end = self.buffer.index(b'\n')
                snapshot = json.loads(self.buffer[:end])
                del self.buffer[:end + 1]
                return snapshot
            try:
                self.buffer.extend(self.tls.read(768))
            except ssl.SSLWantReadError:
                await self.poll()

    async def sample(self):
        while b'\n' not in self.buffer:
            try:
                self.buffer.extend(self.tls.read(768))
            except ssl.SSLWantReadError:
                await self.poll()
        end = self.buffer.index(b'\n')
        value = json.loads(self.buffer[:end])
        del self.buffer[:end + 1]
        return value


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
        return all(source['state'] == 'live' for source in [*snapshot['imu'], snapshot['gnss']])

    await wait_until(all_live, 'live sensor readings')
    check_live(guest.samples[-1])
    await panel.reveal_menu()
    await panel.wait_pixel(40, 33, 0x57f4)  # selected IMU 1 underline
    await panel.tap(118, 20)
    imu2 = await panel.wait_pixel(0, 0, 0x0883)
    await panel.reveal_menu()
    await panel.wait_pixel(118, 33, 0x57f4)  # selected IMU 2 underline
    await panel.tap(195, 20)
    gnss = await panel.wait_pixel(0, 0, 0x0883)
    assert gnss.crop((0, 40, WIDTH, 140)).tobytes() != imu2.crop((0, 40, WIDTH, 140)).tobytes()
    await panel.reveal_menu()
    await panel.wait_pixel(195, 33, 0x57f4)  # selected GNSS underline
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

    last = None
    for attempt in range(2):
        conn, rx, tx = await connect(phone)
        secure = SecureStream(rx, tx, key)
        await asyncio.wait_for(secure.handshake(), 5)
        sample = await asyncio.wait_for(secure.sample(), 5)
        check_live(sample)
        assert last is None or sample['imu'][0]['seq'] > last
        last = sample['imu'][0]['seq']
        linked = await panel.capture()
        assert zxingcpp.read_barcode(linked.crop((0, 72, WIDTH, HEIGHT))) is None, 'QR still visible'
        if attempt == 0:
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
                       '-no-reboot', '-snapshot', '-qtest-log', '/dev/null',
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
                link = BluetoothLink()
                source = ParserSource()
                if attach_controller is None:
                    controller_wire = Stream(peer_writer)
                    tasks.append(asyncio.create_task(pump(peer_reader, source.parser.feed_data)))
                else:
                    controller_wire = await attach_controller(
                        panel, peer_reader, peer_writer, source.parser.feed_data, tasks)
                H4Controller('NuttX H4', source, controller_wire, link=link, public_address=TRACKER)
                central = PhoneController('test phone', link=link, public_address='F0:F1:F2:F3:F4:02')
                phone = Device.with_hci('test phone', Address('F0:F1:F2:F3:F4:A2'), central, central)
                await phone.power_on()
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
