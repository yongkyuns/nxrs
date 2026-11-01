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

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SessionTests(unittest.TestCase):
    def test_autostart_sends_only_credentials_after_the_ready_marker(self):
        console = Console([b''], initial=b'NXRS_WIFI_CREDENTIALS_READY')
        SERVE.Session(console, CREDENTIALS, None, Mock(), tls=True).start(autostart=True)
        self.assertEqual(console.writes, [bytes((3, 9)) + b'labsecret123'])

    def test_saved_profile_sends_nothing_and_preserves_the_tls_reader_boundary(self):
        data = bytearray(b'NXRS_WIFI_CREDENTIALS_SAVED\nNXRS_TLS_CSR bytes=1\nx')
        console = Console([])
        def read(size):
            chunk = bytes(data[:size])
            del data[:size]
            return chunk
        console.read = read
        SERVE.Session(console, CREDENTIALS, None, Mock(), tls=True).start(autostart=True)
        self.assertEqual(console.writes, [])
        self.assertEqual(data, b'\nNXRS_TLS_CSR bytes=1\nx')

    def test_tls_trial_confirmation_reenrolls_but_other_exits_fail(self):
        marker = 'NXRS_STARTUP_CONFIRMED joined=1 resumed=1 watchdog_stopped=1'
        reboot = 'NXRS_UPDATE_REBOOT staged=1 joined=1 pending=1'
        for events, failure, stop_at, accepted, firmware in (
                ([marker], None, None, True, False), ([marker], None, 'confirmation', True, False),
                ([], None, 'shutdown', True, False), ([], None, None, False, False),
                ([marker], 'device failure', None, False, False),
                ([reboot], None, None, True, True), ([], None, None, False, True),
                ([reboot], None, 'cleanup', False, True),
                ([reboot], 'device failure', None, False, True)):
            with self.subTest(events=events, failure=failure, stop_at=stop_at), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'config.json'
                config.write_text(json.dumps(dict(enrollment_directory=str(root / 'enrollment'),
                    ingest_program=sys.executable, ingest=dict(camera='192.168.1.20:8443',
                    allow_insecure_camera_http=False, camera_tls=dict(site_id='s', camera_id='c', gateway_id='g')))))
                config.chmod(0o600)
                stop, emitted, readers = threading.Event(), [], []
                console = Console([b'', b''], initial=b'NXRS_WIFI_CREDENTIALS_READY')
                def supervisor(*args, **kwargs):
                    first = not readers
                    reader = Mock(failure=failure if first else None,
                                  events=list(events) if first else [])
                    reader.ready.wait.return_value = True
                    reader.finished.is_set.return_value = first and stop_at != 'shutdown'
                    reader.credentials.config.return_value = {}
                    reader.shutdown.side_effect = lambda **kw: reader.events.append(
                        marker if first and stop_at == 'shutdown' else 'NXRS_TLS_CAMERA_PASS stopped=1 joined=1')
                    readers.append(reader)
                    if len(readers) == 2 or stop_at in ('shutdown', 'cleanup'):
                        stop.set()
                    return reader
                def emit(event, **kwargs):
                    emitted.append(event)
                    if event == 'firmware_reboot':
                        console.chunks.append(b'NXRS_WIFI_CREDENTIALS_READY')
                    if event == 'startup_confirmed' and stop_at == 'confirmation':
                        stop.set()
                tls = SimpleNamespace(Supervisor=supervisor, INIT_TIMEOUT=1,
                                      private=lambda path, value: path.write_text(value))
                child = Mock()
                child.poll.side_effect = lambda: 0 if firmware and len(readers) == 1 and stop_at != 'cleanup' else None
                if stop_at == 'cleanup':
                    child.wait.side_effect = [SERVE.subprocess.TimeoutExpired('ingest', 10), 0]
                with patch.object(SERVE, 'identity', return_value=(0x303a, 0x1001, 'camera')), \
                     patch.dict(sys.modules, serial=SimpleNamespace(Serial=Mock(return_value=console))), \
                     patch.object(SERVE.importlib.util, 'spec_from_file_location'), \
                     patch.object(SERVE.importlib.util, 'module_from_spec', return_value=tls), \
                     patch.object(SERVE.subprocess, 'run'), \
                     patch.object(SERVE.subprocess, 'Popen', return_value=child):
                    def run():
                        SERVE.supervise_tls('port', CREDENTIALS, config,
                            emit, stop, autostart=True, firmware='candidate.bin' if firmware else None)
                    if accepted:
                        run()
                        self.assertEqual(len(readers), 2)
                        self.assertIn('firmware_reboot' if firmware else 'startup_confirmed', emitted)
                        readers[-1].shutdown.assert_called_once_with(renew=False)
                        credential_frame = bytes((3, 9)) + b'labsecret123'
                        self.assertEqual(console.writes, [credential_frame, b'NXRB', credential_frame]
                                         if firmware else [credential_frame])
                    else:
                        with self.assertRaises(RuntimeError):
                            run()
                        self.assertNotIn(b'NXRB', console.writes)
                        self.assertNotIn('startup_confirmed', emitted)

    def test_auxiliary_metrics_cannot_renew_stream_health(self):
        row = b'NXRS_STREAM_STACK budget=32480 used=14784'
        parsed = SERVE.RUN.owner_stack(row)
        self.assertEqual(parsed, dict(budget_bytes=32480, used_bytes=14784))
        for bad in (row + b' extra=1', row.replace(b'used=14784', b'used=32481'),
                    row.replace(b'used=14784', b'used=0'), row.replace(b'budget=32480', b'budget=1048577')):
            with self.assertRaises(ValueError):
                SERVE.RUN.owner_stack(bad)
        with tempfile.TemporaryDirectory() as directory:
            state = SERVE.State(Path(directory) / 'private-state.json')
            state.emit('online')
            updated = state.data['updated_unix']
            state.emit('owner_stack', stack=parsed)
            self.assertEqual(state.data['status'], 'online')
            self.assertEqual(state.data['updated_unix'], updated)
            self.assertEqual(state.data['owner_stack'], parsed)
            state.emit('tls', tls=dict(handshakes=1, failures=0))
            self.assertEqual(state.data['status'], 'online')
            self.assertEqual(state.data['updated_unix'], updated)
            self.assertEqual(state.data['tls']['handshakes'], 1)

    def test_stage_profile_telemetry_rejects_inconsistent_or_unbounded_layouts(self):
        row = b'NXRS_DETECTION_PROFILE decode_us=1000 resize_us=2000 kernel_us=3000 postprocess_us=0 total_us=6000'
        self.assertEqual(SERVE.RUN.detection_profile(row)['total_us'], 6000)
        for bad in (row + b' extra=1', row.replace(b'total_us=6000', b'total_us=5999'),
                    row.replace(b'kernel_us=3000', b'kernel_us=0')):
            with self.assertRaises(ValueError):
                SERVE.RUN.detection_profile(bad)

    def test_cpu_observations_validate_wall_clock_and_frequency(self):
        row = b'NXRS_DETECTION_CPU wall_us=10000 cpu_us=5000 frequency_hz=240000000'
        self.assertEqual(SERVE.RUN.detection_cpu(row)['cpu_us'], 5000)
        for bad in (row.replace(b'cpu_us=5000', b'cpu_us=13000'),
                    row.replace(b'frequency_hz=240000000', b'frequency_hz=0'),
                    row + b' extra=1'):
            with self.assertRaises(ValueError):
                SERVE.RUN.detection_cpu(bad)

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
    def test_usb_reports_and_owner_events_keep_one_consistent_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SERVE.State(Path(directory) / 'state.json')
            errors = []
            def emit(event, fields):
                try:
                    for _ in range(10):
                        state.emit(event, **fields)
                        json.loads(state.path.read_text())
                except Exception as error:
                    errors.append(error)
            with patch('builtins.print'):
                usb = threading.Thread(target=emit, args=('tls', dict(tls=dict(handshakes=1))))
                owner = threading.Thread(target=emit, args=('renewing', {}))
                usb.start(); owner.start()
                usb.join(); owner.join()
            self.assertEqual(errors, [])
            saved = json.loads(state.path.read_text())
            self.assertEqual(saved['renewals'], 10)
            self.assertEqual(saved['tls']['handshakes'], 1)
            self.assertEqual(os.stat(state.path).st_mode & 0o777, 0o600)

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
