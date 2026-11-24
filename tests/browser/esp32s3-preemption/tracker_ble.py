"""Independent H4 controller, BLE central, and OpenSSL tracker peer."""

import asyncio
import base64
import hashlib
import math
import re
from io import BytesIO

from bumble.device import Device
from bumble.hci import Address
from bumble.transport.common import ParserSource
from PIL import Image
import zxingcpp

from dashboard import BluetoothLink, H4Controller, PhoneController, SecureStream, TRACKER, connect, connect_map_export
from sensors import check_imus, check_live
import tile_fixture


class BlePeer:
    def __init__(self, *, tiles, osm, zoom, move_mps=None):
        self.pending = bytearray()
        self.source = ParserSource()
        link = BluetoothLink()
        self.controller = H4Controller(
            "ESP-EMU H4", self.source, self, link=link, public_address=TRACKER,
        )
        central = PhoneController("browser test phone", link=link, public_address="F0:F1:F2:F3:F4:02")
        self.phone = Device.with_hci(
            "browser test phone", Address("F0:F1:F2:F3:F4:A2"), central, central,
        )
        self.key = asyncio.get_running_loop().create_future()
        self.phase = "starting BLE central"
        self.tiles = tiles
        self.osm = osm
        self.zoom = zoom
        self.move_mps = move_mps
        self.latitude, self.longitude = (51.5074, -0.12145) if osm else (48.1173, -11.5166667)
        if tiles:
            center = tile_fixture.tile_center(zoom, self.latitude, self.longitude)
            self.latitude = center["latitude_deg"]
            self.longitude = center["longitude_deg"]
        if move_mps is not None and move_mps < 0:
            scale = (1 << zoom) / 360
            boundary = (int((180 + self.longitude) * scale) + 1) / scale - 180
            self.longitude = 2 * boundary - self.longitude
        self.fix = {"latitude_deg": self.latitude, "longitude_deg": self.longitude}
        self.map_ready = False
        self.map_frames = {}
        self.want_paint = None
        self.last_panel = None
        self.elapsed_ms = 0
        self.motion = None

    def on_packet(self, packet):
        self.pending.extend(packet)

    async def exchange(self, data):
        if data:
            self.source.parser.feed_data(bytes(data))
        await asyncio.sleep(0)
        reply = list(self.pending)
        self.pending.clear()
        return reply

    def panel(self, png):
        if self.key.done():
            return True
        with Image.open(BytesIO(base64.b64decode(png))) as image:
            code = zxingcpp.read_barcode(image.convert("L").crop((0, 72, 320, 240)))
        if code is None:
            return False
        if not re.fullmatch(r"NXRS1:[0-9A-F]{64}", code.text):
            raise ValueError("invalid tracker pairing code")
        self.key.set_result(bytes.fromhex(code.text[6:]))
        return True

    def position(self, elapsed_ms):
        self.elapsed_ms = elapsed_ms
        if self.motion is not None:
            self.fix.update(self.motion.position(elapsed_ms))
        return self.fix

    def map_panel(self, png, elapsed_ms):
        self.elapsed_ms = elapsed_ms
        if self.motion is not None:
            self.motion.observe(png, elapsed_ms)
        if self.want_paint is not None:
            self.last_panel = png
            name, pixels = self.want_paint
            with Image.open(BytesIO(base64.b64decode(png))) as image:
                if all(image.getpixel(point)[:3] == rgb for point, rgb in pixels):
                    self.map_frames[name] = png
                    self.want_paint = None
        return self.map_ready

    async def check_crossing(self, session, last):
        self.phase = "opening GNSS and waiting for the current tile"
        self.map_ready = True
        world = 1 << self.zoom
        scale = world / 360
        latitude = math.radians(self.latitude)
        x = int((180 + self.longitude) * scale)
        y = int((1 - math.asinh(math.tan(latitude)) / math.pi) / 2 * world)
        direction = -1 if self.move_mps is not None and self.move_mps < 0 else 1
        boundary = (x + max(direction, 0)) / scale - 180
        current, neighbor = [self.zoom, x, y], [self.zoom, x + direction, y]
        tiles = {}

        async def request(tile):
            while True:
                sample = await session.sample()
                if sample.get("tile") == tile:
                    check_imus(sample)
                    assert sample["gnss"]["state"] == "live"
                    return sample

        async def load(tile, color):
            pixels = await asyncio.to_thread(self.osm, *tile) if self.osm else tile_fixture.solid(color)
            assert len(pixels) == tile_fixture.BODY_BYTES
            tiles[tile[1]] = pixels

        def color_at(view_east, column, row):
            left = 75 - round(view_east * 170)
            tile, pixel = divmod(column - left, 170)
            column = pixel * tile_fixture.SIDE // 170
            row = (row - 38) * tile_fixture.SIDE // 170
            rgb = tile_fixture.color_at(tiles[x + tile], column, row)
            red, green, blue = rgb >> 11, (rgb >> 5) & 63, rgb & 31
            return round(red * 255 / 31), round(green * 255 / 63), round(blue * 255 / 31)

        async def paint(name, view_east, points, tile=None):
            pixels = [(point, color_at(view_east, *point)) for point in points]
            pixels.append(((160, 123), (255, 182, 49)))  # RGB565 position marker (0xfda6)
            self.want_paint = (name, pixels)
            if tile:
                await session.send_tile(*tile, pixels=tiles[tile[1]])
            while self.want_paint is not None:
                await session.sample()

        await request(current)
        await load(current, 0x001f)
        await paint("initial", 0, [(90, 50), (120, 80), (210, 60)], current)
        if self.move_mps is not None:
            self.motion = TimedCrossing(self.longitude, boundary, self.latitude, scale,
                                        self.move_mps, self.elapsed_ms)
            self.phase = "awaiting the next tile request during continuous motion"
            early = await request(neighbor)
            request_ms = self.elapsed_ms
            await load(neighbor, 0xf800)
            self.motion.color_at = color_at
            self.phase = "sending and verifying the next tile during continuous motion"
            reply_bytes = await session.send_tile(*neighbor, pixels=tiles[neighbor[1]])
            self.motion.sent_ms = self.elapsed_ms
            while self.motion.painted_ms is None or self.motion.crossed_ms is None:
                await session.sample()
            self.map_frames["next-tile"] = self.motion.frame
            return {
                "currentTile": current,
                "nextTile": neighbor,
                "tileSource": "OpenStreetMap HTTPS (warm HTTP cache)",
                "speedMps": self.move_mps,
                "motionStartMs": self.motion.start_ms,
                "requestMs": request_ms,
                "sendCompleteMs": self.motion.sent_ms,
                "replyBytes": reply_bytes,
                "crossingMs": self.motion.crossed_ms,
                "paintedMs": self.motion.painted_ms,
                "leadMs": self.motion.crossed_ms - self.motion.painted_ms,
                "longitudeAtRequest": early["gnss"]["lon_deg"],
                "boundaryLongitude": boundary,
                "panelFrames": list(self.map_frames),
            }
        distance = min(0.0003, (boundary - self.longitude) / 2)
        before = boundary - distance
        self.fix.update(longitude_deg=before, speed_mps=15)
        self.phase = "waiting for a request ahead of the boundary"
        early = await request(neighbor)
        assert early["gnss"]["lon_deg"] < boundary
        await load(neighbor, 0xf800)
        self.phase = "checking guest LCD before the boundary"
        await paint("before-crossing", 0.5 - distance * scale,
                    [(105, 60), (160, 50), (220, 50), (230, 90)], neighbor)
        self.fix.update(longitude_deg=boundary + 0.00015, speed_mps=15)
        self.phase = "checking guest LCD after the boundary"
        await paint("after-crossing", 0.5 + 0.00015 * scale,
                    [(90, 50), (125, 83), (160, 50), (220, 90)])
        while True:
            sample = await session.sample()
            if sample["gnss"]["state"] == "live" and sample["gnss"]["lon_deg"] > boundary:
                assert all(imu["state"] == "live" and imu["seq"] > previous["seq"]
                           for imu, previous in zip(sample["imu"], last["imu"]))
                break
        return {"currentTile": current, "nextTile": neighbor,
                "tileSource": "OpenStreetMap HTTPS" if self.osm else "synthetic",
                "tileSha256": [hashlib.sha256(tiles[key]).hexdigest() for key in (x, x + 1)],
                "boundaryLongitude": boundary, "longitudeAtRequest": early["gnss"]["lon_deg"],
                "longitudeAfterCrossing": sample["gnss"]["lon_deg"],
                "imuSequencesBefore": [imu["seq"] for imu in last["imu"]],
                "imuSequencesAfter": [imu["seq"] for imu in sample["imu"]],
                "panelFrames": list(self.map_frames)}

    async def run(self, timeout):
        await self.phone.power_on()
        async with asyncio.timeout(timeout):
            self.phase = "waiting for dashboard QR"
            key = await self.key
            self.phase = "scanning and discovering tracker GATT"
            self.phase = "authenticating TLS-PSK"
            if self.tiles:
                connection, session = await connect_map_export(self.phone, key)
            else:
                connection, rx, tx = await connect(self.phone)
                session = SecureStream(rx, tx, key)
                await session.handshake()
            samples = []
            self.phase = "reading authenticated telemetry"
            while len(samples) < 3:
                sample = await session.sample()
                if any(source["state"] != "live" for source in [*sample["imu"], sample["gnss"]]):
                    continue
                check_live(sample, latitude_deg=self.latitude, longitude_deg=self.longitude)
                if samples:
                    previous = samples[-1]
                    assert all(current["seq"] > prior["seq"]
                               for current, prior in zip(sample["imu"], previous["imu"])), sample
                    assert sample["gnss"]["seq"] > previous["gnss"]["seq"], sample
                samples.append(sample)
            crossing = await self.check_crossing(session, samples[-1]) if self.tiles else None
            if self.tiles:
                self.phase = "checking gap-free sensor export across the crossing"
                while min(session.sample_counts[:2]) < 200 or session.sample_counts[2] < 4:
                    await session.sample()
                crossing["exportSamplesWithoutGaps"] = session.sample_counts
                crossing["exportRatesHz"] = session.export_rates()
                crossing["maxExportBatchGapHostMs"] = round(session.max_batch_gap * 1000)
            self.phase = "disconnecting BLE central"
            await connection.disconnect()
        return {"qrDecoded": True, "tls": session.tls.version(),
                "cipher": session.tls.cipher()[0], "samples": samples, "tileCrossing": crossing}


class TimedCrossing:
    def __init__(self, longitude, boundary, latitude, scale, speed, start_ms):
        self.start = longitude
        self.boundary = boundary
        self.scale = scale
        self.speed = abs(speed)
        self.direction = 1 if speed > 0 else -1
        self.start_ms = start_ms
        self.deg_per_ms = speed / (1000 * 111_319.49 * math.cos(math.radians(latitude)))
        self.end = boundary + self.direction * 0.18 / scale
        self.recent = []
        self.color_at = None
        self.sent_ms = None
        self.crossed_ms = None
        self.painted_ms = None
        self.frame = None

    def position(self, elapsed_ms):
        longitude = self.start + (elapsed_ms - self.start_ms) * self.deg_per_ms
        stopped = self.direction * (longitude - self.end) >= 0
        if stopped:
            longitude = self.end
        self.recent = [*self.recent[-1:], longitude]
        if self.direction * (longitude - self.boundary) >= 0 and self.crossed_ms is None:
            self.crossed_ms = elapsed_ms
        return {"longitude_deg": longitude, "speed_mps": 0 if stopped else self.speed}

    def observe(self, png, elapsed_ms):
        if self.sent_ms is None or self.painted_ms is not None:
            return
        points = [(226, 50), (239, 61), (232, 87), (241, 105)]
        if self.direction < 0:
            points = [(319 - x, y) for x, y in points]
        with Image.open(BytesIO(base64.b64decode(png))) as image:
            for longitude in self.recent:
                offset = 0.5 * self.direction + (longitude - self.boundary) * self.scale
                seam = 75 + round((max(self.direction, 0) - offset) * 170)
                if all((x >= seam) == (self.direction > 0)
                       and image.getpixel((x, y))[:3] == self.color_at(offset, x, y)
                       for x, y in points):
                    self.painted_ms = elapsed_ms
                    self.frame = png
                    return
