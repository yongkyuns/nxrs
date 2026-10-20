#!/usr/bin/env python3
"""Bounded host-side qualification for the physical NuttX MJPEG stream."""
import argparse
from collections import Counter
from contextlib import contextmanager, nullcontext
import importlib.util
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
WIFI_RUN = ROOT / "tests/nuttx-std/wifi/run.py"
BOUNDARY = b"--nxrs-frame"
MAX_FRAME = 320 * 240 * 2
MAX_HEADERS = 8192
MAX_LINE = 1024
# Allow the peer's 1.5s and 3s SYN-ACK retries without repeatedly replacing a
# half-open connection. The separate 10s delivery-continuity limit is unchanged.
IO_TIMEOUT = 5.0
COMPLETION_GRACE_SECONDS = 70
SERIAL_LIMIT = 8192
REPORT_LIMIT = 400
FAIL_MARKERS = (b"NXRS_STREAM_FAIL", b"PANIC", b"Assertion failed")
REPORT_RE = re.compile(
    rb"^NXRS_STREAM_REPORT ms=(\d+) captured=(\d+) delivered=(\d+) bytes=(\d+) "
    rb"max_bytes=(\d+) heap_user=(\d+) heap_kernel=(\d+) cpu_busy_pm=(\d+) "
    rb"owner_cpu_ms=(\d+) online=([01]) attempts=(\d+) rejoined=(\d+) errors=(\d+)$"
)
ONLINE_RE = re.compile(
    rb"^NXRS_STREAM_ONLINE ip=(\d{1,3}(?:\.\d{1,3}){3}) port=(\d+) ms=(\d+)\r?$", re.M
)
OFFLINE_RE = re.compile(rb"^NXRS_STREAM_OFFLINE ms=(\d+)$")
PASS_RE = re.compile(rb"^NXRS_STREAM_PASS stopped=1 joined=1$")
ALLOWED_LINE_RE = re.compile(
    rb"^(?:NXRS_STREAM_(?:ONLINE|OFFLINE|REPORT|PASS|FAIL|RECONNECT_REQUEST)\b|"
    rb"NXRS_WIFI_CREDENTIALS_READY\b|nsh> )"
)


def _load_helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WIFI = _load_helper("nxrs_wifi_run", WIFI_RUN)


def wait_stage(console, pending, marker, password, timeout, stage):
    try:
        return WIFI.read_until(console, pending, marker, password, timeout)
    except RuntimeError as error:
        # Keep diagnostic context without ever returning raw serial content.
        detail = ""
        if not password or password not in pending:
            failure = re.search(rb'(?m)^NXRS_STREAM_FAIL error=([ -~]{1,240})\r?$', pending)
            if failure:
                detail = "; device failure: " + failure[1].decode('ascii')
            else:
                reports = [REPORT_RE.fullmatch(line.rstrip(b'\r')) for line in pending.split(b'\n')]
                valid = [report for report in reports if report is not None]
                if valid:
                    last = valid[-1]
                    detail = (f"; last report: ms={int(last[1])}, captured={int(last[2])}, "
                              f"online={int(last[10])}, attempts={int(last[11])}")
                if any(marker in pending for marker in FAIL_MARKERS[1:]):
                    detail += "; device panic detected (raw output withheld)"
                stages = re.findall(rb'NXRS_STREAM_STAGE phase=([a-z_]{1,32})', pending)
                if stages:
                    detail += "; last stage: " + stages[-1].decode('ascii')
        raise RuntimeError(f"{stage}: {error}{detail}") from None


def _outside_repo(path):
    resolved = Path(path).resolve(strict=False)
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("keep private inputs and outputs outside the repository")
    return resolved


def read_wifi_file(path):
    """Read and validate the private two-line SSID/password file without logging it."""
    target = Path(path).absolute()
    _outside_repo(target)
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise ValueError("Wi-Fi file must be owned by you, regular, and mode 0600 or stricter")
        raw = os.read(fd, 257)
    finally:
        os.close(fd)
    if len(raw) > 256:
        raise ValueError("Wi-Fi file exceeds 256 bytes")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("Wi-Fi file must contain printable ASCII") from error
    lines = text.splitlines()
    if len(lines) != 2 or any(not line for line in lines):
        raise ValueError("Wi-Fi file must contain exactly two non-empty lines")
    entries = {}
    for line in lines:
        match = re.fullmatch(r"([^=:]+)([=:])(.*)", line)
        if match is None:
            raise ValueError("Wi-Fi file must have exactly SSID and password keys")
        key, value = match[1].strip().lower(), match[3]
        # In the human-readable `key: value` form, one space belongs to the
        # delimiter. The `key=value` form preserves literal leading spaces.
        if match[2] == ':' and value.startswith(' '):
            value = value[1:]
        if key in entries or key not in ("ssid", "password"):
            raise ValueError("Wi-Fi file must have exactly SSID and password keys")
        entries[key] = value
    if set(entries) != {"ssid", "password"}:
        raise ValueError("Wi-Fi file must have exactly SSID and password keys")
    ssid, password = entries["ssid"].encode("ascii"), entries["password"].encode("ascii")
    if not 1 <= len(ssid) <= 32:
        raise ValueError("SSID must be 1–32 ASCII bytes")
    if len(password) not in (0,) and not 8 <= len(password) <= 63:
        raise ValueError("password must be empty or 8–63 printable ASCII bytes")
    if any(byte < 32 or byte > 126 for byte in password):
        raise ValueError("password must be empty or 8–63 printable ASCII bytes")
    return ssid, password


def _readline(stream, limit=MAX_LINE):
    line = bytearray()
    while len(line) <= limit:
        byte = stream.read(1)
        if not byte:
            raise EOFError("HTTP connection ended inside a line")
        line.extend(byte)
        if line.endswith(b"\r\n"):
            return bytes(line[:-2])
    raise ValueError("HTTP line exceeded its bound")


def _read_exact(stream, length):
    if length < 0 or length > MAX_FRAME:
        raise ValueError("frame length outside supported bound")
    body = bytearray()
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise EOFError("HTTP connection ended inside a frame")
        body.extend(chunk)
    return bytes(body)


def read_http_headers(stream, expected_status=b"200"):
    status = _readline(stream)
    if not status.startswith(b"HTTP/1.") or b" " + expected_status + b" " not in status:
        raise ValueError("unexpected HTTP status")
    headers = {}
    total = len(status) + 2
    while True:
        line = _readline(stream)
        total += len(line) + 2
        if total > MAX_HEADERS:
            raise ValueError("HTTP headers exceeded their bound")
        if not line:
            return headers
        if line[:1] in (b" ", b"\t") or b":" not in line:
            raise ValueError("malformed HTTP header")
        name, value = line.split(b":", 1)
        key = name.strip().lower()
        if not key or key in headers:
            raise ValueError("duplicate or empty HTTP header")
        headers[key] = value.strip()


def clock_sample(host, port, clock_fn=time.monotonic, stop_event=None, source_ip=None, samples=5,
                 socket_scope=nullcontext):
    """Return the best sampled device-minus-host offset and its RTT bound."""
    measured = []
    for _ in range(samples):
        if stop_event is not None and stop_event.is_set():
            break
        try:
            before = clock_fn()
            with socket.create_connection((host, port), timeout=IO_TIMEOUT,
                                          source_address=(source_ip, 0) if source_ip else None) as conn:
                with socket_scope(conn):
                    # A connect already in flight may finish after cancellation.
                    if stop_event is not None and stop_event.is_set():
                        break
                    conn.settimeout(IO_TIMEOUT)
                    conn.sendall(b"GET /clock HTTP/1.1\r\nHost: nxrs\r\nConnection: close\r\n\r\n")
                    with conn.makefile("rb", buffering=0) as stream:
                        headers = read_http_headers(stream)
            after = clock_fn()
            device_ms = int(headers[b"x-clock-ms"])
        except (KeyError, ValueError) as error:
            raise ValueError("clock response omitted X-Clock-Ms") from error
        except (OSError, EOFError):
            continue
        rtt_ms = max(0.0, (after - before) * 1000)
        midpoint_ms = ((before + after) / 2) * 1000
        measured.append((rtt_ms, device_ms - midpoint_ms))
    if not measured:
        raise TimeoutError("device clock samples failed")
    rtt, offset = min(measured, key=lambda item: item[0])
    return offset, rtt / 2


def read_mjpeg_response(stream):
    headers = read_http_headers(stream)
    content_type = headers.get(b"content-type", b"").lower()
    if content_type != b"multipart/x-mixed-replace; boundary=nxrs-frame":
        raise ValueError("unexpected MJPEG content type or boundary")
    return headers


def read_mjpeg_frame(stream):
    if _readline(stream) != BOUNDARY:
        raise ValueError("invalid multipart boundary")
    headers = {}
    total = 0
    while True:
        line = _readline(stream)
        total += len(line) + 2
        if total > MAX_HEADERS:
            raise ValueError("multipart headers exceeded their bound")
        if not line:
            break
        if b":" not in line or line[:1] in (b" ", b"\t"):
            raise ValueError("malformed multipart header")
        name, value = line.split(b":", 1)
        key = name.strip().lower()
        if key in headers or not key:
            raise ValueError("duplicate or empty multipart header")
        headers[key] = value.strip()
    try:
        raw_length = headers[b"content-length"]
        raw_sequence = headers[b"x-sequence"]
        raw_capture = headers[b"x-capture-ms"]
        if any(re.fullmatch(rb"[0-9]+", value) is None
               for value in (raw_length, raw_sequence, raw_capture)):
            raise ValueError("non-decimal multipart metadata")
        length, sequence, capture_ms = int(raw_length), int(raw_sequence), int(raw_capture)
    except (KeyError, ValueError) as error:
        raise ValueError("multipart frame omitted a valid required header") from error
    if not 4 <= length <= MAX_FRAME or sequence > 0xffffffffffffffff or capture_ms > 0xffffffffffffffff:
        raise ValueError("invalid multipart frame metadata")
    body = _read_exact(stream, length)
    if _read_exact(stream, 2) != b"\r\n":
        raise ValueError("missing multipart frame terminator")
    if headers.get(b"content-type", b"").lower() != b"image/jpeg":
        raise ValueError("multipart frame is not declared as JPEG")
    return sequence, capture_ms, body


def decode_jpeg(image, body, expected_size=(320, 240)):
    """Fully decode one JPEG only after checking its negotiated dimensions."""
    try:
        with image.open(io.BytesIO(body)) as decoded:
            if decoded.format != "JPEG" or decoded.size != expected_size:
                raise ValueError("decoded frame does not match the requested JPEG dimensions")
            decoded.load()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("JPEG frame failed full image decoding") from error


def probe_slow_reader(host, port, source_ip, pause_seconds=10, drain_timeout=10):
    """Fill a small receive window, then require server-initiated retirement.

    This does not time the exact server deadline: queued TCP data/FIN may take
    longer to reach the reader. It checks bounded retirement and later recovery.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        connection.settimeout(IO_TIMEOUT)
        connection.bind((source_ip, 0))
        try:
            connection.connect((host, port))
        except OSError as error:
            raise RuntimeError("slow-reader TCP connect failed: " + type(error).__name__) from None
        receive_bytes = connection.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        try:
            connection.sendall(b"GET /stream HTTP/1.1\r\nHost: nxrs\r\nConnection: close\r\n\r\n")
        except OSError as error:
            raise RuntimeError("slow-reader HTTP request failed: " + type(error).__name__) from None
        time.sleep(pause_seconds)
        deadline = time.monotonic() + drain_timeout
        total = 0
        retirement = None
        try:
            while total < MAX_FRAME + MAX_HEADERS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                connection.settimeout(remaining)
                data = connection.recv(min(8192, MAX_FRAME + MAX_HEADERS - total))
                if not data:
                    retirement = "eof"
                    break
                total += len(data)
        except ConnectionResetError:
            retirement = "reset"
        except socket.timeout:
            pass
    if retirement is None or total == 0:
        raise RuntimeError("slow reader did not receive data and a bounded peer close")
    return {"retirement": retirement, "received_bytes": total,
            "receive_buffer_bytes": receive_bytes, "paused_seconds": pause_seconds}


def _new_file(path, suffix):
    target = Path(path).absolute()
    if target.suffix.lower() != suffix:
        raise ValueError(f"output path must end in {suffix}")
    _outside_repo(target.parent)
    return target


class StreamClient:
    """Single bounded worker that samples, decodes, reconnects, and records evidence."""
    LATENCY_BOUNDS_MS = (0, 10, 20, 40, 80, 120, 200, 350, 500, 750, 1000, 1500, 2000, 3000, 5000)
    FPS_BOUNDS = (0, 1, 2, 5, 10, 15, 20, 25, 30)
    GAP_BOUNDS_MS = (250, 500, 1000, 2000, 5000, 10000, 30000)

    def __init__(self, host, port, duration, first_jpeg_path, pillow_image, clock_fn=time.monotonic,
                 source_ip=None, expected_size=(320, 240)):
        self.host, self.port, self.duration = host, port, duration
        self.first_jpeg_path, self.image = first_jpeg_path, pillow_image
        self.clock_fn = clock_fn
        self.source_ip = source_ip
        self.expected_size = expected_size
        self.stop_event = threading.Event()
        self._socket_lock = threading.Lock()
        self._active_socket = None
        self.thread = threading.Thread(target=self._run, name="mjpeg-qualification", daemon=False)
        self.error = None
        self.frames = 0
        self.connections = 0
        self.reconnects = 0
        self.gaps = 0
        self.gap_histogram = Counter()
        self.transport_failures = Counter()
        self.last_sequence = None
        self.clock_offset_ms = None
        self.clock_uncertainty_ms = None
        self.max_clock_uncertainty_ms = 0.0
        self.latency_histogram = Counter()
        self.fps_histogram = Counter()
        self._fps_window_start = None
        self._fps_window_frames = 0
        self._last_frame_host_ms = None
        self._first_saved = False
        self._last_forced_reconnect = None
        self.first_capture_ms = None
        self.last_capture_ms = None
        self.first_decode_ms = None
        self.started_ms = None
        self.payload_bytes = 0
        self.latency_sum_ms = 0.0
        self.latency_max_ms = 0.0
        self.latency_samples = 0
        self.latency_unavailable_frames = 0
        self.max_gap_ms = 0.0
        self.max_gap_detail = None
        self._last_frame_failures = Counter()

    def start(self):
        self.started_ms = self.clock_fn() * 1000
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        # Interrupt clock headers and partial frames before joining, rather
        # than waiting for each blocking operation's timeout in turn.
        with self._socket_lock:
            if self._active_socket is not None:
                try:
                    self._active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    @contextmanager
    def _track_socket(self, conn):
        with self._socket_lock:
            if self.stop_event.is_set():
                raise InterruptedError("stream client stopped")
            self._active_socket = conn
        try:
            yield
        finally:
            with self._socket_lock:
                if self._active_socket is conn:
                    self._active_socket = None

    def join(self, timeout):
        self.thread.join(timeout)

    def alive(self):
        return self.thread.is_alive()

    @staticmethod
    def _hist_bucket(value, bounds):
        for bound in bounds:
            if value <= bound:
                return str(bound)
        return f"gt_{bounds[-1]}"

    def _run(self):
        deadline = self.clock_fn() + self.duration
        previous_sequence = None
        disconnected_since = None
        calibrate_clock = True
        try:
            self._last_forced_reconnect = self.clock_fn()
            while not self.stop_event.is_set() and self.clock_fn() < deadline:
                conn = None
                stream = None
                phase = "clock"
                try:
                    # Calibrate on healthy transitions, not on every recovery
                    # retry: repeated probe timeouts must not delay viewing.
                    if calibrate_clock:
                        calibrate_clock = False
                        try:
                            self.clock_offset_ms, self.clock_uncertainty_ms = clock_sample(
                                self.host, self.port, self.clock_fn, self.stop_event, self.source_ip,
                                samples=5 if self.frames == 0 else 1, socket_scope=self._track_socket)
                            self.max_clock_uncertainty_ms = max(self.max_clock_uncertainty_ms,
                                                                self.clock_uncertainty_ms)
                        except (OSError, EOFError, TimeoutError) as error:
                            self.transport_failures[f"clock/{type(error).__name__}"] += 1
                            self.clock_offset_ms = self.clock_uncertainty_ms = None
                    if self.stop_event.is_set():
                        break
                    phase = "connect"
                    conn = socket.create_connection((self.host, self.port), timeout=IO_TIMEOUT,
                                                    source_address=(self.source_ip, 0) if self.source_ip else None)
                    with self._track_socket(conn):
                        conn.settimeout(IO_TIMEOUT)
                        conn.sendall(b"GET /stream HTTP/1.1\r\nHost: nxrs\r\nConnection: close\r\n\r\n")
                        stream = conn.makefile("rb", buffering=8192)
                        phase = "response"
                        read_mjpeg_response(stream)
                        if self.connections:
                            self.reconnects += 1
                        if disconnected_since is not None:
                            gap_ms = (self.clock_fn() - disconnected_since) * 1000
                            self.gap_histogram[self._hist_bucket(gap_ms, self.GAP_BOUNDS_MS)] += 1
                            if gap_ms > 2000:
                                self.gaps += 1
                        self.connections += 1
                        disconnected_since = None
                        phase = "frame"
                        while not self.stop_event.is_set() and self.clock_fn() < deadline:
                            sequence, capture_ms, jpeg = read_mjpeg_frame(stream)
                            if previous_sequence is not None and sequence <= previous_sequence:
                                raise ValueError("frame sequence did not increase across reconnects")
                            previous_sequence = sequence
                            decode_jpeg(self.image, jpeg, self.expected_size)
                            host_decode_ms = self.clock_fn() * 1000
                            self._record_frame(sequence, capture_ms, host_decode_ms, jpeg)
                            calibrate_clock = True
                            # Periodically force a client reconnect to exercise server recovery.
                            if self.clock_fn() - self._last_forced_reconnect >= 30:
                                self._last_forced_reconnect = self.clock_fn()
                                break
                except (OSError, EOFError, TimeoutError, ValueError) as error:
                    self.transport_failures[f"{phase}/{type(error).__name__}"] += 1
                    if isinstance(error, ValueError):
                        raise
                    calibrate_clock = False
                    self.clock_offset_ms = self.clock_uncertainty_ms = None
                    if self.stop_event.is_set() or self.clock_fn() >= deadline:
                        break
                finally:
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                    if conn is not None:
                        try:
                            conn.close()
                        except OSError:
                            pass
                if disconnected_since is None:
                    disconnected_since = self.clock_fn()
                self.stop_event.wait(min(0.15, max(0.0, deadline - self.clock_fn())))
            self._finish_fps_window()
            if self.frames == 0 and not self.stop_event.is_set():
                raise RuntimeError("no MJPEG frames were captured")
        except BaseException as error:
            if not self.stop_event.is_set():
                self.error = str(error) or type(error).__name__

    def _record_frame(self, sequence, capture_ms, decode_ms, jpeg):
        if self.clock_offset_ms is not None:
            latency_ms = decode_ms + self.clock_offset_ms - capture_ms
            if latency_ms < -self.clock_uncertainty_ms - 2:
                raise ValueError("capture timestamp is unexpectedly in the future")
            self.latency_histogram[self._hist_bucket(max(0, latency_ms), self.LATENCY_BOUNDS_MS)] += 1
            self.latency_sum_ms += max(0, latency_ms)
            self.latency_max_ms = max(self.latency_max_ms, latency_ms)
            self.latency_samples += 1
        else:
            self.latency_unavailable_frames += 1
        gap_ms = decode_ms - (self._last_frame_host_ms or decode_ms)
        if gap_ms > self.max_gap_ms:
            self.max_gap_ms = gap_ms
            # Keep one numeric window, not an unbounded per-frame/event trace.
            self.max_gap_detail = {
                "gap_ms": round(gap_ms, 2),
                "previous_capture_ms": self.last_capture_ms,
                "capture_ms": capture_ms,
                "previous_sequence": self.last_sequence,
                "sequence": sequence,
                "connections": self.connections,
                "transport_failures_since_previous_frame": dict(
                    self.transport_failures - self._last_frame_failures),
            }
        self._last_frame_failures = self.transport_failures.copy()
        if self.first_capture_ms is None:
            self.first_capture_ms = capture_ms
            self.first_decode_ms = decode_ms
        self.last_capture_ms = capture_ms
        self.payload_bytes += len(jpeg)
        if self._fps_window_start is None:
            self._fps_window_start = decode_ms
        self._fps_window_frames += 1
        if decode_ms - self._fps_window_start >= 10000:
            self._finish_fps_window(decode_ms)
        self._last_frame_host_ms = decode_ms
        self.frames += 1
        self.last_sequence = sequence
        if not self._first_saved:
            fd = os.open(self.first_jpeg_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(jpeg)
            self._first_saved = True

    def _finish_fps_window(self, now_ms=None):
        if self._fps_window_start is None or not self._fps_window_frames:
            return
        elapsed = max(0.001, ((now_ms or self._last_frame_host_ms or self._fps_window_start)
                              - self._fps_window_start) / 1000)
        fps = self._fps_window_frames / elapsed
        self.fps_histogram[self._hist_bucket(fps, self.FPS_BOUNDS)] += 1
        self._fps_window_start = now_ms or self._last_frame_host_ms
        self._fps_window_frames = 0


def delivery_snapshot(client, finished_ms):
    """Preserve numeric continuity evidence even when qualification fails."""
    if client is None:
        return None
    startup = (client.first_decode_ms - client.started_ms
               if client.first_decode_ms is not None and client.started_ms is not None else None)
    terminal = (finished_ms - client._last_frame_host_ms
                if client._last_frame_host_ms is not None else None)
    return {
        "startup_delay_ms": round(startup, 2) if startup is not None else None,
        "terminal_gap_ms": round(max(0, terminal), 2) if terminal is not None else None,
        "max_inter_frame_gap_ms": round(client.max_gap_ms, 2),
        "worst_inter_frame_gap": getattr(client, "max_gap_detail", None),
        "inter_frame_limit_ms": 10_000,
    }


def latency_snapshot(client):
    """Use the same nullable clock/latency evidence in PASS and FAIL reports."""
    samples = client.latency_samples if client else 0
    return {
        "clock_offset_device_minus_host_ms": (round(client.clock_offset_ms, 3)
                                              if client and client.clock_offset_ms is not None else None),
        "clock_uncertainty_ms": (round(client.clock_uncertainty_ms, 3)
                                  if client and client.clock_uncertainty_ms is not None else None),
        "max_clock_uncertainty_ms": round(client.max_clock_uncertainty_ms, 3) if samples else None,
        "capture_to_host_decode_latency_ms": {
            "definition": "host decode completion minus capture timestamp, after clock offset calibration; not exposure or display latency",
            "histogram_upper_bounds": list(StreamClient.LATENCY_BOUNDS_MS) + [None],
            "histogram_counts": dict(client.latency_histogram) if client else {},
            "samples": samples,
            "unmeasured_frames": client.latency_unavailable_frames if client else 0,
            "mean": round(client.latency_sum_ms / samples, 2) if samples else None,
            "max": round(client.latency_max_ms, 2) if samples else None,
        },
    }


def stop_client(client):
    if client is not None:
        client.stop()
        client.join(IO_TIMEOUT + 1)
        if client.alive():
            raise RuntimeError("host stream worker failed to stop")


def report_live_stall(client, now_ms, reported_frames):
    """Emit only numeric stall/recovery edges, independently of MCU reports."""
    if reported_frames is not None and client.frames != reported_frames:
        print(f'recovered decoded={client.frames}', flush=True)
        reported_frames = None
    if (reported_frames is None and client._last_frame_host_ms is not None
            and now_ms - client._last_frame_host_ms > 10000):
        print(f'stall decoded={client.frames} frame_age_ms={round(now_ms - client._last_frame_host_ms)}', flush=True)
        reported_frames = client.frames
    return reported_frames


def close_runner(client, console):
    """Close serial even when a worker cannot be joined; never hide that failure."""
    try:
        stop_client(client)
    finally:
        console.close()


def validate_delivery(client, finished_ms):
    """Earlier decoded frames cannot make a later, stalled stream pass."""
    if client is None or client.frames == 0 or client.first_decode_ms is None:
        raise RuntimeError("no decoded stream frames")
    if client.error:
        raise RuntimeError("host stream validation failed: " + client.error)
    startup_ms = client.first_decode_ms - client.started_ms
    terminal_ms = finished_ms - client._last_frame_host_ms
    if startup_ms > 30_000:
        raise RuntimeError("stream startup exceeded 30 seconds")
    if client.max_gap_ms > 10_000 or terminal_ms > 10_000:
        raise RuntimeError("stream delivery stalled for over 10 seconds")
    return delivery_snapshot(client, finished_ms)


def _private_report(path, payload):
    target = _new_file(path, ".json")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(payload, output, sort_keys=True, indent=2)
        output.write("\n")


def device_failure(data):
    match = re.fullmatch(rb'NXRS_STREAM_FAIL error=([ -~]{1,240})', data)
    if match:
        return match[1].decode('ascii')
    location = re.search(rb'at file: ([-A-Za-z0-9_/.]{1,200}):([0-9]{1,6})', data)
    if location:
        return f"device panic at {location[1].decode('ascii')}:{location[2].decode('ascii')}"
    return "device panic (raw output withheld)"


def parse_heap_snapshot(output):
    rows = re.findall(
        rb'^\s*(\d+)\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+(Kmem|Umem)\s*$',
        output, re.M)
    heaps = {name.decode('ascii'): dict(zip(('total', 'used', 'free'),
             map(int, (total, used, free)))) for total, used, free, name in rows}
    if len(rows) != 2 or set(heaps) != {'Kmem', 'Umem'}:
        raise ValueError('expected exactly one numeric Kmem and Umem row')
    if any(row['total'] != row['used'] + row['free'] for row in heaps.values()):
        raise ValueError('heap total does not equal used plus free')
    return heaps


def read_stopped_resources(console, pending, password):
    """Measure before closing serial: USB-UART line changes may reset the MCU."""
    def command(text, label):
        marker = f'NXRS_STREAM_{label}_STATUS_0'.encode('ascii')
        console.write(text + f'\necho NXRS_STREAM_{label}_STATUS_$?\n'.encode('ascii'))
        output = wait_stage(console, pending, marker, password, 5, 'post-stop resources')
        if re.search(rb'(?:^|\r?\n)' + marker + rb'$', output) is None:
            raise RuntimeError('post-stop resource command did not succeed')
        wait_stage(console, pending, b'nsh> ', password, 5, 'post-stop prompt')
        return output

    heaps = [parse_heap_snapshot(command(b'free', f'HEAP_{index}')) for index in range(2)]
    uptime = re.findall(rb'^\s*(\d+\.\d{2})\s*$', command(b'cat /proc/uptime', 'UPTIME'), re.M)
    if len(uptime) != 1:
        raise ValueError('expected one numeric device uptime')
    return {'heap_samples': heaps, 'uptime_seconds': float(uptime[0]),
            'context': 'after command exit, before closing the original serial connection'}


def qualify(args):
    import serial
    from PIL import Image

    host_ip = ipaddress.IPv4Address(args.host_ip)
    dimensions = (getattr(args, 'width', 320), getattr(args, 'height', 240))
    if dimensions not in ((320, 240), (640, 480)):
        raise ValueError("supported JPEG sizes are 320x240 and 640x480")
    quality = getattr(args, 'quality', 81)
    if not 1 <= quality <= 100:
        raise ValueError("JPEG quality must be 1..100")
    if host_ip.is_unspecified or host_ip.is_multicast or host_ip.is_loopback:
        raise ValueError("--host-ip must be the Mac LAN IPv4 address")
    if not 10 <= args.seconds <= 3600 or args.reconnect_at < 0 or args.reconnect_at >= args.seconds and args.reconnect_at != 0:
        raise ValueError("seconds must be 10–3600 and reconnect-at must be 0 or less than seconds")
    if not args.port:
        raise ValueError("--port must name the serial device")
    if args.slow_reader and args.seconds < 30:
        raise ValueError("--slow-reader requires at least 30 seconds")
    output = _new_file(args.output, ".jpg")
    report_path = _new_file(args.report, ".json")
    if output.exists() or report_path.exists() or output == report_path:
        raise ValueError("--output and --report must be distinct new paths")
    ssid, password = read_wifi_file(args.wifi_file)
    console = serial.Serial()
    console.port, console.baudrate, console.timeout = args.port, 115200, 0.1
    console.write_timeout = IO_TIMEOUT
    console.dtr = console.rts = False
    console.open()
    pending = bytearray()
    records = []
    link_events = []
    client = None
    slow_probe = None
    stopped_resources = None
    finished_ms = None
    reported_stall_frames = None
    pass_seen = False
    started = time.monotonic()
    try:
        with console:
            console.write(b"\n\n\n")
            wait_stage(console, pending, b"nsh> ", b"", 10, "NSH startup")
            command = f"stream_test {args.seconds} {args.reconnect_at} {dimensions[0]} {dimensions[1]} {quality}\n".encode("ascii")
            console.write(command)
            wait_stage(console, pending, b"NXRS_WIFI_CREDENTIALS_READY", b"", 10, "credential reader startup")
            console.write(bytes((len(ssid), len(password))) + ssid + password)
            # The reusable helper enforces bounded marker waits and suppresses raw console output.
            online_event = wait_stage(console, pending, b"NXRS_STREAM_ONLINE", password, 60, "station association / stream startup")
            line_buffer = bytearray(online_event)
            line_buffer.extend(pending)
            pending.clear()
            if len(line_buffer) > SERIAL_LIMIT:
                raise RuntimeError("bounded serial event buffer exceeded")
            line = bytes(line_buffer)
            match = ONLINE_RE.search(line)
            if match is None:
                # read_until removes through its marker; gather only the marker's line, never log it.
                deadline = time.monotonic() + 5
                marker_start = line_buffer.find(b"NXRS_STREAM_ONLINE")
                while line_buffer.find(b"\n", marker_start) < 0 and time.monotonic() < deadline:
                    line_buffer.extend(console.read(512))
                    if len(line_buffer) > SERIAL_LIMIT:
                        raise RuntimeError("bounded serial event buffer exceeded")
                match = ONLINE_RE.search(bytes(line_buffer))
            if match is None:
                raise RuntimeError("device online marker was malformed")
            line_end = line_buffer.find(b"\n", match.end())
            if line_end >= 0:
                pending.extend(line_buffer[line_end + 1:])
            device_ip, device_port = str(ipaddress.IPv4Address(match.group(1).decode("ascii"))), int(match.group(2))
            link_events.append({'ms': int(match.group(3)), 'online': True})
            if device_ip in ("0.0.0.0", "255.255.255.255") or not 1 <= device_port <= 65535:
                raise RuntimeError("device reported an invalid stream endpoint")
            if args.slow_reader:
                # An assigned address is not proof that the HTTP owner has
                # dispatched its first poll. Establish normal endpoint readiness
                # before applying backpressure to a separate connection.
                clock_sample(device_ip, device_port, source_ip=str(host_ip), samples=5)
                slow_probe = probe_slow_reader(device_ip, device_port, str(host_ip))
            # MCU and host duration clocks need not run at the same rate. Keep
            # decoding until the coordinator sees device completion and stops us.
            client = StreamClient(device_ip, device_port, args.seconds + COMPLETION_GRACE_SECONDS, output, Image,
                                  source_ip=str(host_ip), expected_size=dimensions)
            client.start()

            status_buf = bytearray(pending)
            deadline = time.monotonic() + args.seconds + COMPLETION_GRACE_SECONDS
            while time.monotonic() < deadline:
                chunk = console.read(512)
                if chunk:
                    status_buf.extend(chunk)
                    if len(status_buf) > SERIAL_LIMIT:
                        status_buf[:] = status_buf[-4096:]
                    if password and password in status_buf:
                        raise RuntimeError("credential echo detected; serial output withheld")
                    while b"\n" in status_buf:
                        raw_line, _, rest = status_buf.partition(b"\n")
                        status_buf = bytearray(rest)
                        clean = raw_line.strip(b"\r\t ")
                        if any(marker in clean for marker in FAIL_MARKERS):
                            raise RuntimeError("device failure: " + device_failure(clean))
                        if not ALLOWED_LINE_RE.match(clean):
                            continue
                        if clean.startswith(b"NXRS_STREAM_REPORT"):
                            metric = REPORT_RE.match(clean)
                            if metric is None:
                                raise RuntimeError("device resource report was malformed")
                            if len(records) < REPORT_LIMIT:
                                names = ("ms", "captured", "delivered", "bytes", "max_bytes", "heap_user",
                                         "heap_kernel", "cpu_busy_pm", "owner_cpu_ms", "online", "attempts",
                                         "rejoined", "errors")
                                records.append(dict(zip(names, (int(x) for x in metric.groups()))))
                                frame_age_ms = (None if client._last_frame_host_ms is None else
                                                max(0, round(time.monotonic() * 1000 - client._last_frame_host_ms)))
                                if len(records) % 6 == 0 or (frame_age_ms is not None and frame_age_ms > 10_000):
                                    row = records[-1]
                                    print(f"progress device_s={row['ms'] // 1000} decoded={client.frames} "
                                          f"heap_user={row['heap_user']} heap_kernel={row['heap_kernel']} "
                                          f"online={row['online']} rejoined={row['rejoined']} "
                                          f"frame_age_ms={frame_age_ms}", flush=True)
                        elif clean.startswith(b"NXRS_STREAM_OFFLINE"):
                            offline = OFFLINE_RE.match(clean)
                            if offline and len(link_events) < 16:
                                link_events.append({'ms': int(offline.group(1)), 'online': False})
                        elif clean.startswith(b"NXRS_STREAM_ONLINE"):
                            match = ONLINE_RE.match(clean)
                            if match:
                                if len(link_events) < 16:
                                    link_events.append({'ms': int(match.group(3)), 'online': True})
                                current_ip = str(ipaddress.IPv4Address(match.group(1).decode("ascii")))
                                if current_ip != device_ip:
                                    # The listener is bound to all interfaces;
                                    # DHCP is allowed to assign a new address.
                                    client.host = current_ip
                                    device_ip = current_ip
                        elif clean.startswith(b"NXRS_STREAM_PASS"):
                            if not PASS_RE.match(clean):
                                raise RuntimeError("device pass marker was malformed")
                            pass_seen = True
                            break
                    if pass_seen:
                        break
                if client.error:
                    raise RuntimeError(f"host stream validation failed: {client.error}")
                reported_stall_frames = report_live_stall(
                    client, time.monotonic() * 1000, reported_stall_frames)
            if not pass_seen:
                raise RuntimeError("device did not report stream completion before deadline")
            stop_client(client)
            finished_ms = time.monotonic() * 1000
            console.write(b"echo NXRS_STREAM_STATUS_$?\n")
            status_marker = WIFI.read_until(console, pending, b"NXRS_STREAM_STATUS_0", password, 5)
            WIFI.read_until(console, pending, b"nsh> ", password, 5)
            if re.search(rb"(?:^|\r?\n)NXRS_STREAM_STATUS_0$", status_marker) is None:
                raise RuntimeError("NSH stream command exit status was not zero")
            stopped_resources = read_stopped_resources(console, pending, password)
            if stopped_resources['uptime_seconds'] < args.seconds:
                raise RuntimeError('device uptime is shorter than the completed qualification')
            continuity = validate_delivery(client, finished_ms)
    except (OSError, ValueError, RuntimeError) as error:
        # Preserve only validated numeric reports and our sanitized error.
        # Never save raw serial input, SSID, or credentials, including on failure.
        _private_report(report_path, {"result": "FAIL", "reason": str(error),
                                     "width": dimensions[0], "height": dimensions[1],
                                     "jpeg_quality": quality,
                                     "requested_seconds": args.seconds,
                                     "transport_timeout_seconds": IO_TIMEOUT,
                                     "device_ip": client.host if client else None,
                                     "stream_port": client.port if client else None,
                                     "firmware_resource_samples": records,
                                     "frames_decoded": client.frames if client else 0,
                                     "link_events": link_events,
                                     "post_stop_resources": stopped_resources,
                                     "delivery_continuity": delivery_snapshot(
                                         client, finished_ms if finished_ms is not None else time.monotonic() * 1000),
                                     **latency_snapshot(client),
                                     "transport_failures": dict(client.transport_failures) if client else {},
                                     "client_error": client.error if client else None})
        raise
    finally:
        close_runner(client, console)
    if client is None or client.error or client.frames == 0 or not client._first_saved:
        raise RuntimeError("host stream qualification produced no valid saved frame")
    if not pass_seen:
        raise RuntimeError("device stream did not pass")
    ram_delta = None
    if len(records) >= 2:
        warm = records[0]
        recent = records[-1]
        ram_delta = {key: recent[key] - warm[key] for key in ("heap_user", "heap_kernel")}
    payload = {
        "result": "PASS",
        "device_ip": client.host,
        "stream_port": client.port,
        "requested_seconds": args.seconds,
        "transport_timeout_seconds": IO_TIMEOUT,
        "width": dimensions[0],
        "height": dimensions[1],
        "jpeg_quality": quality,
        "device_reconnect_at_seconds": args.reconnect_at,
        "link_events": link_events,
        "frames_decoded": client.frames,
        "jpeg_payload_bytes": client.payload_bytes,
        "mean_jpeg_bytes": round(client.payload_bytes / client.frames, 1),
        "delivered_fps": round((client.frames - 1) * 1000 /
                               max(1, client._last_frame_host_ms - client.first_decode_ms), 2),
        "max_inter_frame_gap_ms": round(client.max_gap_ms, 2),
        "delivery_continuity": continuity,
        "slow_reader_probe": slow_probe,
        "last_sequence": client.last_sequence,
        "connections": client.connections,
        "client_reconnects": client.reconnects,
        "transport_failures": dict(client.transport_failures),
        "connection_recovery_gaps_over_2s": client.gaps,
        "reconnect_gap_histogram_ms": {
            "upper_bounds": list(client.GAP_BOUNDS_MS) + [None],
            "counts": dict(client.gap_histogram),
        },
        **latency_snapshot(client),
        "ten_second_fps_histogram": {
            "upper_bounds": list(client.FPS_BOUNDS) + [None],
            "window_counts": dict(client.fps_histogram),
        },
        "firmware_resource_samples": records,
        "heap_used_delta_first_to_last_sample_bytes": ram_delta,
        "heap_delta_interpretation": "sample delta only; not proof of zero RAM growth",
        "post_stop_resources": stopped_resources,
        "image_path": str(output),
        "elapsed_host_seconds": round(time.monotonic() - started, 3),
    }
    _private_report(report_path, payload)
    return report_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="serial device path (for example /dev/cu.usbmodem*)")
    parser.add_argument("--host-ip", required=True, help="Mac LAN IPv4 address")
    parser.add_argument("--wifi-file", type=Path, required=True)
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--quality", type=int, default=81, help="normalized JPEG quality: 1 lowest, 100 highest")
    parser.add_argument("--reconnect-at", type=int, required=True, help="device Wi-Fi reconnect time; 0 disables")
    parser.add_argument("--slow-reader", action="store_true", help="first stall a small-window reader, then require normal viewing")
    parser.add_argument("--output", type=Path, required=True, help="new private .jpg path outside the repository")
    parser.add_argument("--report", type=Path, required=True, help="new private .json path outside the repository")
    args = parser.parse_args()
    try:
        report = qualify(args)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"FAIL: {error}\n")
    print(f"PASS: bounded MJPEG qualification; report {report}")


if __name__ == "__main__":
    main()
