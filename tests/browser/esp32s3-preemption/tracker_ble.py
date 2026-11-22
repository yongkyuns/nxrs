"""Independent H4 controller, BLE central, and OpenSSL tracker peer."""

import asyncio
import base64
import re
from io import BytesIO

from bumble.controller import Controller
from bumble.device import Device
from bumble.hci import Address
from bumble.transport.common import ParserSource
from PIL import Image
import zxingcpp

from dashboard import BluetoothLink, H4Controller, SecureStream, TRACKER, connect
from sensors import check_live


class BlePeer:
    def __init__(self):
        self.pending = bytearray()
        self.source = ParserSource()
        link = BluetoothLink()
        self.controller = H4Controller(
            "ESP-EMU H4", self.source, self, link=link, public_address=TRACKER,
        )
        central = Controller("browser test phone", link=link, public_address="F0:F1:F2:F3:F4:02")
        self.phone = Device.with_hci(
            "browser test phone", Address("F0:F1:F2:F3:F4:A2"), central, central,
        )
        self.key = asyncio.get_running_loop().create_future()
        self.phase = "starting BLE central"

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

    async def run(self, timeout):
        await self.phone.power_on()
        async with asyncio.timeout(timeout):
            self.phase = "waiting for dashboard QR"
            key = await self.key
            self.phase = "scanning and discovering tracker GATT"
            connection, rx, tx = await connect(self.phone)
            session = SecureStream(rx, tx, key)
            self.phase = "authenticating TLS-PSK"
            await session.handshake()
            samples = []
            self.phase = "reading authenticated telemetry"
            while len(samples) < 3:
                sample = await session.sample()
                if any(source["state"] != "live" for source in [*sample["imu"], sample["gnss"]]):
                    continue
                check_live(sample)
                if samples:
                    previous = samples[-1]
                    assert all(current["seq"] > prior["seq"]
                               for current, prior in zip(sample["imu"], previous["imu"])), sample
                    assert sample["gnss"]["seq"] > previous["gnss"]["seq"], sample
                samples.append(sample)
            self.phase = "disconnecting BLE central"
            await connection.disconnect()
        return {"qrDecoded": True, "tls": session.tls.version(),
                "cipher": session.tls.cipher()[0], "samples": samples}
