"""Session lifecycle/fault controls; these do not establish hardware stability."""
import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("camera_session", ROOT / "tests/nuttx-std/stream/serve.py")
SERVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVE)

ONLINE = b"NXRS_STREAM_ONLINE ip=192.168.1.20 port=8080 ms=3000\n"
REPORT = (b"NXRS_STREAM_REPORT ms=10000 captured=178 delivered=175 bytes=1000000 "
          b"max_bytes=10000 heap_user=821896 heap_kernel=60000 cpu_busy_pm=540 "
          b"owner_cpu_ms=3700 online=1 attempts=1 rejoined=0 errors=0 camera_discarded=0\n")
PASS = b"NXRS_STREAM_PASS stopped=1 joined=1\nnsh> "
CREDENTIALS = (b"lab", b"secret123")


class Console:
    def __init__(self, responses=(), initial=b"nsh> "):
        self.chunks = [initial]
        self.responses = iter(responses)
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        if data.startswith(b"stream_test "):
            self.chunks.append(b"NXRS_WIFI_CREDENTIALS_READY")
        elif data == bytes((3, 9)) + b"labsecret123":
            self.chunks.append(next(self.responses))
        elif data == b"stop\n":
            self.chunks.append(PASS)
        return len(data)

    def read(self, _):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class SessionTests(unittest.TestCase):
    def run_session(self, console, cycles=1, stop=None, clock=lambda: 0):
        events = []
        session = SERVE.Session(console, CREDENTIALS, 20,
                                lambda event, **kw: events.append((event, kw)), stop, clock)
        completed = session.run(cycles)
        return completed, events

    def test_three_clean_cycles_renew_without_closing_the_console(self):
        console = Console([ONLINE + REPORT + PASS] * 3)
        completed, events = self.run_session(console, 3)
        self.assertEqual(completed, 3)
        self.assertEqual(sum(e == "renewing" for e, _ in events), 2)
        self.assertEqual(sum(w.startswith(b"stream_test ") for w in console.writes), 3)
        self.assertFalse(console.closed)

    def test_detection_is_typed_local_telemetry_without_extending_stream_health(self):
        detection = (b'NXRS_DETECTION_REPORT ms=10000 state=Person submitted=8 completed=7 failures=0 '
                     b'inference_us=7000000 max_inference_us=1100000 events=1 sequence=100 age_ms=1200 score=900\n')
        _, events = self.run_session(Console([ONLINE + REPORT + detection + PASS]))
        parsed = next(fields['detection'] for event, fields in events if event == 'detection')
        self.assertEqual((parsed['state'], parsed['sequence']), ('Person', 100))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private-state.json'
            state = SERVE.State(path)
            state.emit('online')
            state.emit('detection', detection=parsed)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['status'], 'online')
            self.assertEqual(saved['detection']['state'], 'Person')
            state.emit('usb_disconnected')
            saved = json.loads(path.read_text())
            self.assertEqual((saved['detection']['state'], saved['detection']['sequence']), ('Unavailable', 0))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_reboot_prompt_restarts_only_after_shell_is_ready(self):
        console = Console([ONLINE + b"*** Booting NuttX ***\nnsh> ", ONLINE + PASS])
        completed, events = self.run_session(console)
        self.assertEqual(completed, 1)
        self.assertIn("boot_observed", [e for e, _ in events])
        self.assertIn("unexpected_shell", [e for e, _ in events])
        self.assertEqual(sum(w.startswith(b"stream_test ") for w in console.writes), 2)

    def test_reconnect_adopts_a_running_command_without_sending_credentials(self):
        console = Console(initial=ONLINE + REPORT + PASS)
        completed, _ = self.run_session(console)
        self.assertEqual(completed, 1)
        self.assertEqual(console.writes, [b"\n"])

    def test_panic_or_explicit_failure_stops_instead_of_hiding_it(self):
        for failure in (b"PANIC at chip.c:1", b"NXRS_STREAM_FAIL error=camera stalled"):
            console = Console([failure + b"\nnsh> "])
            with self.subTest(failure=failure), self.assertRaisesRegex(RuntimeError, "firmware failure"):
                self.run_session(console)
            self.assertEqual(sum(w.startswith(b"stream_test ") for w in console.writes), 1)

    def test_credential_echo_split_across_reads_is_withheld(self):
        console = Console(initial=b"secr")
        console.chunks.append(b"et123")
        with self.assertRaisesRegex(RuntimeError, "output withheld") as caught:
            self.run_session(console)
        self.assertNotIn("secret123", str(caught.exception))

    def test_partial_console_output_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError, "exceeded bound"):
            self.run_session(Console(initial=b"x" * 9000))

    def test_stop_drains_clean_shutdown_without_starting_another_cycle(self):
        stop = threading.Event()
        console = Console(initial=ONLINE + REPORT + PASS)
        stop.set()
        completed, events = self.run_session(console, cycles=0, stop=stop)
        self.assertEqual(completed, 1)
        self.assertNotIn("starting", [e for e, _ in events])

    def test_continuous_stop_uses_command_then_requires_quiescent_pass(self):
        stop = threading.Event()
        console = Console([ONLINE])
        events = []
        def emit(event, **fields):
            events.append(event)
            if event == "online":
                stop.set()
        completed = SERVE.Session(console, CREDENTIALS, None, emit, stop).run()
        self.assertEqual(completed, 1)
        self.assertIn(b"stream_test continuous 0 320 240 81\n", console.writes)
        self.assertEqual(console.writes[-1], b"stop\n")
        self.assertIn("clean_shutdown", events)

    def test_usb_disconnect_recovers_only_to_the_same_device(self):
        class SerialFault(OSError):
            pass
        broken = Console()
        def failed_read(_):
            raise SerialFault("transport gone")
        broken.read = failed_read
        broken.open = lambda: None
        recovered = Console([ONLINE + PASS])
        recovered.open = lambda: None
        events = []
        class FastStop(threading.Event):
            def wait(self, timeout=None):
                return self.is_set()
        with patch.object(SERVE, "identity", return_value=(0x303A, 0x1001, "stable")), \
                patch.dict(sys.modules, serial=SimpleNamespace(
                    Serial=Mock(side_effect=[broken, recovered]), SerialException=SerialFault)):
            SERVE.supervise("explicit-port", CREDENTIALS, 20, 1,
                            lambda event, **kw: events.append(event), FastStop())
        self.assertIn("usb_disconnected", events)
        self.assertIn("usb_reconnected", events)
        self.assertIn("clean_shutdown", events)
        self.assertNotIn(bytes((3, 9)) + b"labsecret123", broken.writes)

    def test_stalled_console_has_a_deadline_and_no_hardware_reset(self):
        ticks = iter((0, 61))
        console = Console(initial=b"")
        with self.assertRaisesRegex(RuntimeError, "stalled"):
            self.run_session(console, clock=lambda: next(ticks))
        self.assertEqual(console.writes, [b"\n"])

    def test_wrong_usb_identity_is_rejected_before_any_write(self):
        expected = (0x303A, 0x1001, "board-one")
        replacement = (0x303A, 0x1001, "board-two")
        console = Console()
        with patch.object(SERVE, "identity", side_effect=[expected, replacement]), \
                patch.dict(sys.modules, serial=SimpleNamespace(
                    Serial=Mock(return_value=console), SerialException=OSError)), \
                self.assertRaisesRegex(RuntimeError, "identity changed"):
            SERVE.supervise("explicit-port", CREDENTIALS, 20, 1, lambda *a, **k: None, threading.Event())
        self.assertEqual(console.writes, [])


class StateTests(unittest.TestCase):
    def test_private_atomic_status_and_restart_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = SERVE.State(path)
            with patch("builtins.print"):
                state.emit("renewing")
                state.emit("usb_reconnected")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            data = json.loads(path.read_text())
            self.assertEqual((data["renewals"], data["usb_reconnections"]), (1, 1))
            with self.assertRaises(FileExistsError):
                SERVE.State(path)


if __name__ == "__main__":
    unittest.main()
