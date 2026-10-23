#!/usr/bin/env python3
"""Host protocol for an already-flashed IDF reference. Never resets or flashes."""
import contextlib
import importlib.util
from pathlib import Path
import re
import socket
import threading
import time

SPEC = importlib.util.spec_from_file_location(
    "csi_radio_host", Path(__file__).resolve().parents[1] / "run.py"
)
RADIO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RADIO)
RESULT = re.compile(
    rb"IDF60_RESULT elapsed_ms=(\d+) frames=(\d+) factors=(\d+) sent=(\d+) echoes=(\d+) raw_exported=(\d+) force=0 transport_only=([01])"
)
OBSERVATION = re.compile(
    rb"IDF60_CALLBACKS observed=(\d+) foreign=(\d+) legacy=(\d+) ht=(\d+) other=(\d+) dropped=0"
)
PASS = b"IDF60_PASS capture_cleanup=confirmed tcp_eof=1 station_disconnected=1 sdk_deinitialized=1"


def credential_frame(ssid, password):
    secrets = tuple(
        value.encode() if isinstance(value, str) else bytes(value)
        for value in (ssid, password)
    )
    if (
        not 1 <= len(secrets[0]) <= 32
        or len(secrets[1]) > 63
        or 0 < len(secrets[1]) < 8
    ):
        raise ValueError("invalid credential lengths")
    return bytes(map(len, secrets)) + b"".join(secrets), secrets


def validate(lines, served, seconds, transport, raw_records):
    results = [
        RESULT.fullmatch(line) for line in lines if line.startswith(b"IDF60_RESULT ")
    ]
    if len(results) != 1 or results[0] is None or lines.count(PASS) != 1:
        raise ValueError("reference result/cleanup evidence missing or ambiguous")
    values = list(map(int, results[0].groups()))
    elapsed, frames, factors, sent, echoes, exported, mode = values
    if mode != int(transport) or not seconds * 1000 <= elapsed <= seconds * 1000 + 2000:
        raise ValueError("reference mode or observation duration mismatch")
    if sent != echoes or echoes < 10 or served != set(range(echoes)):
        raise ValueError("reference echo loss or mismatch")
    observations = [
        OBSERVATION.fullmatch(line)
        for line in lines
        if line.startswith(b"IDF60_CALLBACKS ")
    ]
    if transport:
        if frames or factors or exported or raw_records or observations:
            raise ValueError("transport control performed capture")
    else:
        if (
            frames < 50
            or factors != frames - 49
            or not 1 <= exported <= seconds * 10 + 1
            or exported != raw_records
        ):
            raise ValueError("incomplete reference capture")
        if len(observations) != 1 or observations[0] is None:
            raise ValueError("reference callback evidence missing or ambiguous")
        observed, foreign, legacy, ht, other = map(int, observations[0].groups())
        if (
            foreign > observed
            or observed - foreign < frames
            or legacy + ht + other != observed
        ):
            raise ValueError("inconsistent reference callback evidence")
    report = dict(
        zip(
            ("elapsed_ms", "frames", "factors", "sent", "echoes", "raw_exported"),
            values[:6],
        )
    )
    report.update(
        requested_seconds=seconds,
        transport_only=transport,
        traffic_protocol="tcp",
        gain_forcing=False,
        presence_qualified=False,
        private_gain_metadata_qualified=False,
        tcp_peer_eof=True,
        capture_cleanup_confirmed=True,
        station_disconnected=True,
        sdk_deinitialized=True,
    )
    if not transport:
        report["callbacks"] = dict(
            zip(
                ("observed", "foreign", "legacy", "ht", "other"),
                map(int, observations[0].groups()),
            )
        )
        report["callbacks"]["dropped"] = 0
    return report


class Client:
    def __init__(self, console):
        self.console = console
        self.buffer = bytearray()

    def line(self, deadline, secrets):
        while time.monotonic() < deadline:
            if any(secret and secret in self.buffer for secret in secrets):
                raise RuntimeError("credential echo detected; console withheld")
            if b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer[:] = rest
                if any(
                    x in line
                    for x in (
                        b"PANIC",
                        b"Guru Meditation",
                        b"abort()",
                        b"assert failed",
                        b"IDF60_FAIL",
                    )
                ):
                    raise RuntimeError("reference firmware failed; console withheld")
                return bytes(line).strip()
            self.buffer.extend(self.console.read(1024))
            if len(self.buffer) > 8192:
                raise RuntimeError("reference console exceeded line bound")
        raise TimeoutError("reference console deadline")

    def qualify(self, host_ip, ssid, password, seconds, capture=None):
        transport = capture is None
        frame, secrets = credential_frame(ssid, password)
        deadline = time.monotonic() + 50
        while self.line(deadline, secrets) != b"IDF60_COMMAND_READY force=0":
            pass
        stopped, served, invalid = threading.Event(), set(), []
        raw_records = 0
        lines = []
        peer = None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host_ip, 0))
            listener.listen(1)
            listener.settimeout(0.2)
            port = listener.getsockname()[1]

            def serve():
                try:
                    if RADIO.serve_tcp_echo(
                        listener, stopped, served, seconds * 10 + 20
                    ):
                        invalid.append(True)
                except Exception:
                    invalid.append(True)

            worker = threading.Thread(target=serve, name="idf60-reference-echo")
            worker.start()
            try:
                mode = "transport" if transport else "capture"
                RADIO.write_console(
                    self.console, f"IDF60 {seconds} {host_ip}:{port} {mode}\n".encode()
                )
                expected = f"IDF60_OPTIONS seconds={seconds} transport_only={int(transport)} tcp=1 raw={int(not transport)} force=0 peer_port={port}".encode()
                matched = False
                while True:
                    line = self.line(deadline, secrets)
                    if line.startswith(b"IDF60_OPTIONS "):
                        if matched or line != expected:
                            raise RuntimeError(
                                "reference options mismatch; credentials withheld"
                            )
                        matched = True
                    if line == b"IDF60_CREDENTIALS_READY":
                        if not matched:
                            raise RuntimeError(
                                "reference did not acknowledge options; credentials withheld"
                            )
                        break
                RADIO.write_console(self.console, frame)
                deadline = time.monotonic() + seconds + 45
                handle = (
                    RADIO.private_capture(capture)
                    if capture
                    else contextlib.nullcontext(None)
                )
                with handle as output:
                    while True:
                        line = self.line(deadline, secrets)
                        if RADIO.RAW.fullmatch(line):
                            if output is None or raw_records >= seconds * 10 + 1:
                                raise RuntimeError(
                                    "unexpected or excessive reference RAW data"
                                )
                            output.write(line + b"\n")
                            raw_records += 1
                        elif line.startswith(b"IDF60_PEER "):
                            match = re.fullmatch(
                                rb"IDF60_PEER bssid=([0-9a-f]{12}) channel=(\d{1,2})",
                                line,
                            )
                            if peer is not None or match is None:
                                raise RuntimeError(
                                    "invalid or ambiguous reference peer"
                                )
                            address = bytes.fromhex(match[1].decode())
                            if (
                                address == b"\x00" * 6
                                or address[0] & 1
                                or not 1 <= int(match[2]) <= 14
                            ):
                                raise RuntimeError("invalid reference peer")
                            peer = dict(bssid=match[1].decode(), channel=int(match[2]))
                        elif line.startswith(
                            (b"IDF60_RESULT ", b"IDF60_CALLBACKS ", b"IDF60_PASS ")
                        ):
                            lines.append(line)
                            if len(lines) > 3:
                                raise RuntimeError("duplicate reference evidence")
                        if line == PASS:
                            break
            finally:
                stopped.set()
                worker.join(timeout=2)
        if worker.is_alive() or invalid:
            raise RuntimeError("reference echo worker failed or did not stop")
        report = validate(lines, served, seconds, transport, raw_records)
        if peer is None:
            raise RuntimeError("reference peer evidence missing")
        report["private_peer"] = peer
        report["host_echo_worker_joined"] = True
        return report
