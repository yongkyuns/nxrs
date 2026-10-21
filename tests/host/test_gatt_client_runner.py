"""Host-only protocol tests for the public two-board GATT runner."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import tempfile
import threading
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "nuttx-std/ble/client.py"
SPEC = importlib.util.spec_from_file_location("gatt_client_runner", MODULE_PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def client_transcript(case="full", phases=None, values=None, result=None, trailer=b"nsh> "):
    protocol = RUNNER._MODES[case]
    phases = protocol["phases"] if phases is None else phases
    values = protocol["values"] if values is None else values
    lines = []
    for phase in phases:
        lines.append(f"NXRS_GATT_CLIENT phase={phase}".encode())
        if phase == "discovered":
            lines.extend(values)
    lines.append((result or protocol["pass"]).encode())
    if trailer == b"nsh> ":
        trailer = b"nsh> echo $?\r\n0\r\nnsh> "
    return b"\r\n".join(lines) + b"\r\n" + trailer


class FakeConsole:
    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.writes = []
        self.closed = False

    def read(self, size):
        if self.closed:
            raise OSError("private-port-id")
        value = bytes(self.data[:size])
        del self.data[:size]
        return value

    def write(self, value):
        self.writes.append(value)
        return len(value)

    def close(self):
        self.closed = True


class GattClientRunnerTests(unittest.TestCase):
    def test_console_reader_strips_ansi_and_recognizes_unterminated_prompt(self):
        console = FakeConsole(b"\x1b[32mready\x1b[0m\r\nnsh> ")
        reader = RUNNER._ConsoleReader(console, time.monotonic() + 1, time.monotonic)
        self.assertEqual(reader.line_or_prompt(), (b"ready", False))
        self.assertEqual(reader.line_or_prompt(), (b"", True))

    def test_console_reader_bounds_raw_input_even_when_ansi_is_discarded(self):
        console = FakeConsole(b"\x1b[" + b"1" * 8192)
        reader = RUNNER._ConsoleReader(console, time.monotonic() + 1, time.monotonic)
        with self.assertRaisesRegex(RUNNER.ProtocolError, "line exceeded limit"):
            reader.line_or_prompt()

    def test_console_reader_deadline_and_read_errors_are_bounded_and_private(self):
        console = FakeConsole()
        reader = RUNNER._ConsoleReader(console, 1, lambda: 1)
        with self.assertRaisesRegex(RUNNER.ProtocolError, "console timed out"):
            reader.line_or_prompt()
        console.close()
        reader = RUNNER._ConsoleReader(console, 1, lambda: 0)
        with self.assertRaisesRegex(RUNNER.ProtocolError, "^console read failed$"):
            reader.line_or_prompt()

    def test_combined_image_entry_reuses_every_exact_case_oracle(self):
        for case in RUNNER._MODES:
            console = FakeConsole(client_transcript(case))
            def qualify(*args, **kwargs):
                self.assertEqual(args[:3], ("client", "peer", [case]))
                kwargs["exchange"](console, case, time.monotonic)
                return 0

            with self.subTest(case=case), mock.patch.object(RUNNER, "qualify", side_effect=qualify):
                self.assertEqual(RUNNER.main([
                    "--client-port", "client", "--peer-port", "peer",
                    "--client-app", "radio_test", "--case", case,
                ]), 0)
            suffix = "" if case == "full" else " " + case
            self.assertEqual(console.writes, [
                ("radio_test ble-client" + suffix + "\n").encode(), b"echo $?\n",
            ])

        # Selecting another entry cannot relax payload/cleanup/status acceptance.
        case = "full"
        for data in (client_transcript().replace(b"pairs=100", b"pairs=99"),
                     client_transcript().replace(b"cleanup_gatt=confirmed", b"cleanup_gatt=pending"),
                     client_transcript().replace(b"echo $?\r\n0", b"echo $?\r\n1")):
            console = FakeConsole(data)
            with mock.patch.object(RUNNER, "qualify", side_effect=qualify):
                with self.assertRaises(RuntimeError):
                    RUNNER.main(["--client-port", "client", "--peer-port", "peer",
                                 "--client-app", "radio_test", "--case", case])

    def test_client_app_requires_serial_and_does_not_change_default_entry(self):
        with mock.patch.object(RUNNER, "qualify") as qualify:
            for app in ("radio_test", "gatt_client", "invalid\ncommand"):
                with self.subTest(app=app), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        RUNNER.main(["--native-client", "/unused", "--peer-port", "peer",
                                     "--client-app", app])
                    self.assertEqual(error.exception.code, 2)
            qualify.assert_not_called()
            qualify.return_value = 0
            for app_args in ([], ["--client-app", "gatt_client"]):
                self.assertEqual(RUNNER.main([
                    "--client-port", "client", "--peer-port", "peer", *app_args,
                ]), 0)
                self.assertIsNone(qualify.call_args.kwargs["exchange"])

    def test_legacy_case_requires_its_own_result_and_keeps_the_full_peer_contract(self):
        legacy = RUNNER._MODES["legacy"]
        current = RUNNER._MODES["full"]
        self.assertEqual(legacy["client"], "gatt_client legacy\n")
        for key in ("peer", "peer_ready", "peer_done", "phases", "values"):
            self.assertEqual(legacy[key], current[key])
        self.run_client(client_transcript("legacy"), "legacy")
        with self.assertRaisesRegex(RuntimeError, "client result record invalid"):
            self.run_client(client_transcript("legacy", result=current["pass"]), "legacy")
        with self.assertRaisesRegex(RuntimeError, "client result record invalid"):
            self.run_client(client_transcript("full", result=legacy["pass"]), "full")

    def test_client_records_finish_requires_complete_sequence_and_rejects_duplicate_pass(self):
        protocol = RUNNER._MODES["full"]
        records = RUNNER._ClientRecords("full")
        for phase in protocol["phases"]:
            records.observe(f"NXRS_GATT_CLIENT phase={phase}".encode())
        records.observe(protocol["pass"].encode())
        records.finish()
        with self.assertRaisesRegex(RuntimeError, "result record invalid"):
            records.observe(protocol["pass"].encode())

        partial = RUNNER._ClientRecords("full")
        for phase in protocol["phases"][:-1]:
            partial.observe(f"NXRS_GATT_CLIENT phase={phase}".encode())
        with self.assertRaisesRegex(RuntimeError, "ended before exact pass"):
            partial.finish()

    def run_client(self, data, case="full"):
        console = FakeConsole(data)
        RUNNER._client_exchange(console, case)
        return console

    def test_exact_phase_order_and_status_are_required(self):
        for phases in (
            ("start", "scan_started", "scan_stopped", "connected", "disconnected", "discovered"),
            ("start", "scan_started", "scan_started", "scan_stopped", "connected", "discovered", "disconnected"),
        ):
            with self.subTest(phases=phases), self.assertRaises(RuntimeError):
                self.run_client(client_transcript(phases=phases))
        with mock.patch.object(RUNNER, "STATUS_LIMIT", 0.01):
            for status in (b"1", b"", b"0\r\n0"):
                trailer = b"nsh> echo $?\r\n" + status + b"\r\nnsh> "
                with self.subTest(status=status), self.assertRaises(RuntimeError):
                    self.run_client(client_transcript(trailer=trailer))

    def test_stale_prompt_does_not_complete_sync_before_fresh_marker(self):
        console = FakeConsole(b"banner\r\nnsh> nsh> echo NXRS_SYNC_fresh\r\nNXRS_SYNC_fresh\r\nnsh> ")
        RUNNER.sync_console(console, "client", token_factory=lambda: "fresh")
        self.assertEqual(console.writes, [b"\n\n\n", b"echo NXRS_SYNC_fresh\n"])

    def test_sync_rejects_command_echo_without_echo_output(self):
        console = FakeConsole(b"echo NXRS_SYNC_fresh\r\nnsh> ")
        with self.assertRaises(RuntimeError):
            RUNNER.sync_console(console, "client", timeout=0.01,
                                token_factory=lambda: "fresh")

    def test_status_requires_one_numeric_zero_and_prompt(self):
        with mock.patch.object(RUNNER, "STATUS_LIMIT", 0.01):
            for trailer in (
                b"nsh> echo $?\r\n1\r\nnsh> ",
                b"nsh> echo $?\r\n0\r\n0\r\nnsh> ",
                b"nsh> echo $?\r\necho $?\r\n0\r\nnsh> ",
                b"nsh> echo $?\r\n0\r\necho $?\r\nnsh> ",
                b"nsh> echo $?\r\nNXRS_GATT_CLIENT result=pass\r\n0\r\nnsh> ",
                b"nsh> NXRS_GATT_CLIENT result=pass\r\necho $?\r\n0\r\nnsh> ",
                b"nsh> echo $?\r\n0\r\n",
            ):
                with self.subTest(trailer=trailer), self.assertRaises(RuntimeError):
                    self.run_client(client_transcript(trailer=trailer))

    def test_unknown_payload_is_never_in_exception_or_output(self):
        secret = b"device-id=private-1234"
        console = FakeConsole(secret + b"\r\n" + client_transcript())
        # Unknown chatter is ignored, and the runner never prints it.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            RUNNER._client_exchange(console, "full")
        self.assertNotIn(secret.decode(), out.getvalue())
        bad = FakeConsole(b"NXRS_GATT_CLIENT phase=private-device-id\r\n")
        with self.assertRaises(RuntimeError) as error:
            RUNNER._client_exchange(bad, "full")
        self.assertNotIn("private-device-id", str(error.exception))

    def test_only_known_failure_stage_is_reported(self):
        known = [(stage, stage.decode("ascii")) for stage in RUNNER._FAILURE_STAGES]
        for stage, expected in [*known, (b"private_device_name", "client reported failure")]:
            console = FakeConsole(b"NXRS_GATT_CLIENT result=fail stage=" + stage + b"\r\n")
            with self.subTest(stage=stage), self.assertRaisesRegex(RuntimeError, expected) as error:
                RUNNER._client_exchange(console, "values")
            self.assertNotIn("private_device_name", str(error.exception))

    def test_values_mode_requires_the_four_exact_ordered_records(self):
        self.run_client(client_transcript("values"), "values")
        values = list(RUNNER._MODES["values"]["values"])
        invalid = (values[:-1], values[:2] + [values[2], values[1], values[3]],
                   values[:1] + [values[1] + b" extra"] + values[2:])
        for records in invalid:
            with self.subTest(records=records), self.assertRaises(RuntimeError):
                    self.run_client(client_transcript("values", values=records), "values")

    def test_values_records_after_disconnect_are_not_qualification(self):
        protocol = RUNNER._MODES["values"]
        records = [f"NXRS_GATT_CLIENT phase={phase}".encode() for phase in protocol["phases"]]
        records.extend(protocol["values"])
        records.append(protocol["pass"].encode())
        with self.assertRaisesRegex(RuntimeError, "values record invalid"):
            self.run_client(b"\r\n".join(records) + b"\r\nnsh> ", "values")

    def test_cancel_peer_requires_exact_cleanup_record(self):
        class Pump:
            def __init__(self, lines):
                self.lines = iter((line, False) for line in lines)

            def get(self, deadline):
                try:
                    return next(self.lines)
                except StopIteration:
                    raise RuntimeError("peer protocol timed out") from None

        with self.assertRaises(RuntimeError):
            RUNNER._peer_until(
                Pump((b"NXRS_RADIO_GATT_READY mode=cancel",
                      b"NXRS_RADIO_CANCEL_PASS connected=1 disconnected=1 writes=0 reads=0 invalid=0 cleanup=unconfirmed")),
                lambda line: line == RUNNER._CANCEL_PASS,
                time.monotonic() + 0.1,
                "cancel cleanup proof",
            )

        found = RUNNER._peer_until(Pump((RUNNER._CANCEL_PASS,)),
                                   lambda line: line == RUNNER._CANCEL_PASS,
                                   time.monotonic() + 0.1,
                                   "cancel cleanup proof")
        self.assertEqual(found, RUNNER._CANCEL_PASS)

    def test_peer_queue_discards_unknown_logs_and_fails_stickily_on_overflow(self):
        pump = RUNNER._PeerPump(FakeConsole(), time.monotonic() + 1)
        for _ in range(100):
            pump._publish((b"private-device-id-and-unrelated-log", False))
        self.assertEqual(pump.events.qsize(), 0)
        for _ in range(33):
            pump._publish((b"NXRS_RADIO_DIAGNOSTIC code=1", False))
        self.assertEqual(pump.events.qsize(), 32)
        with self.assertRaisesRegex(RuntimeError, "queue overflow"):
            pump.get(time.monotonic() + 0.1)

    def test_serial_configuration_is_lazy_and_disables_dtr_rts_before_open(self):
        state = []

        class Serial:
            def __init__(self, **kwargs):
                state.append(("init", kwargs))
                self._dtr = self._rts = None

            @property
            def dtr(self):
                return self._dtr

            @dtr.setter
            def dtr(self, value):
                self._dtr = value
                state.append(("dtr", value))

            @property
            def rts(self):
                return self._rts

            @rts.setter
            def rts(self, value):
                self._rts = value
                state.append(("rts", value))

            port = None

            def open(self):
                state.append(("open", self.port))

        console = RUNNER.open_serial("fake-port", type("SerialModule", (), {"Serial": Serial}))
        self.assertEqual([item for item in state if item[0] in ("dtr", "rts")],
                         [("dtr", False), ("rts", False)])
        self.assertEqual(state[-1], ("open", "fake-port"))
        self.assertEqual(state[0][1]["timeout"], 0.1)
        self.assertEqual(state[0][1]["write_timeout"], 1)
        self.assertTrue(state[0][1]["exclusive"])
        self.assertIsNotNone(console)

    def test_case_sequence_stops_after_first_failure_and_sanitizes_output(self):
        class Serial:
            def __init__(self, **kwargs):
                self.data = bytearray()
                self.closed = False
                self.port = None

            def open(self):
                pass

            def write(self, data):
                if data.startswith(b"echo NXRS_SYNC_"):
                    marker = data[len(b"echo "):].rstrip(b"\n")
                    self.data.extend(data.rstrip(b"\n") + b"\r\n" + marker + b"\r\nnsh> ")
                return len(data)

            def read(self, size):
                if self.closed:
                    raise OSError("private-serial-id")
                value = bytes(self.data[:size])
                del self.data[:size]
                return value

            def close(self):
                self.closed = True

        class Pump:
            def __init__(self, *args):
                self.thread = type("Thread", (), {"is_alive": lambda self: False, "start": lambda self: None})()

            def close(self):
                return True

        calls = []
        def fail_first(client, peer, pump, case):
            calls.append(case)
            raise RuntimeError("private-device-id")

        output = io.StringIO()
        with mock.patch.object(RUNNER, "_PeerPump", Pump), \
                mock.patch.object(RUNNER, "_run_case", fail_first), \
                contextlib.redirect_stdout(output):
            status = RUNNER.main(["--client-port", "c", "--peer-port", "p",
                                  "--case", "full", "--case", "values"],
                                 type("SerialModule", (), {"Serial": Serial}))
        self.assertEqual(status, 1)
        self.assertEqual(calls, ["full"])
        self.assertNotIn("private-device-id", output.getvalue())

    def test_stuck_client_thread_is_closed_joined_and_reported_as_failure(self):
        release = threading.Event()
        class Pump:
            def __init__(self):
                self.ready = True

            def get(self, deadline):
                if self.ready:
                    self.ready = False
                    return RUNNER._MODES["full"]["peer_ready"], False
                raise RuntimeError("peer protocol timed out")

            def _enqueue(self, item):
                pass

        class Client(FakeConsole):
            pass

        def stuck(*args):
            owned.append(threading.current_thread())
            release.wait(3)

        owned = []
        with mock.patch.object(RUNNER, "_client_exchange", stuck), \
                mock.patch.object(RUNNER, "CLIENT_LIMIT", 0.01):
            with self.assertRaisesRegex(RuntimeError, "reader thread did not finish"):
                RUNNER._run_case(Client(), FakeConsole(), Pump(), "full")
        release.set()
        owned[0].join(1)
        self.assertFalse(owned[0].is_alive())

    def test_peer_cleanup_is_safe_when_reader_thread_never_started(self):
        console = FakeConsole()
        pump = RUNNER._PeerPump(console, time.monotonic() + 1)
        self.assertTrue(pump.close())
        self.assertTrue(console.closed)

    def test_missing_native_binary_is_rejected_before_opening_a_console(self):
        output = io.StringIO()
        with mock.patch.object(RUNNER, 'open_serial') as opened, contextlib.redirect_stdout(output):
            status = RUNNER.qualify(None, 'p', native_client='/missing/private-device-id')
        self.assertEqual(status, 1)
        opened.assert_not_called()
        self.assertNotIn('private-device-id', output.getvalue())

    @unittest.skipUnless(os.name == 'posix', 'Unix native process transport')
    def test_native_client_uses_real_exit_and_records_without_a_fake_nsh_prompt(self):
        class Pump:
            def __init__(self, *args):
                self.thread = type('Thread', (), {'start': lambda self: None})()

            def close(self):
                return True

        transcript = client_transcript().split(b'nsh> ', 1)[0].decode()
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'native-client'
            for exit_code in (0, 7):
                binary.write_text('#!/bin/sh\nprintf %s ' + shlex.quote(transcript) + f'\nexit {exit_code}\n')
                binary.chmod(0o700)
                peer = FakeConsole()
                output = io.StringIO()

                def exchange_case(client, peer, pump, case, *, exchange, client_limit):
                    # The actual owned child and shared parser are used; only
                    # the separately tested device-peer handshake is injected.
                    exchange(client, case, time.monotonic)

                with mock.patch.object(RUNNER, 'open_serial', return_value=peer) as opened, \
                        mock.patch.object(RUNNER, 'sync_console') as synced, \
                        mock.patch.object(RUNNER, '_PeerPump', Pump), \
                        mock.patch.object(RUNNER, '_run_case', exchange_case), \
                        contextlib.redirect_stdout(output):
                    status = RUNNER.qualify(None, 'p', native_client=binary, client_limit=10)
                with self.subTest(exit_code=exit_code):
                    self.assertEqual(status, int(exit_code != 0), output.getvalue())
                    opened.assert_called_once_with('p', None)
                    synced.assert_called_once_with(peer, 'peer')
                    self.assertTrue(peer.closed)
                    if exit_code:
                        self.assertNotIn('PASS cases=', output.getvalue())
                        self.assertIn('native_client_exited_nonzero', output.getvalue())

    def test_peer_progress_can_precede_client_cleanup(self):
        for mode in ("full", "values", "cancel"):
            with self.subTest(mode=mode):
                release = threading.Event()
                client = FakeConsole(client_transcript(mode))
                pump = RUNNER._PeerPump(FakeConsole(), time.monotonic() + 2)
                original_observe = RUNNER._PeerState.observe

                def observed(state, line, prompt):
                    original_observe(state, line, prompt)
                    if state.done or state.prompt:
                        release.set()

                class Peer(FakeConsole):
                    def write(self, value):
                        super().write(value)
                        if value.startswith(b"radio_test"):
                            pump._publish((RUNNER._MODES[mode]["peer_ready"], False))
                            if mode == "cancel":
                                pump._publish((RUNNER._CANCEL_PASS, False))
                                pump._publish((b"", True))
                            else:
                                pump._publish((b"NXRS_RADIO_GATT_DONE", False))
                        elif value == b"done\n":
                            pump._publish((RUNNER._PEER_PASS, False))
                            pump._publish((b"", True))
                        elif value == b"echo $?\n":
                            for item in ((b"echo $?", False), (b"0", False), (b"", True)):
                                pump._publish(item)
                        return len(value)

                peer = Peer()
                exchange = RUNNER._client_exchange

                def delayed_client(*args):
                    if not release.wait(1):
                        raise RuntimeError("peer progress was not retained")
                    exchange(*args)

                with mock.patch.object(RUNNER._PeerState, "observe", observed), \
                        mock.patch.object(RUNNER, "_client_exchange", delayed_client):
                    RUNNER._run_case(client, peer, pump, mode)
                self.assertEqual(peer.writes.count(b"done\n"), int(mode != "cancel"))

    def test_peer_cleanup_may_not_precede_finish_authorization(self):
        state = RUNNER._PeerState("values")
        state.observe(b"NXRS_RADIO_GATT_DONE", False)
        with self.assertRaisesRegex(RuntimeError, "cleanup proof invalid"):
            state.observe(RUNNER._PEER_PASS, False)

    def test_peer_duplicate_completion_and_premature_prompt_fail(self):
        state = RUNNER._PeerState("full")
        state.observe(b"NXRS_RADIO_GATT_DONE", False)
        with self.assertRaisesRegex(RuntimeError, "done record invalid"):
            state.observe(b"NXRS_RADIO_GATT_DONE", False)
        with self.assertRaisesRegex(RuntimeError, "prompt arrived"):
            state.observe(b"", True)


if __name__ == "__main__":
    unittest.main()
