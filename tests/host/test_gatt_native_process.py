"""Native GATT process transport tests, using short owned Python children only."""
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / 'nuttx-std/ble/native.py'
SPEC = importlib.util.spec_from_file_location('gatt_native_process', PATH)
NATIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE)


class Records:
    def __init__(self):
        self.lines = []
        self.finished = False

    def observe(self, line):
        self.lines.append(line)

    def finish(self):
        if self.lines != [b'expected']:
            raise NATIVE.RunError('incomplete records')
        self.finished = True


@unittest.skipUnless(os.name == 'posix', 'Unix process groups and pipe selectors')
class NativeGattProcessTests(unittest.TestCase):
    def test_exiting_darwin_group_permission_race_is_reaped_not_assumed_stopped(self):
        exiting = False
        reaped = False

        class Process:
            pid = 1234  # No real process is signalled: killpg is injected below.

            def poll(self):
                nonlocal reaped
                reaped = exiting
                return 7 if reaped else None

            def wait(self, timeout):
                self.poll()
                return 7

        def killpg(group, value):
            nonlocal exiting
            self.assertEqual(group, 1234)
            if reaped:
                raise ProcessLookupError()
            if value == NATIVE.signal.SIGTERM:
                exiting = True
                raise PermissionError()

        with mock.patch.object(NATIVE.os, 'killpg', side_effect=killpg):
            NATIVE._stop(Process())
        self.assertTrue(exiting)
        self.assertTrue(reaped)

    def test_permission_denied_without_exit_is_not_a_quiet_process_group(self):
        process = mock.Mock(pid=1234)
        process.poll.return_value = None
        process.wait.side_effect = NATIVE.subprocess.TimeoutExpired('owned child', 2)
        with mock.patch.object(NATIVE.os, 'killpg', side_effect=PermissionError()), \
                mock.patch.object(NATIVE.time, 'monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(NATIVE.RunError, 'group did not stop'):
                NATIVE._stop(process)

    def client(self, code):
        client = NATIVE.ProcessClient([sys.executable, '-c', code])
        self.addCleanup(client.close)
        return client

    def test_exact_records_eof_and_actual_zero_exit_are_all_required(self):
        client = self.client('print("expected")')
        for _ in range(2):
            records = Records()
            client.exchange('full', records, timeout=10)
            self.assertTrue(records.finished)
            self.assertIsNone(client._process)
        for code, expected in [
            ('print("expected"); raise SystemExit(7)', 'exited nonzero'),
            ('print("unrelated")', 'incomplete records'),
            ('import sys; sys.stdout.write("expected")', 'ended mid-line'),
        ]:
            records = Records()
            client = self.client(code)
            with self.subTest(code=code), self.assertRaisesRegex(NATIVE.RunError, expected):
                client.exchange('full', records, timeout=10)
            self.assertFalse(records.finished)
            self.assertIsNone(client._process)

    def test_output_and_line_limits_reject_unbounded_or_partial_output(self):
        for code, expected in [
            ('print("x"*8193)', 'line exceeded limit'),
            ('import sys; sys.stdout.write("x"*8193); sys.stdout.flush()', 'line exceeded limit'),
            ('print("x\\n"*70000)', 'output exceeded limit'),
        ]:
            client = self.client(code)
            with self.subTest(expected=expected), self.assertRaisesRegex(NATIVE.RunError, expected):
                client.exchange('full', Records(), timeout=10)
            self.assertIsNone(client._process)

    def test_timeout_reaps_child_even_after_a_plausible_pass_record(self):
        client = self.client('import time; print("expected", flush=True); time.sleep(60)')
        records = Records()
        with self.assertRaisesRegex(NATIVE.RunError, 'timed out'):
            client.exchange('full', records, timeout=0.2)
        self.assertFalse(records.finished)
        self.assertIsNone(client._process)

    def test_close_before_start_does_not_spawn_and_errors_are_identity_free(self):
        client = self.client('raise SystemExit(91)')
        client.close()
        with self.assertRaisesRegex(NATIVE.RunError, 'closed'):
            client.exchange('full', Records(), timeout=10)
        missing = NATIVE.ProcessClient(['/nonexistent/private-device-identifier'])
        with self.assertRaises(NATIVE.RunError) as error:
            missing.exchange('full', Records(), timeout=10)
        self.assertNotIn('private-device-identifier', str(error.exception))
        self.assertIsNone(missing._process)

    def test_concurrent_close_stops_the_owned_child_and_reader(self):
        client = self.client('import time; time.sleep(60)')
        errors = []

        def run():
            try:
                client.exchange('full', Records(), timeout=10)
            except NATIVE.RunError as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while client._process is None and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(client._process)
            client.close()
        finally:
            client.close()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)
        self.assertIsNone(client._process)


if __name__ == '__main__':
    unittest.main()
