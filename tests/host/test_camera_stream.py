"""Host parser and control tests for MJPEG qualification; no hardware results."""
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import socket
import sys
import threading
import time
from unittest.mock import MagicMock, Mock, patch
from types import SimpleNamespace
import unittest

try:
    from PIL import Image
except ImportError:
    Image = None

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("camera_stream_run", ROOT / "tests/nuttx-std/stream/run.py")
RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN)


def jpeg_fixture():
    if Image is None:
        raise unittest.SkipTest("install stream/requirements.txt for real codec controls")
    image = Image.new("RGB", (320, 240), (80, 120, 160))
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def part(sequence=1, capture_ms=123, body=None):
    body = b'\xff\xd8parser-control\xff\xd9' if body is None else body
    headers = (f"--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(body)}\r\n"
               f"X-Sequence: {sequence}\r\nX-Capture-Ms: {capture_ms}\r\n\r\n").encode()
    return headers + body + b"\r\n"


class StreamParserTests(unittest.TestCase):
    def test_camera_discard_count_is_optional_for_older_firmware(self):
        legacy = (b'NXRS_STREAM_REPORT ms=10000 captured=178 delivered=175 bytes=1000000 '
                  b'max_bytes=10000 heap_user=1743496 heap_kernel=60000 cpu_busy_pm=540 '
                  b'owner_cpu_ms=3700 online=1 attempts=1 rejoined=0 errors=0')
        self.assertIsNone(RUN.REPORT_RE.fullmatch(legacy).groups()[-1])
        self.assertEqual(RUN.REPORT_RE.fullmatch(legacy + b' camera_discarded=2').groups()[-1], b'2')
        self.assertIsNone(RUN.REPORT_RE.fullmatch(legacy + b' camera_discarded=-1'))

    def test_failure_diagnostics_only_accept_the_bounded_firmware_marker(self):
        self.assertEqual(RUN.device_failure(b'NXRS_STREAM_FAIL error=camera stalled'), 'camera stalled')
        self.assertEqual(RUN.device_failure(b'PANIC arbitrary output'), 'device panic (raw output withheld)')
        self.assertEqual(RUN.device_failure(b'Assertion failed : at file: chip/adapter.c:123 task: wifi'),
                         'device panic at chip/adapter.c:123')
        self.assertEqual(RUN.device_failure(b'NXRS_STREAM_FAIL error=' + b'x' * 241),
                         'device panic (raw output withheld)')

    def test_multipart_frame_is_bounded_and_has_expected_metadata(self):
        jpeg = b'\xff\xd8parser-control\xff\xd9'
        sequence, capture, body = RUN.read_mjpeg_frame(io.BytesIO(part(42, 99, jpeg)))
        self.assertEqual((sequence, capture, body), (42, 99, jpeg))

    def test_rejects_bad_boundary_headers_length_and_terminator(self):
        valid = part()
        cases = (
            valid.replace(b"--nxrs-frame", b"--wrong", 1),
            valid.replace(b"X-Sequence: 1", b"X-Sequence: -1"),
            valid.replace(b"Content-Length: ", b"Content-Length: 153601", 1),
            valid[:-2],
            valid.replace(b"Content-Type: image/jpeg", b"Content-Type: image/png"),
        )
        for broken in cases:
            with self.subTest(prefix=broken[:80]):
                with self.assertRaises((ValueError, EOFError)):
                    RUN.read_mjpeg_frame(io.BytesIO(broken))

    def test_response_requires_expected_multipart_boundary(self):
        response = (b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n\r\n")
        self.assertIn(b"content-type", RUN.read_mjpeg_response(io.BytesIO(response)))
        with self.assertRaises(ValueError):
            RUN.read_mjpeg_response(io.BytesIO(response.replace(b"nxrs-frame", b"other")))

    def test_real_decoder_requires_320_by_240_jpeg(self):
        jpeg = jpeg_fixture()
        RUN.decode_jpeg(Image, jpeg)
        small = Image.new("RGB", (160, 120))
        output = io.BytesIO()
        small.save(output, format="JPEG")
        for invalid in (output.getvalue(), b"not a JPEG"):
            with self.subTest(size=len(invalid)), self.assertRaises(ValueError):
                RUN.decode_jpeg(Image, invalid)

    @unittest.skipIf(Image is None, 'install stream/requirements.txt for real codec controls')
    def test_real_decoder_checks_the_selected_resolution(self):
        image = Image.new('RGB', (640, 480))
        output = io.BytesIO()
        image.save(output, format='JPEG')
        RUN.decode_jpeg(Image, output.getvalue(), (640, 480))
        with self.assertRaises(ValueError):
            RUN.decode_jpeg(Image, output.getvalue())


class WifiFileTests(unittest.TestCase):
    def write(self, directory, value, mode=0o600):
        path = Path(directory) / "wifi"
        path.write_bytes(value)
        path.chmod(mode)
        return path

    def test_reads_exact_keys_and_supported_separators(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, b"SSID=lab-net\npassword=secret123\n")
            self.assertEqual(RUN.read_wifi_file(path), (b"lab-net", b"secret123"))
            path.write_bytes(b"SSID: lab-net\npassword: secret123\n")
            self.assertEqual(RUN.read_wifi_file(path), (b"lab-net", b"secret123"))
            path.write_bytes(b"ssid= lab-net\npassword= secret123\n")
            self.assertEqual(RUN.read_wifi_file(path), (b" lab-net", b" secret123"))
            path.write_bytes(b"ssid:lab-net\nPassword:sec=ret123\n")
            path.chmod(0o400)
            self.assertEqual(RUN.read_wifi_file(path), (b"lab-net", b"sec=ret123"))
            path.chmod(0o600)
            path.write_bytes(b"SSID:lab-net\npassword:secret123\n")
            path.chmod(0o600)
            self.assertEqual(RUN.read_wifi_file(path), (b"lab-net", b"secret123"))

    def test_rejects_wrong_permissions_duplicates_and_invalid_lengths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, b"SSID=lab\npassword=secret123\n", 0o640)
            with self.assertRaises(ValueError):
                RUN.read_wifi_file(path)
            for value in (b"SSID=x\nSSID=y\npassword=secret123\n",
                          b"SSID=x\npassword=short\n", b"SSID=\npassword=secret123\n",
                          b"SSID=x\n\npassword=secret123\n",
                          b"SSID=x\npassword=secret123\nextra=z\n",
                          b"SSID=x\npassword=secret123\n" + b" " * 240):
                path.write_bytes(value)
                path.chmod(0o600)
                with self.subTest(data_length=len(value)), self.assertRaises(ValueError):
                    RUN.read_wifi_file(path)

    def test_rejects_symlinks_without_following_them(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self.write(directory, b"SSID=x\npassword=secret123\n")
            link = Path(directory) / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                RUN.read_wifi_file(link)


class HistogramTests(unittest.TestCase):
    def test_continuous_viewing_does_not_force_connections_or_clock_probes(self):
        for interval, expected_connections in ((0, 1), (30, 3)):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory() as directory:
                now = [0.0]
                client = RUN.StreamClient('127.0.0.1', 8080, 100,
                                          Path(directory) / 'frame.jpg', None,
                                          clock_fn=lambda: now[0],
                                          reconnect_every_seconds=interval)
                client.stop_event.wait = Mock(return_value=False)
                count = [0]

                def read_frame(stream):
                    count[0] += 1
                    now[0] = count[0] * 31.0
                    if count[0] == 3:
                        client.stop_event.set()
                    return count[0], int(now[0] * 1000), b'parser-control'

                with patch.object(RUN.socket, 'create_connection') as connect, \
                     patch.object(RUN, 'clock_sample', return_value=(0, 1)) as clock, \
                     patch.object(RUN, 'read_mjpeg_response'), \
                     patch.object(RUN, 'read_mjpeg_frame', side_effect=read_frame), \
                     patch.object(RUN, 'decode_jpeg'):
                    client._run()
                self.assertEqual(client.frames, 3)
                self.assertEqual(connect.call_count, expected_connections)
                self.assertEqual(clock.call_count, expected_connections)
                self.assertEqual(client.reconnects, expected_connections - 1)
                self.assertIsNone(client.error)

    def test_client_reconnect_interval_is_bounded(self):
        for invalid in (-1, 3601, 0.5, None):
            with self.subTest(interval=invalid), self.assertRaises(ValueError):
                RUN.StreamClient('127.0.0.1', 8080, 100, Path('/unused.jpg'), None,
                                 reconnect_every_seconds=invalid)

    def test_slow_reader_requires_data_and_peer_close_not_just_a_timeout(self):
        with patch.object(RUN.socket, 'socket') as factory:
            connection = factory.return_value.__enter__.return_value
            connection.getsockopt.return_value = 2048
            connection.recv.side_effect = [b'HTTP/1.1 200 OK\r\n', b'']
            result = RUN.probe_slow_reader('127.0.0.1', 8080, '127.0.0.1', pause_seconds=0)
            self.assertEqual(result['retirement'], 'eof')
            self.assertEqual(result['received_bytes'], 17)
            connection.recv.side_effect = [b'']
            with self.assertRaises(RuntimeError):
                RUN.probe_slow_reader('127.0.0.1', 8080, '127.0.0.1', pause_seconds=0)
            connection.recv.side_effect = RUN.socket.timeout()
            with self.assertRaises(RuntimeError):
                RUN.probe_slow_reader('127.0.0.1', 8080, '127.0.0.1', pause_seconds=0)
            connection.recv.side_effect = [b'HTTP/1.1 200 OK\r\n', ConnectionResetError()]
            result = RUN.probe_slow_reader('127.0.0.1', 8080, '127.0.0.1', pause_seconds=0)
            self.assertEqual(result['retirement'], 'reset')

    def test_default_slow_reader_pause_exceeds_send_budget_and_window_fill(self):
        with patch.object(RUN.socket, 'socket') as factory, patch.object(RUN.time, 'sleep') as sleep:
            connection = factory.return_value.__enter__.return_value
            connection.getsockopt.return_value = 33580
            connection.recv.side_effect = [b'HTTP/1.1 200 OK\r\n', b'']
            result = RUN.probe_slow_reader('127.0.0.1', 8080, '127.0.0.1')
            sleep.assert_called_once_with(10)
            self.assertEqual(result['paused_seconds'], 10)

    def test_old_frames_cannot_qualify_a_stream_that_later_stalled(self):
        client = SimpleNamespace(frames=100, first_decode_ms=1000, started_ms=0,
                                 _last_frame_host_ms=89_000, max_gap_ms=1000, error=None)
        self.assertEqual(RUN.validate_delivery(client, 90_000)['terminal_gap_ms'], 1000)
        for updates in ({'_last_frame_host_ms': 45_000}, {'max_gap_ms': 10_001},
                        {'first_decode_ms': 30_001}, {'frames': 0}):
            original = vars(client).copy()
            with self.subTest(updates=updates), self.assertRaises(RuntimeError):
                RUN.validate_delivery(SimpleNamespace(**(original | updates)), 90_000)

    def test_stop_interrupts_a_partial_frame_before_join(self):
        accepted = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(2)

            def serve():
                with listener.accept()[0] as connection:
                    connection.settimeout(2)
                    connection.recv(1024)
                    connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n\r\n'
                                       b'--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: 153600\r\n'
                                       b'X-Sequence: 1\r\nX-Capture-Ms: 1\r\n\r\n\xff\xd8')
                    accepted.set()
                    release.wait(2)

            server = threading.Thread(target=serve)
            server.start()
            client = RUN.StreamClient('127.0.0.1', listener.getsockname()[1], 10,
                                      Path(directory) / 'frame.jpg', None)
            try:
                with patch.object(RUN, 'clock_sample', return_value=(0, 1)):
                    client.start()
                    self.assertTrue(accepted.wait(1))
                    client.stop()
                    client.join(0.5)
                    self.assertFalse(client.alive(), 'partial frame reader did not stop')
                    self.assertEqual(client.max_clock_uncertainty_ms, 1)
            finally:
                client.stop()
                client.join(3)
                release.set()
                server.join(3)
                self.assertFalse(server.is_alive())

    def test_stop_interrupts_partial_clock_headers_before_join(self):
        accepted = threading.Event()
        peer_closed = threading.Event()
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(2)

            def serve():
                with listener.accept()[0] as connection:
                    connection.settimeout(2)
                    connection.recv(1024)
                    connection.sendall(b'HTTP/1.1 200 OK\r\nX-Clock-Ms: ')
                    accepted.set()
                    try:
                        if connection.recv(1) == b'':
                            peer_closed.set()
                    except OSError:
                        pass

            server = threading.Thread(target=serve)
            server.start()
            client = RUN.StreamClient('127.0.0.1', listener.getsockname()[1], 10,
                                      Path(directory) / 'frame.jpg', None)
            try:
                client.start()
                self.assertTrue(accepted.wait(1))
                client.stop()
                client.join(0.75)
                self.assertFalse(client.alive(), 'clock header reader did not stop')
                self.assertTrue(peer_closed.wait(0.5))
                self.assertIsNone(client._active_socket)
                self.assertIsNone(client.error)
            finally:
                client.stop()
                client.join(3)
                server.join(3)
                self.assertFalse(server.is_alive())

    def test_connect_finishing_after_stop_does_not_send_or_start_another_connection(self):
        for phase in ('clock', 'stream'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                connecting = threading.Event()
                connected = threading.Event()
                connection = MagicMock()
                connection.__enter__.return_value = connection

                def connect(*args, **kwargs):
                    connecting.set()
                    if not connected.wait(2):
                        raise TimeoutError('test did not release connect')
                    return connection

                client = RUN.StreamClient('127.0.0.1', 8080, 10,
                                          Path(directory) / 'frame.jpg', None)
                with patch.object(RUN.socket, 'create_connection', side_effect=connect) as factory:
                    # Leave the clock probe real in the first case; in the
                    # second, exercise cancellation during the stream connect.
                    with patch.object(RUN, 'clock_sample', return_value=(0, 1)) if phase == 'stream' \
                            else patch.object(RUN, 'clock_sample', wraps=RUN.clock_sample):
                        try:
                            client.start()
                            self.assertTrue(connecting.wait(1))
                            client.stop()
                            connected.set()
                            client.join(0.75)
                            self.assertFalse(client.alive())
                            factory.assert_called_once()
                            connection.sendall.assert_not_called()
                            connection.makefile.assert_not_called()
                            if phase == 'clock':
                                connection.__exit__.assert_called_once()
                            else:
                                connection.close.assert_called_once()
                            self.assertIsNone(client._active_socket)
                        finally:
                            client.stop()
                            connected.set()
                            client.join(3)

    def test_malformed_clock_data_remains_a_fatal_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = RUN.StreamClient('127.0.0.1', 8080, 10, Path(directory) / 'frame.jpg', None)
            with patch.object(RUN.socket, 'create_connection') as factory:
                connection = factory.return_value.__enter__.return_value
                connection.makefile.return_value = io.BytesIO(
                    b'HTTP/1.1 200 OK\r\nX-Clock-Ms: not-a-number\r\n\r\n')
                client.start()
                client.join(0.75)
                self.assertFalse(client.alive())
                self.assertIn('X-Clock-Ms', client.error)
                factory.assert_called_once()
                self.assertEqual(client.frames, 0)
                self.assertIsNone(client._active_socket)

    def test_failed_continuity_keeps_one_bounded_numeric_gap_window(self):
        with tempfile.TemporaryDirectory() as directory:
            client = RUN.StreamClient('127.0.0.1', 8080, 30,
                                      Path(directory) / 'first.jpg', None)
            client.started_ms = 0
            client.clock_offset_ms = 0
            client.clock_uncertainty_ms = 1
            client._record_frame(1, 1000, 1000, b'parser-control')
            client.transport_failures['clock/TimeoutError'] += 5
            client._record_frame(60, 13000, 13000, b'parser-control')
            with self.assertRaises(RuntimeError):
                RUN.validate_delivery(client, 14000)
            snapshot = RUN.delivery_snapshot(client, 14000)
            self.assertEqual(snapshot['max_inter_frame_gap_ms'], 12000)
            self.assertEqual(snapshot['worst_inter_frame_gap'], {
                'gap_ms': 12000, 'previous_capture_ms': 1000, 'capture_ms': 13000,
                'previous_sequence': 1, 'sequence': 60, 'connections': 0,
                'transport_failures_since_previous_frame': {'clock/TimeoutError': 5}})
            client._record_frame(61, 13200, 13200, b'parser-control')
            self.assertEqual(RUN.delivery_snapshot(client, 14000)['worst_inter_frame_gap'],
                             snapshot['worst_inter_frame_gap'])
            self.assertIsNone(RUN.delivery_snapshot(None, 14000))

    def test_device_minus_host_clock_offset_has_correct_sign(self):
        with tempfile.TemporaryDirectory() as directory:
            client = RUN.StreamClient('127.0.0.1', 8080, 10, Path(directory) / 'first.jpg', None)
            client.clock_offset_ms = 125
            client.clock_uncertainty_ms = 2
            client._record_frame(1, 1000, 880, b'parser-control')
            self.assertEqual(client.latency_histogram, {'10': 1})

    def test_clock_probe_timeout_does_not_gate_stream_delivery(self):
        release = threading.Event()
        request_seen = threading.Event()
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(0.1)

            def serve():
                while not release.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(1)
                        request = connection.recv(1024)
                        if request.startswith(b'GET /stream '):
                            request_seen.set()
                            connection.sendall(
                                b'HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n\r\n'
                                b'--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n'
                                b'X-Sequence: 1\r\nX-Capture-Ms: 1000\r\n\r\n\xff\xd8\xff\xd9\r\n')
                            release.wait(2)
                    break

            server = threading.Thread(target=serve)
            server.start()
            client = RUN.StreamClient('127.0.0.1', listener.getsockname()[1], 5,
                                      Path(directory) / 'frame.jpg', None)
            try:
                with patch.object(RUN, 'clock_sample', side_effect=TimeoutError), \
                     patch.object(RUN, 'decode_jpeg'):
                    client.start()
                    self.assertTrue(request_seen.wait(1), 'clock probe blocked video admission')
                    deadline = time.monotonic() + 1
                    while client.frames == 0 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(client.frames, 1)
                    self.assertEqual(client.latency_samples, 0)
                    self.assertEqual(client.latency_unavailable_frames, 1)
                    self.assertFalse(client.latency_histogram)
                    self.assertGreater(client.transport_failures['clock/TimeoutError'], 0)
            finally:
                client.stop()
                client.join(3)
                release.set()
                server.join(3)
                self.assertFalse(client.alive())
                self.assertFalse(server.is_alive())

    def test_failed_retries_skip_clock_probes_until_a_frame_then_recalibrate(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0.0]
            client = RUN.StreamClient('127.0.0.1', 8080, 100,
                                      Path(directory) / 'frame.jpg', None,
                                      clock_fn=lambda: now[0])
            client.stop_event.wait = Mock(return_value=False)
            connection = MagicMock()
            connection.makefile.return_value = io.BytesIO()
            connects = [TimeoutError('outage'), ConnectionError('outage'), connection]
            probes = []

            def clock_probe(*args, **kwargs):
                probes.append(True)
                if len(probes) == 1:
                    raise TimeoutError('clock unavailable during outage')
                client.stop_event.set()
                return 0, 1

            def read_frame(stream):
                now[0] = 31.0
                return 1, 1000, b'jpeg'

            with patch.object(RUN.socket, 'create_connection', side_effect=connects) as connect, \
                 patch.object(RUN, 'clock_sample', side_effect=clock_probe), \
                 patch.object(RUN, 'read_mjpeg_response'), \
                 patch.object(RUN, 'read_mjpeg_frame', side_effect=read_frame), \
                 patch.object(RUN, 'decode_jpeg'):
                client._run()

            self.assertEqual(len(probes), 2)
            self.assertEqual(connect.call_count, 3)
            self.assertEqual(client.frames, 1)
            self.assertEqual(client.latency_unavailable_frames, 1)
            self.assertIsNone(client.error)
            self.assertEqual(client.transport_failures['clock/TimeoutError'], 1)
            self.assertEqual(client.transport_failures['connect/TimeoutError'], 1)
            self.assertEqual(client.transport_failures['connect/ConnectionError'], 1)

    def test_peer_retry_can_complete_without_replacing_the_connection(self):
        for timeout, expected_frames in ((2.0, 0), (RUN.IO_TIMEOUT, 1)):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as directory:
                now = [0.0]
                client = RUN.StreamClient('127.0.0.1', 8080, 10,
                                          Path(directory) / 'frame.jpg', None,
                                          clock_fn=lambda: now[0])
                client.stop_event.wait = Mock(return_value=False)
                connection = MagicMock()
                connection.makefile.return_value = io.BytesIO()

                def connect(*args, timeout, **kwargs):
                    now[0] += min(timeout, 3.0)
                    if timeout < 3.0:
                        raise TimeoutError('peer retry has not completed')
                    return connection

                def read_frame(stream):
                    client.stop_event.set()
                    return 1, 1000, b'jpeg'

                with patch.object(RUN, 'IO_TIMEOUT', timeout), \
                     patch.object(RUN.socket, 'create_connection', side_effect=connect), \
                     patch.object(RUN, 'clock_sample', return_value=(0, 1)), \
                     patch.object(RUN, 'read_mjpeg_response'), \
                     patch.object(RUN, 'read_mjpeg_frame', side_effect=read_frame), \
                     patch.object(RUN, 'decode_jpeg'):
                    client._run()
                self.assertEqual(client.frames, expected_frames)
                if expected_frames:
                    self.assertIsNone(client.error)
                else:
                    self.assertIn('no MJPEG frames', client.error)

    def test_fixed_bins_do_not_retain_per_frame_samples(self):
        self.assertEqual(RUN.StreamClient._hist_bucket(21, (0, 10, 20, 40)), "40")
        self.assertEqual(RUN.StreamClient._hist_bucket(90, (0, 10, 20, 40)), "gt_40")


class ResourceTests(unittest.TestCase):
    def test_heap_snapshot_requires_one_consistent_numeric_row_per_heap(self):
        output = (b"  123456  23456  100000  0  0  0  0 Kmem\n"
                  b"  654321  54321  600000  0  0  0  0 Umem\n")
        self.assertEqual(RUN.parse_heap_snapshot(output), {
            'Kmem': {'total': 123456, 'used': 23456, 'free': 100000},
            'Umem': {'total': 654321, 'used': 54321, 'free': 600000},
        })
        malformed = (
            output.replace(b' Umem', b' Xmem'),
            output + output.splitlines(keepends=True)[0],
            output.replace(b'  100000 ', b'  99999 '),
        )
        for broken in malformed:
            with self.subTest(output=broken), self.assertRaises(ValueError):
                RUN.parse_heap_snapshot(broken)

    def test_stopped_resource_reads_use_original_console_and_unique_commands(self):
        console, pending = Mock(), bytearray()
        seen = []
        heaps = (
            b'  123456  23456  100000  0  0  0  0 Kmem\r\n'
            b'  654321  54321  600000  0  0  0  0 Umem\r\n',
            b'  123456  23456  100000  0  0  0  0 Kmem\r\n'
            b'  654321  54321  600000  0  0  0  0 Umem\r\n',
        )

        def wait_stage(actual_console, actual_pending, marker, password, timeout, stage):
            self.assertIs(actual_console, console)
            self.assertIs(actual_pending, pending)
            seen.append((marker, stage))
            if marker == b'nsh> ':
                return b'nsh> '
            if marker == b'NXRS_STREAM_HEAP_0_STATUS_0':
                return heaps[0] + b'NXRS_STREAM_HEAP_0_STATUS_0'
            if marker == b'NXRS_STREAM_HEAP_1_STATUS_0':
                return heaps[1] + b'NXRS_STREAM_HEAP_1_STATUS_0'
            if marker == b'NXRS_STREAM_UPTIME_STATUS_0':
                return b'123.45\r\nNXRS_STREAM_UPTIME_STATUS_0'
            self.fail(f'unexpected status marker: {marker!r}')

        serial_factory = Mock()
        with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=serial_factory)}), \
             patch.object(RUN, 'wait_stage', side_effect=wait_stage):
            result = RUN.read_stopped_resources(console, pending, b'secret')
        serial_factory.assert_not_called()

        self.assertEqual(result['heap_samples'], [
            {'Kmem': {'total': 123456, 'used': 23456, 'free': 100000},
             'Umem': {'total': 654321, 'used': 54321, 'free': 600000}},
            {'Kmem': {'total': 123456, 'used': 23456, 'free': 100000},
             'Umem': {'total': 654321, 'used': 54321, 'free': 600000}},
        ])
        self.assertEqual(result['uptime_seconds'], 123.45)
        self.assertEqual([call.args[0] for call in console.write.call_args_list], [
            b'free\necho NXRS_STREAM_HEAP_0_STATUS_$?\n',
            b'free\necho NXRS_STREAM_HEAP_1_STATUS_$?\n',
            b'cat /proc/uptime\necho NXRS_STREAM_UPTIME_STATUS_$?\n',
        ])
        self.assertEqual([marker for marker, stage in seen if stage == 'post-stop resources'], [
            b'NXRS_STREAM_HEAP_0_STATUS_0', b'NXRS_STREAM_HEAP_1_STATUS_0',
            b'NXRS_STREAM_UPTIME_STATUS_0',
        ])
        self.assertEqual(sum(stage == 'post-stop prompt' for _, stage in seen), 3)

    def test_stopped_resource_reads_reject_bad_status_and_uptime(self):
        for malformed_status in (True, False):
            with self.subTest(malformed_status=malformed_status):
                console, pending = Mock(), bytearray()

                def wait_stage(actual_console, actual_pending, marker, password, timeout, stage):
                    self.assertIs(actual_console, console)
                    if marker == b'nsh> ':
                        return b'nsh> '
                    heap = (b'1 0 1 0 0 0 0 Kmem\n'
                            b'1 0 1 0 0 0 0 Umem\n')
                    if marker.endswith(b'UPTIME_STATUS_0'):
                        uptime = b'not uptime\r\n' if not malformed_status else b'12.34\r\n'
                        return uptime + marker
                    status = b'NXRS_STREAM_HEAP_0_STATUS_1' if malformed_status and marker.endswith(b'HEAP_0_STATUS_0') else marker
                    return heap + status

                with patch.object(RUN, 'wait_stage', side_effect=wait_stage), \
                     self.assertRaises(RuntimeError if malformed_status else ValueError):
                    RUN.read_stopped_resources(console, pending, b'')


class ReportAndCleanupTests(unittest.TestCase):
    @unittest.skipIf(Image is None, 'install stream/requirements.txt for qualification controls')
    def test_continuity_timestamp_follows_last_decode_and_join_but_precedes_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wifi = root / 'wifi'
            wifi.write_bytes(b'SSID=lab\npassword=secret123\n')
            wifi.chmod(0o600)
            args = SimpleNamespace(host_ip='192.0.2.1', seconds=10, reconnect_at=0,
                                   slow_reader=False, port='not-a-real-port', wifi_file=wifi,
                                   output=root / 'first.jpg', report=root / 'report.json')
            console = MagicMock()
            console.read.return_value = b'NXRS_STREAM_PASS stopped=1 joined=1\n'
            now = [100.0]

            def seed(client):
                client.started_ms = now[0] * 1000
                client._record_frame(1, 0, now[0] * 1000, b'parser-control')

            def join(client, timeout):
                if client.frames == 1:
                    now[0] += 0.05
                    client._record_frame(2, 1, now[0] * 1000, b'parser-control')

            def audit(*args):
                now[0] += 20  # Serial auditing is not part of delivery timing.
                return {'heap_samples': [], 'uptime_seconds': 30}

            with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=lambda: console)}), \
                 patch.object(RUN.time, 'monotonic', side_effect=lambda: now[0]), \
                 patch.object(RUN, 'wait_stage', side_effect=[b'', b'',
                     b'NXRS_STREAM_ONLINE ip=192.0.2.2 port=8080 ms=0\n']), \
                 patch.object(RUN.WIFI, 'read_until', side_effect=[b'NXRS_STREAM_STATUS_0', b'nsh> ']), \
                 patch.object(RUN, 'read_stopped_resources', side_effect=audit), \
                 patch.object(RUN.StreamClient, 'start', autospec=True, side_effect=seed), \
                 patch.object(RUN.StreamClient, 'join', autospec=True, side_effect=join), \
                 patch.object(RUN, 'validate_delivery', wraps=RUN.validate_delivery) as validate:
                RUN.qualify(args)
                self.assertAlmostEqual(validate.call_args.args[1], 100050)
                self.assertGreaterEqual(validate.call_args.args[1],
                                        validate.call_args.args[0]._last_frame_host_ms)
            report = json.loads(args.report.read_text())
            self.assertEqual(report['result'], 'PASS')
            self.assertEqual(report['delivery_continuity']['terminal_gap_ms'], 0)

    def test_live_stall_reports_only_numeric_edges_after_ten_seconds(self):
        client = SimpleNamespace(frames=0, _last_frame_host_ms=None)
        with patch('builtins.print') as output:
            state = RUN.report_live_stall(client, 30000, None)
            output.assert_not_called()
            client.frames, client._last_frame_host_ms = 1, 0
            state = RUN.report_live_stall(client, 10000, state)
            output.assert_not_called()
            state = RUN.report_live_stall(client, 10001, state)
            self.assertEqual(state, 1)
            output.assert_called_once_with('stall decoded=1 frame_age_ms=10001', flush=True)
            self.assertEqual(RUN.report_live_stall(client, 11000, state), state)
            self.assertEqual(output.call_count, 1)
            client.frames, client._last_frame_host_ms = 2, 11010
            self.assertIsNone(RUN.report_live_stall(client, 11020, state))
            self.assertEqual(output.call_count, 2)
            output.assert_called_with('recovered decoded=2', flush=True)

    @unittest.skipIf(Image is None, 'install stream/requirements.txt for qualification controls')
    def test_success_requires_same_connection_resources_and_sufficient_uptime(self):
        for uptime in (9.0, 30.0):
            with self.subTest(uptime=uptime), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                wifi = root / 'wifi'
                wifi.write_bytes(b'SSID=lab\npassword=secret123\n')
                wifi.chmod(0o600)
                args = SimpleNamespace(host_ip='192.0.2.1', seconds=10, reconnect_at=0,
                                       slow_reader=False, port='not-a-real-port', wifi_file=wifi,
                                       output=root / 'first.jpg', report=root / 'report.json')
                console = MagicMock()
                console.read.return_value = b'NXRS_STREAM_PASS stopped=1 joined=1\n'
                resources = {'heap_samples': [], 'uptime_seconds': uptime}

                def seed(client):
                    now = time.monotonic() * 1000
                    client.started_ms = now
                    client._record_frame(1, 100, now, b'parser-control')

                with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=lambda: console)}), \
                     patch.object(RUN, 'wait_stage', side_effect=[b'', b'',
                         b'NXRS_STREAM_ONLINE ip=192.0.2.2 port=8080 ms=0\n']), \
                     patch.object(RUN.WIFI, 'read_until', side_effect=[b'NXRS_STREAM_STATUS_0', b'nsh> ']), \
                     patch.object(RUN, 'read_stopped_resources', return_value=resources) as observe, \
                     patch.object(RUN.StreamClient, 'start', autospec=True, side_effect=seed), \
                     patch.object(RUN.StreamClient, 'join', autospec=True):
                    if uptime < args.seconds:
                        with self.assertRaisesRegex(RuntimeError, 'uptime is shorter'):
                            RUN.qualify(args)
                    else:
                        RUN.qualify(args)
                    observe.assert_called_once()
                    self.assertIs(observe.call_args[0][0], console)
                report = json.loads(args.report.read_text())
                self.assertEqual(report['result'], 'FAIL' if uptime < args.seconds else 'PASS')
                if uptime >= args.seconds:
                    self.assertEqual(report['post_stop_resources'], resources)
                console.close.assert_called()

    @unittest.skipIf(Image is None, 'install stream/requirements.txt for qualification controls')
    def test_invalid_resolution_or_quality_does_not_open_the_device(self):
        factory = Mock()
        with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=factory)}):
            for width, height, quality in ((320, 240, 0), (640, 480, 101), (640, 240, 81)):
                with self.subTest(width=width, height=height, quality=quality):
                    args = SimpleNamespace(host_ip='192.0.2.1', width=width, height=height, quality=quality)
                    with self.assertRaises(ValueError):
                        RUN.qualify(args)
            factory.assert_not_called()

    def test_no_client_has_no_timing_measurements_not_zero_latency(self):
        snapshot = RUN.latency_snapshot(None)
        latency = snapshot['capture_to_host_decode_latency_ms']
        self.assertEqual(latency['samples'], 0)
        self.assertEqual(latency['unmeasured_frames'], 0)
        self.assertFalse(latency['histogram_counts'])
        for field in ('mean', 'max'):
            self.assertIsNone(latency[field])
        for field in ('clock_offset_device_minus_host_ms', 'clock_uncertainty_ms',
                      'max_clock_uncertainty_ms'):
            self.assertIsNone(snapshot[field])

    @unittest.skipIf(Image is None, 'install stream/requirements.txt for qualification controls')
    def test_fail_report_preserves_measured_and_unmeasured_frames(self):
        for calibrated in (False, True):
            with self.subTest(calibrated=calibrated), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                wifi = root / 'wifi'
                wifi.write_bytes(b'SSID=lab\npassword=secret123\n')
                wifi.chmod(0o600)
                args = SimpleNamespace(host_ip='192.0.2.1', seconds=10, reconnect_at=0,
                                       slow_reader=False, port='not-a-real-port', wifi_file=wifi,
                                       output=root / 'first.jpg', report=root / 'report.json')
                console = MagicMock()
                console.read.return_value = b'NXRS_STREAM_FAIL error=controlled failure\n'

                def seed_frames(client):
                    client.started_ms = 0
                    client._record_frame(1, 100, 105, b'parser-control')
                    if calibrated:
                        client.clock_offset_ms = 5
                        client.clock_uncertainty_ms = client.max_clock_uncertainty_ms = 1
                        client._record_frame(2, 200, 205, b'parser-control')

                with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=lambda: console)}), \
                     patch.object(RUN, 'wait_stage', side_effect=[b'', b'',
                         b'NXRS_STREAM_ONLINE ip=192.0.2.2 port=8080 ms=0\n']), \
                     patch.object(RUN.StreamClient, 'start', autospec=True, side_effect=seed_frames), \
                     patch.object(RUN.StreamClient, 'join', autospec=True) as join:
                    with self.assertRaisesRegex(RuntimeError, 'controlled failure'):
                        RUN.qualify(args)
                    join.assert_called_once()
                    self.assertEqual(RUN.StreamClient.start.call_args[0][0].duration,
                                     args.seconds + RUN.COMPLETION_GRACE_SECONDS)
                report = json.loads(args.report.read_text())
                self.assertEqual(report['result'], 'FAIL')
                self.assertEqual((report['width'], report['height'], report['jpeg_quality']), (320, 240, 81))
                latency = report['capture_to_host_decode_latency_ms']
                self.assertEqual(latency['samples'], int(calibrated))
                self.assertEqual(latency['unmeasured_frames'], 1)
                self.assertEqual(report['frames_decoded'], 1 + int(calibrated))
                self.assertEqual(latency['histogram_upper_bounds'],
                                 list(RUN.StreamClient.LATENCY_BOUNDS_MS) + [None])
                self.assertEqual(latency['histogram_counts'], {'10': 1} if calibrated else {})
                self.assertEqual(latency['mean'], 10 if calibrated else None)
                self.assertEqual(latency['max'], 10 if calibrated else None)
                self.assertEqual(report['max_clock_uncertainty_ms'], 1 if calibrated else None)
                self.assertNotIn('secret123', args.report.read_text())
                self.assertEqual(args.report.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(Image is None, 'install stream/requirements.txt for qualification controls')
    def test_failed_continuity_keeps_post_stop_resources_and_bounded_link_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wifi = root / 'wifi'
            wifi.write_bytes(b'SSID=lab\npassword=secret123\n')
            wifi.chmod(0o600)
            args = SimpleNamespace(host_ip='192.0.2.1', seconds=10, reconnect_at=0,
                                   slow_reader=False, port='not-a-real-port', wifi_file=wifi,
                                   output=root / 'first.jpg', report=root / 'report.json')
            console = MagicMock()
            events = b''.join(
                (f'NXRS_STREAM_OFFLINE ms={index}\n' if index % 2 else
                 f'NXRS_STREAM_ONLINE ip=192.0.2.2 port=8080 ms={index}\n').encode()
                for index in range(1, 21))
            console.read.return_value = events + b'NXRS_STREAM_PASS stopped=1 joined=1\n'
            resources = {'heap_samples': [{'Kmem': {'total': 100, 'used': 40, 'free': 60}}],
                         'uptime_seconds': 42.0}
            order = []

            def seed(client):
                client.started_ms = 0
                client._record_frame(1, 1000, 1000, b'parser-control')
                client._record_frame(2, 13000, 13000, b'parser-control')

            def read_resources(actual_console, pending, password):
                order.append('resources')
                self.assertIs(actual_console, console)
                return resources

            def read_status(actual_console, actual_pending, marker, password, timeout):
                order.append('status')
                return b'NXRS_STREAM_STATUS_0' if marker == b'NXRS_STREAM_STATUS_0' else b'nsh> '

            def reject_delivery(client, finished_ms):
                order.append('continuity')
                self.assertGreaterEqual(finished_ms, client._last_frame_host_ms)
                raise RuntimeError('maximum inter-frame gap exceeded')

            def stop(client):
                order.append('stop')

            def join(client, timeout):
                order.append('join')

            with patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=lambda: console)}), \
                 patch.object(RUN, 'wait_stage', side_effect=[b'', b'',
                     b'NXRS_STREAM_ONLINE ip=192.0.2.2 port=8080 ms=0\n']), \
                 patch.object(RUN.WIFI, 'read_until', side_effect=read_status) as read_until, \
                 patch.object(RUN, 'read_stopped_resources', side_effect=read_resources), \
                 patch.object(RUN.StreamClient, 'start', autospec=True, side_effect=seed), \
                 patch.object(RUN.StreamClient, 'stop', autospec=True, side_effect=stop), \
                 patch.object(RUN.StreamClient, 'join', autospec=True, side_effect=join), \
                 patch.object(RUN, 'validate_delivery', side_effect=reject_delivery):
                with self.assertRaisesRegex(RuntimeError, 'maximum inter-frame gap'):
                    RUN.qualify(args)

            report = json.loads(args.report.read_text())
            self.assertEqual(order, ['stop', 'join', 'status', 'status', 'resources',
                                     'continuity', 'stop', 'join'])
            self.assertEqual(report['result'], 'FAIL')
            self.assertEqual(report['post_stop_resources'], resources)
            self.assertEqual(report['delivery_continuity']['max_inter_frame_gap_ms'], 12000)
            self.assertEqual(len(report['link_events']), 16)
            self.assertTrue(all(isinstance(event['ms'], int) and isinstance(event['online'], bool)
                                for event in report['link_events']))
            self.assertEqual([call.args[2] for call in read_until.call_args_list][:2], [
                b'NXRS_STREAM_STATUS_0', b'nsh> '])
            console.write.assert_any_call(b'echo NXRS_STREAM_STATUS_$?\n')
            self.assertNotIn('secret123', args.report.read_text())
            console.close.assert_called_once()

    def test_serial_always_closes_and_worker_shutdown_failures_are_visible(self):
        for failure in ('alive', 'join', 'stop'):
            with self.subTest(failure=failure):
                client, console = Mock(), Mock()
                client.alive.return_value = failure == 'alive'
                if failure in ('join', 'stop'):
                    getattr(client, failure).side_effect = RuntimeError('controlled shutdown failure')
                with self.assertRaises(RuntimeError):
                    RUN.close_runner(client, console)
                console.close.assert_called_once()
        client, console = Mock(), Mock()
        client.alive.return_value = False
        RUN.close_runner(client, console)
        client.stop.assert_called_once()
        client.join.assert_called_once_with(RUN.IO_TIMEOUT + 1)
        console.close.assert_called_once()
        console.reset_mock()
        RUN.close_runner(None, console)
        console.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
