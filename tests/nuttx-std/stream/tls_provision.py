#!/usr/bin/env python3
"""USB-only TLS enrollment/time witness for a physical camera qualification."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import ssl
import struct
import sys
import tempfile
import threading
import time
import unittest
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


def _read_exact(port, size, deadline):
    data = bytearray()
    while len(data) < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("device binary response deadline")
        part = port.read(size - len(data))
        if part:
            data.extend(part)
    return bytes(data)


def _line(port, deadline):
    data = bytearray()
    while time.monotonic() < deadline:
        char = port.read(1)
        if not char:
            time.sleep(0.01)
            continue
        if char == b"\n":
            line = bytes(data).rstrip(b"\r")
            if len(line) > MAX_LINE:
                raise ValueError("device line exceeds bound")
            try:
                return line.decode("ascii")
            except UnicodeDecodeError:
                raise ValueError("device line is not ASCII") from None
        data.extend(char)
        if len(data) > MAX_LINE:
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

    def __init__(self, port, root, camera_ip, *, site=SITE, camera=CAMERA, gateway=GATEWAY):
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
        self._stop = threading.Event()
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
                line = _line(self.port, min(deadline, time.monotonic() + 0.5))
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise
                continue
            if line.startswith("NXRS_TLS_"):
                if len(self.events) < 256 and re.fullmatch(r"NXRS_TLS_[A-Z_]+(?: [A-Za-z0-9_.= -]{0,300})?", line):
                    self.events.append(line)
            if line.startswith(prefix):
                return line
            if line.startswith("NXRS_TLS_CAMERA_FAIL"):
                raise RuntimeError("device TLS qualification failed")
        raise RuntimeError("TLS qualification stopped")

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
            pem = _read_exact(self.port, int(match[1]), self._deadline)
            private(self.root / "camera.csr", pem)
            self._enroll(pem)
            while not self._stop.is_set():
                try:
                    line = _line(self.port, time.monotonic() + 1)
                except TimeoutError:
                    if self._stop.is_set():
                        break
                    continue
                time_match = re.fullmatch(r"NXRS_TLS_TIME_REQUEST sequence=(\d+)", line)
                if time_match:
                    seq = int(time_match[1])
                    if seq > 0xffffffffffffffff:
                        raise ValueError("device time sequence invalid")
                    self._write(b"NXT1" + struct.pack(">QQ", seq, int(time.time())))
                    continue
                listen = re.fullmatch(r"NXRS_TLS_LISTEN ip=(\d{1,3}(?:\.\d{1,3}){3}) port=(\d+)", line)
                if listen:
                    address = str(ipaddress.IPv4Address(listen[1]))
                    if address != self.address or int(listen[2]) != 8443:
                        raise ValueError("device TLS listener endpoint mismatch")
                    self.address = address
                    self.ready.set()
                    continue
                if line.startswith("NXRS_TLS_CAMERA_PASS"):
                    self.events.append("NXRS_TLS_CAMERA_PASS")
                    return
                if line.startswith("NXRS_TLS_CAMERA_FAIL"):
                    self.events.append("NXRS_TLS_CAMERA_FAIL")
                    raise RuntimeError("device TLS qualification failed")
                if line.startswith("NXRS_TLS_") and len(self.events) < 256:
                    self.events.append("NXRS_TLS_OTHER")
        except Exception as error:
            if self._stop.is_set():
                return
            self.failure = str(error) if isinstance(error, (ValueError, RuntimeError, TimeoutError)) else "TLS supervisor error"
            self.ready.set()
        finally:
            self.finished.set()

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


class SupervisorTests(unittest.TestCase):
    def test_address_is_kept_and_idle_reader_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = Supervisor(_IdleSerial(), directory, "192.0.2.8")
            self.assertEqual(supervisor.address, "192.0.2.8")
            supervisor.stop()
            self.assertTrue(supervisor.finished.is_set())
            self.assertIsNone(supervisor.failure)

    def test_enrollment_field_length_prefix(self):
        self.assertEqual(_field("urn:nxrs:camera:x:y"), b"\x00\x13urn:nxrs:camera:x:y")


if __name__ == "__main__":
    unittest.main()
