"""Physical Motion Tracker bench. Receives v1 telemetry and logs only timing."""

import argparse
import asyncio
import getpass
import hashlib
import itertools
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time


UUID = "3cb1de6e-a955-49b7-be4f-c4572d3f"
SERVICE, INGRESS, EGRESS = (UUID + suffix for suffix in ("3400", "3401", "3402"))
FRAME_LIMIT = 768
SUMMARY_LIMIT = 288


def event(kind, **fields):
    print(json.dumps({"event": kind, "host_s": time.monotonic(), **fields}), flush=True)


def reading(raw):
    if not raw.endswith(b"\n") or len(raw) > FRAME_LIMIT:
        raise ValueError("invalid tracker frame length")
    frame = json.loads(raw)
    if frame["v"] != 1 or len(frame["imu"]) != 2:
        raise ValueError("expected Tracker v1 with two IMUs")
    imus = []
    for imu in frame["imu"]:
        if imu["state"] == "live":
            stamp = [imu["seq"], imu["received_ms"]]
            if any(type(value) is not int or value < 0 for value in stamp):
                raise ValueError("invalid tracker sample metadata")
            imus.append(stamp)
        else:
            imus.append(None)
    return imus


def summary_time(raw):
    if not raw.endswith(b"\n") or len(raw) > SUMMARY_LIMIT:
        raise ValueError("invalid cellular summary length")
    frame = json.loads(raw)
    if frame["v"] != 1 or frame["kind"] != "summary" or len(frame["imu"]) != 2:
        raise ValueError("expected a Tracker v1 cellular summary")
    stamp = frame["imu1_received_ms"]
    if frame["imu"] != ["live", "live"]:
        return None
    if type(stamp) is not int or stamp < 0:
        raise ValueError("invalid tracker summary timestamp")
    return stamp


async def collect(address, port):
    sessions = itertools.count(1)

    async def receive(reader, writer):
        session = next(sessions)
        event("tls_connected", session=session)
        try:
            while raw := await asyncio.wait_for(reader.readline(), 180):
                event("upload", session=session, received_ms=summary_time(raw))
        except (KeyError, OSError, TimeoutError, TypeError, ValueError) as err:
            event("tls_error", session=session, reason=type(err).__name__)
        finally:
            writer.close()
            event("tls_closed", session=session)

    with tempfile.TemporaryDirectory(prefix="tracker-collector-") as directory:
        cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:P-256", "-pkeyopt", "ec_param_enc:named_curve",
            "-nodes", "-sha256", "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-subj", "/CN=tracker-bench",
        ], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(cert, key)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        async with await asyncio.start_server(
            receive, address, port, ssl=context, ssl_handshake_timeout=5,
            limit=FRAME_LIMIT,
        ) as server:
            address, port = server.sockets[0].getsockname()
            event("collector_ready", listen=address, port=port, pin=pin)
            await server.serve_forever()


class TrackerStream:
    def __init__(self, client, key):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("PSK-AES128-GCM-SHA256")
        context.set_psk_client_callback(lambda _: ("nxrs-tracker-v1", key))
        self.client = client
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.tls = context.wrap_bio(self.incoming, self.outgoing)
        self.buffer = bytearray()

    async def exchange(self):
        while self.outgoing.pending:
            await self.client.write_gatt_char(INGRESS, self.outgoing.read(20), response=True)
        chunk = await self.client.read_gatt_char(EGRESS)
        if chunk:
            self.incoming.write(chunk)
        else:
            await asyncio.sleep(0.01)

    async def handshake(self):
        while True:
            try:
                self.tls.do_handshake()
                break
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                await self.exchange()
        if self.tls.cipher()[0] != "PSK-AES128-GCM-SHA256":
            raise RuntimeError("unexpected BLE TLS cipher")

    async def sample(self):
        while b"\n" not in self.buffer:
            try:
                chunk = self.tls.read(FRAME_LIMIT - len(self.buffer))
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                await self.exchange()
                continue
            if not chunk:
                raise EOFError("BLE TLS closed")
            self.buffer.extend(chunk)
            if len(self.buffer) == FRAME_LIMIT and b"\n" not in self.buffer:
                raise ValueError("invalid BLE frame length")
        end = self.buffer.index(b"\n") + 1
        imu = reading(bytes(self.buffer[:end]))
        del self.buffer[:end]
        return imu


async def observe(seconds, qr_image):
    from bleak import BleakClient, BleakScanner

    if not ssl.HAS_PSK:
        raise SystemExit("The BLE probe requires Python 3.13+ with OpenSSL PSK support")
    if qr_image:
        from PIL import Image
        import zxingcpp

        with Image.open(qr_image) as photo:
            code = zxingcpp.read_barcode(photo, formats=zxingcpp.BarcodeFormat.QRCode)
        pairing = code.text if code else ""
    else:
        if not sys.stdin.isatty():
            raise SystemExit("Enter the pairing payload in a terminal or use --qr-image")
        pairing = getpass.getpass("CoreS3 pairing payload (hidden): ").strip()
    if not re.fullmatch(r"(?:NXRS1:)?[0-9a-fA-F]{64}", pairing):
        raise SystemExit("Expected the local QR payload or its 64 hex digits")
    key = bytes.fromhex(pairing.removeprefix("NXRS1:"))
    device = await BleakScanner.find_device_by_filter(
        lambda _, adv: SERVICE in (uuid.lower() for uuid in adv.service_uuids),
        service_uuids=[SERVICE], timeout=10,
    )
    if device is None:
        raise SystemExit("Tracker service unavailable. Open Pair on the CoreS3.")
    try:
        async with BleakClient(device, services=[SERVICE]) as client:
            stream = TrackerStream(client, key)
            await asyncio.wait_for(stream.handshake(), 15)
            event("ble_connected")
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                imu = await asyncio.wait_for(stream.sample(), 5)
                if not client.is_connected:
                    raise EOFError("BLE disconnected")
                event("ble", imu=imu)
    except Exception as err:
        event("ble_error", reason=type(err).__name__)
        raise SystemExit(1) from None


def summarize(ble, uploads, loss_ms, restored_ms):
    if not 0 <= loss_ms < restored_ms:
        raise ValueError("Use IMU 1 timestamps from the USB console for loss and restoration")
    if sum(entry["event"] == "ble_connected" for entry in ble) != 1:
        raise ValueError("expected a single authenticated BLE session")
    if any(entry["event"] == "ble_error" for entry in ble):
        raise ValueError("BLE session stopped or stalled")
    samples = [entry for entry in ble if entry["event"] == "ble"]
    count = [0, 0, 0]
    max_gap = 0.0
    previous = None
    for entry in samples:
        if not all(entry["imu"]):
            raise ValueError("both IMUs must remain live over BLE")
        sample_ms = entry["imu"][0][1]
        if sample_ms <= loss_ms:
            count[0] += 1
        elif sample_ms <= restored_ms:
            count[1] += 1
        else:
            count[2] += 1
        if previous:
            for current, old in zip(entry["imu"], previous["imu"]):
                if current[0] <= old[0] or current[1] <= old[1]:
                    raise ValueError("BLE reading did not advance")
            max_gap = max(max_gap, entry["host_s"] - previous["host_s"])
        previous = entry
    if min(count) < 4 or max_gap > 2.5:
        raise ValueError(f"BLE readings by phase {count}, max host gap {max_gap:.3f}s")
    before = max((
        entry["session"] for entry in uploads
        if entry["received_ms"] is not None and entry["received_ms"] <= loss_ms
    ), default=0)
    last_ble_ms = samples[-1]["imu"][0][1]
    resumed = next((
        entry for entry in uploads
        if entry["received_ms"] is not None and restored_ms < entry["received_ms"] <= last_ble_ms
        and entry["session"] > before
    ), None)
    if not before or resumed is None:
        raise ValueError("expected a new TLS session with fresh telemetry while BLE is active")
    delay = (resumed["received_ms"] - restored_ms) / 1000
    return count, max_gap, resumed["session"], delay


def report(args):
    with args.ble.open() as log:
        ble = [json.loads(line) for line in log]
    with args.collector.open() as log:
        uploads = [entry for line in log if (entry := json.loads(line))["event"] == "upload"]
    try:
        count, max_gap, session, delay = summarize(ble, uploads, args.loss_ms, args.restored_ms)
    except ValueError as err:
        raise SystemExit(f"FAIL: {err}") from None
    print(f"PASS: BLE readings by phase {count}, max host gap {max_gap:.3f}s")
    print(f"PASS: fresh upload on TLS session {session}, source delay {delay:.3f}s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    collector = sub.add_parser("collector")
    collector.add_argument("--listen", default="127.0.0.1")
    collector.add_argument("--port", type=int, default=8443)
    ble = sub.add_parser("ble")
    ble.add_argument("--seconds", type=int, default=300)
    ble.add_argument("--qr-image", type=Path)
    summary = sub.add_parser("report")
    summary.add_argument("--ble", type=Path, required=True)
    summary.add_argument("--collector", type=Path, required=True)
    summary.add_argument("--loss-ms", type=int, required=True)
    summary.add_argument("--restored-ms", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "collector":
            asyncio.run(collect(args.listen, args.port))
        elif args.mode == "ble":
            asyncio.run(observe(args.seconds, args.qr_image))
        else:
            report(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
