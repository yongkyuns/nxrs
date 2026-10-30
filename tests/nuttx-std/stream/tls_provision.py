#!/usr/bin/env python3
"""USB-only TLS enrollment/time witness for a physical camera qualification."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import ssl
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "gateway/tests"))
from tls_reference import Credentials, private  # noqa: E402

MAX_LINE = 512
MAX_CSR = 1024
MAX_ENROLLMENT = 33792
CHUNK = 32
ACK_TIMEOUT = 10.0
INIT_TIMEOUT = 120.0
SITE = "qualification"
CAMERA = "camera"
GATEWAY = "gateway"
TLS_ERROR = r"(?:Unsupported|Busy|Entropy|Poisoned|InvalidInput|Capacity|Crypto|Authentication|Time|Io)"
_KNOWN_LINES = tuple(re.compile(pattern) for pattern in (
    r"NXRS_TLS_LISTEN ip=(?:\d{1,3}\.){3}\d{1,3} port=8443",
    rf"NXRS_TLS_STACK budget=\d+ used=\d+",
    rf"NXRS_TLS_REPORT ms=\d+ captured=\d+ delivered=\d+ handshakes=\d+ failures=\d+ last=(?:None|Some\({TLS_ERROR}\)) heap_user=\d+ heap_kernel=\d+",
    r"NXRS_TLS_DELIVERED captured=\d+ delivered=\d+ handshakes=\d+ failures=\d+",
    r"NXRS_TLS_CAMERA_PASS stopped=1 joined=1",
    r"NXRS_TLS_SESSION_PASS stopped=1 joined=1",
    r"NXRS_STREAM_STACK budget=\d+ used=\d+",
    r"NXRS_STREAM_REPORT ms=\d+ captured=\d+ delivered=\d+ bytes=\d+ max_bytes=\d+ heap_user=\d+ heap_kernel=\d+ cpu_busy_pm=\d+ owner_cpu_ms=\d+ online=[01] attempts=\d+ rejoined=\d+ errors=\d+(?: camera_discarded=\d+)?",
    r"NXRS_STREAM_ONLINE ip=(?:\d{1,3}\.){3}\d{1,3} port=(?:8080|8443) ms=\d+",
    r"NXRS_DETECTION_REPORT ms=\d+ state=(?:Unavailable|Clear|Person) submitted=\d+ completed=\d+ failures=\d+ inference_us=\d+ max_inference_us=\d+ events=\d+ sequence=\d+ age_ms=\d+ score=\d+",
    r"NXRS_STREAM_PASS stopped=1 joined=1",
))


def _read_exact(port, size, deadline, stopped):
    data = bytearray()
    while len(data) < size:
        if stopped.is_set():
            raise RuntimeError("TLS qualification stopped")
        if time.monotonic() >= deadline:
            raise TimeoutError("device binary response deadline")
        part = port.read(size - len(data))
        if part:
            data.extend(part)
    return bytes(data)


def _line(port, deadline, pending):
    while time.monotonic() < deadline:
        char = port.read(1)
        if not char:
            time.sleep(0.01)
            continue
        if char == b"\n":
            line = bytes(pending).rstrip(b"\r")
            pending.clear()
            if len(line) > MAX_LINE:
                raise ValueError("device line exceeds bound")
            try:
                return line.decode("ascii")
            except UnicodeDecodeError:
                raise ValueError("device line is not ASCII") from None
        pending.extend(char)
        if len(pending) > MAX_LINE:
            raise ValueError("device line exceeds bound")
    raise TimeoutError("device line deadline")


def _field(value):
    encoded = value.encode("ascii")
    if not encoded or len(encoded) > 0xffff:
        raise ValueError("enrollment field length")
    return struct.pack(">H", len(encoded)) + encoded


def _certificate_der(path):
    return ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))


class Supervisor:
    """Own serial protocol processing on a daemon reader thread.

    The caller owns the serial object and private empty directory for the full
    lifetime. Only bounded, recognized NXRS_TLS lines are retained in `events`.
    """

    def __init__(self, port, root, camera_ip, *, site=SITE, camera=CAMERA, gateway=GATEWAY, observer=None):
        self.port = port
        self.root = Path(root).resolve()
        self.address = str(ipaddress.IPv4Address(camera_ip))
        self.site, self.camera, self.gateway = site, camera, gateway
        if not self.root.is_dir() or any(self.root.iterdir()):
            raise ValueError("TLS qualification directory must be an existing empty directory")
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.failure = None
        self.credentials = None
        self.events = []
        self.observer = observer
        self.pending = bytearray()
        self._stop = threading.Event()
        self._device_stop = threading.Event()
        self._renew = False
        self._write_lock = threading.Lock()
        self._deadline = time.monotonic() + INIT_TIMEOUT
        self._thread = threading.Thread(target=self._run, name="nxrs-tls-usb", daemon=True)
        self._thread.start()

    def _write(self, data):
        with self._write_lock:
            self.port.write(data)
            self.port.flush()

    def _wait_line(self, prefix, deadline):
        while not self._stop.is_set():
            try:
                line = _line(self.port, min(deadline, time.monotonic() + 0.5), self.pending)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise
                continue
            self._record(line)
            if line.startswith(prefix):
                return line
            if line.startswith(("NXRS_TLS_CAMERA_FAIL", "NXRS_STREAM_FAIL")):
                raise RuntimeError("device TLS qualification failed")
        raise RuntimeError("TLS qualification stopped")

    def _record(self, line):
        if line.startswith(("NXRS_TLS_CAMERA_FAIL", "NXRS_STREAM_FAIL")):
            line = line.split()[0]
        elif not any(pattern.fullmatch(line) for pattern in _KNOWN_LINES):
            return
        if len(self.events) == 256:
            del self.events[0]
        self.events.append(line)
        if self.observer is not None:
            self.observer(line)

    def _enroll(self, csr):
        marker = self._wait_line("NXRS_TLS_ENROLL_READY", self._deadline)
        match = re.fullmatch(r"NXRS_TLS_ENROLL_READY max=(\d+)", marker)
        if not match or int(match[1]) != MAX_ENROLLMENT:
            raise ValueError("device enrollment bound mismatch")
        creds = Credentials(self.root, server_csr=self.root / "camera.csr", server_ip=self.address,
                            site=self.site, camera=self.camera, gateway=self.gateway)
        der_crl = self.root / "crl.der"
        creds.call("crl", "-in", self.root / "crl.pem", "-outform", "DER", "-out", der_crl)
        for path in self.root.iterdir():
            if path.is_file():
                path.chmod(0o600)
        issuer = _certificate_der(self.root / "ca.pem")
        own_uri = f"urn:nxrs:camera:{self.site}:{self.camera}"
        peer_uri = f"urn:nxrs:ingest:{self.site}:{self.gateway}:{self.camera}"
        parts = [own_uri, peer_uri,
                 _certificate_der(self.root / "server.pem"), issuer,
                 _certificate_der(self.root / "client.pem"), der_crl.read_bytes()]
        blob = b"NXTE" + hashlib.sha256(issuer).digest() + b"".join(
            _field(v) if isinstance(v, str) else struct.pack(">H", len(v)) + v for v in parts)
        if len(blob) > MAX_ENROLLMENT:
            raise ValueError("enrollment exceeds device bound")
        self._write(struct.pack(">I", len(blob)))
        ack = self._wait_line("NXRS_TLS_INPUT_ACK", min(self._deadline, time.monotonic() + ACK_TIMEOUT))
        if ack != "NXRS_TLS_INPUT_ACK offset=0":
            raise ValueError("device enrollment acknowledgement mismatch")
        for offset in range(0, len(blob), CHUNK):
            chunk = blob[offset:offset + CHUNK]
            self._write(chunk)
            expected = offset + len(chunk)
            ack = self._wait_line("NXRS_TLS_INPUT_ACK", min(self._deadline, time.monotonic() + ACK_TIMEOUT))
            if ack != f"NXRS_TLS_INPUT_ACK offset={expected}":
                raise ValueError("device enrollment acknowledgement mismatch")
        self.credentials = creds

    def _run(self):
        try:
            line = self._wait_line("NXRS_TLS_CSR", self._deadline)
            match = re.fullmatch(r"NXRS_TLS_CSR bytes=(\d+)", line)
            if not match or not 1 <= int(match[1]) <= MAX_CSR:
                raise ValueError("device CSR size invalid")
            pem = _read_exact(self.port, int(match[1]), self._deadline, self._stop)
            private(self.root / "camera.csr", pem)
            self._enroll(pem)
            while not self._stop.is_set():
                try:
                    line = _line(self.port, time.monotonic() + 0.5, self.pending)
                except TimeoutError:
                    if self._stop.is_set():
                        break
                    continue
                time_match = re.fullmatch(r"NXRS_TLS_TIME_REQUEST sequence=(\d+)", line)
                if time_match:
                    seq = int(time_match[1])
                    if seq > 0xffffffffffffffff:
                        raise ValueError("device time sequence invalid")
                    if self._device_stop.is_set():
                        self._write((b"NXR1" if self._renew else b"NXS1") + struct.pack(">QQ", seq, 0))
                    else:
                        self._write(b"NXT1" + struct.pack(">QQ", seq, int(time.time())))
                    continue
                listen = re.fullmatch(r"NXRS_(?:TLS_LISTEN|STREAM_ONLINE) ip=(\d{1,3}(?:\.\d{1,3}){3}) port=(\d+)(?: ms=\d+)?", line)
                if listen:
                    address = str(ipaddress.IPv4Address(listen[1]))
                    if address != self.address or int(listen[2]) != 8443:
                        raise ValueError("device TLS listener endpoint mismatch")
                    self.address = address
                    self.ready.set()
                    continue
                if line in ("NXRS_TLS_CAMERA_PASS stopped=1 joined=1", "NXRS_TLS_SESSION_PASS stopped=1 joined=1", "NXRS_STREAM_PASS stopped=1 joined=1"):
                    self._record(line)
                    return
                if line.startswith(("NXRS_TLS_CAMERA_FAIL", "NXRS_STREAM_FAIL")):
                    self._record(line)
                    raise RuntimeError("device TLS qualification failed")
                self._record(line)
        except Exception as error:
            if self._stop.is_set():
                return
            self.failure = str(error) if isinstance(error, (ValueError, RuntimeError, TimeoutError)) else "TLS supervisor error"
            self.ready.set()
        finally:
            self.finished.set()

    def request_stop(self, *, renew=False):
        """Answer the next device time request with a sequence-bound stop.

        The caller can open a TCP connection to wake an idle TLS accept loop.
        The device owner, not this serial reader, joins camera and Wi-Fi.
        """
        self._renew = renew
        self._device_stop.set()

    def shutdown(self, *, renew=False, timeout=45):
        """Hold a TCP wake peer until the USB owner confirms retirement.

        Closing before accept can abort the listener on NuttX. A busy rejection
        retries within the same deadline; only the USB reader consumes replies.
        """
        self.request_stop(renew=renew)
        deadline = time.monotonic() + timeout
        while not self.finished.is_set() and time.monotonic() < deadline:
            try:
                with socket.create_connection((self.address, 8443), timeout=2) as wake:
                    wake.settimeout(.5)
                    while not self.finished.is_set() and time.monotonic() < deadline:
                        try:
                            if not wake.recv(1):
                                break
                        except socket.timeout:
                            pass
            except OSError:
                pass
            self.finished.wait(.05)
        if not self.finished.is_set():
            raise RuntimeError("TLS camera shutdown deadline")

    def stop(self):
        self._stop.set()
        cancel = getattr(self.port, "cancel_read", None)
        if cancel is not None:
            cancel()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            self.failure = self.failure or "TLS serial reader did not stop"


class _IdleSerial:
    def read(self, _size):
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        pass


class _ChunkedSerial(_IdleSerial):
    def __init__(self, data):
        self.data = bytearray(data)
        self.drained = threading.Event()
        self.writes = []
        self.written = threading.Event()

    def write(self, data):
        self.writes.append(data)
        self.written.set()
        return len(data)

    def read(self, _size):
        if not self.data:
            self.drained.set()
            time.sleep(0.005)
            return b""
        value = self.data[0]
        del self.data[0]
        return bytes((value,))


class SupervisorTests(unittest.TestCase):
    def test_control_keeps_wake_peer_until_sequence_bound_join(self):
        for renew in (False, True):
            port = _ChunkedSerial(b"NXRS_TLS_CSR bytes=1\nxNXRS_TLS_TIME_REQUEST sequence=3\n")
            with self.subTest(renew=renew), tempfile.TemporaryDirectory() as directory, \
                    patch.object(Supervisor, '_enroll', lambda *_: None), \
                    patch.object(time, 'time', return_value=1_800_000_000):
                supervisor = Supervisor(port, directory, "192.0.2.8")
                owner = self
                marker = 'NXRS_TLS_SESSION_PASS' if renew else 'NXRS_TLS_CAMERA_PASS'
                class WakePeer:
                    closed = False
                    def __enter__(self): return self
                    def __exit__(self, *_): self.closed = True
                    def settimeout(self, _): pass
                    def recv(self, _):
                        port.data.extend(f"NXRS_TLS_TIME_REQUEST sequence=4\n{marker} stopped=1 joined=1\n".encode())
                        owner.assertTrue(supervisor.finished.wait(1))
                        owner.assertFalse(self.closed)
                        return b''
                wake = WakePeer()
                try:
                    self.assertTrue(port.written.wait(1))
                    self.assertEqual(port.writes, [b'NXT1' + struct.pack('>QQ', 3, 1_800_000_000)])
                    self.assertFalse(supervisor.finished.is_set())
                    with patch.object(socket, 'create_connection', return_value=wake):
                        supervisor.shutdown(renew=renew, timeout=2)
                    self.assertTrue(wake.closed)
                    self.assertEqual(port.writes[-1], (b'NXR1' if renew else b'NXS1') + struct.pack('>QQ', 4, 0))
                    self.assertEqual(supervisor.events[-1], marker + ' stopped=1 joined=1')
                    self.assertIsNone(supervisor.failure)
                finally:
                    supervisor.stop()

    def test_address_is_kept_and_idle_reader_stops(self):
        for port in (_IdleSerial(), _ChunkedSerial(b"NXRS_TLS_CSR bytes=100\n")):
            with tempfile.TemporaryDirectory() as directory:
                supervisor = Supervisor(port, directory, "192.0.2.8")
                if isinstance(port, _ChunkedSerial):
                    self.assertTrue(port.drained.wait(1))  # Partial CSR input entered.
                self.assertEqual(supervisor.address, "192.0.2.8")
                supervisor.stop()
                self.assertTrue(supervisor.finished.is_set())
                self.assertIsNone(supervisor.failure)

    def test_partial_line_survives_idle_deadline(self):
        pending = bytearray()
        serial = _ChunkedSerial(b"NXRS_TLS_STACK budget=12 ")
        with self.assertRaises(TimeoutError):
            _line(serial, time.monotonic() + 0.03, pending)
        self.assertEqual(pending, b"NXRS_TLS_STACK budget=12 ")
        serial.data.extend(b"used=10\r\n")
        self.assertEqual(_line(serial, time.monotonic() + 0.5, pending), "NXRS_TLS_STACK budget=12 used=10")
        self.assertEqual(pending, b"")

    def test_only_bounded_numeric_witness_lines_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = Supervisor(_IdleSerial(), directory, "192.0.2.8")
            supervisor.stop()
            for line in (
                "NXRS_TLS_STACK budget=65536 used=4096",
                "NXRS_TLS_REPORT ms=10000 captured=100 delivered=99 handshakes=1 failures=0 last=None heap_user=1 heap_kernel=2",
                "NXRS_TLS_DELIVERED captured=100 delivered=99 handshakes=1 failures=0",
                "NXRS_TLS_INPUT_ACK offset=32",
                "NXRS_TLS_STACK budget=65536 used=untrusted",
                "NXRS_TLS_CAMERA_PASS stopped=1 joined=1",
            ):
                supervisor._record(line)
            self.assertEqual(supervisor.events, [
                "NXRS_TLS_STACK budget=65536 used=4096",
                "NXRS_TLS_REPORT ms=10000 captured=100 delivered=99 handshakes=1 failures=0 last=None heap_user=1 heap_kernel=2",
                "NXRS_TLS_DELIVERED captured=100 delivered=99 handshakes=1 failures=0",
                "NXRS_TLS_CAMERA_PASS stopped=1 joined=1",
            ])

            for _ in range(300):
                supervisor._record("NXRS_TLS_STACK budget=65536 used=4096")
            supervisor._record("NXRS_TLS_CAMERA_PASS stopped=1 joined=1")
            self.assertEqual(len(supervisor.events), 256)
            self.assertEqual(supervisor.events[-1], "NXRS_TLS_CAMERA_PASS stopped=1 joined=1")


if __name__ == "__main__":
    unittest.main()
