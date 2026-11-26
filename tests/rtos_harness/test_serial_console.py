"""Host fixtures for serial framing and failure cleanup; no board is opened."""
from collections import deque
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.serial_console import SerialConsole


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SerialFixture:
    """An unopened byte stream with deterministic blocking and line replies."""
    def __init__(self, clock, *, fragment=4096, max_write=4096):
        self.clock = clock
        self.fragment = fragment
        self.max_write = max_write
        self.is_open = False
        self.dtr = self.rts = True
        self.incoming = deque([b"old boot\r\nnsh> "])
        self.sent = bytearray()
        self.line = bytearray()
        self.commands = []
        self.opened_with = None
        self.close_calls = 0
        self.open_error = None
        self.close_error = None
        self.write_error = None
        self.write_delay = 0
        self.timeouts = []
        self.read_sizes = []
        self.reply = lambda command: command + b"\r\nNuttX\r\nnsh> "
        self.echo_nonce = True

    def open(self):
        self.opened_with = (self.port, self.baudrate, self.dtr, self.rts,
                            self.timeout, self.write_timeout)
        self.is_open = True
        if self.open_error:
            raise self.open_error

    def close(self):
        self.close_calls += 1
        self.is_open = False
        if self.close_error:
            raise self.close_error

    def write(self, data):
        self.timeouts.append(self.write_timeout)
        if self.write_error:
            raise self.write_error
        self.clock.now += self.write_delay
        size = min(self.max_write, len(data))
        accepted = data[:size]
        self.sent.extend(accepted)
        self.line.extend(accepted)
        while b"\n" in self.line:
            line, _, rest = self.line.partition(b"\n")
            self.line = bytearray(rest)
            command = bytes(line)
            self.commands.append(command)
            if not command:
                self.incoming.append(b"\r\nnsh> ")
            elif command.startswith(b"echo NXRS_CONSOLE_SYNC_"):
                nonce = command.removeprefix(b"echo ")
                output = nonce + b"\r\n" if self.echo_nonce else b""
                self.incoming.append(command + b"\r\n" + output + b"nsh> ")
            else:
                result = self.reply(command)
                self.incoming.extend(result if isinstance(result, list) else [result])
        return size

    def read(self, amount):
        self.read_sizes.append(amount)
        self.timeouts.append(self.timeout)
        if not self.incoming:
            self.clock.now += self.timeout
            return b""
        data = self.incoming.popleft()
        if isinstance(data, Exception):
            raise data
        if data is None:
            self.is_open = False
            return b""
        size = min(amount, self.fragment, len(data))
        if size < len(data):
            self.incoming.appendleft(data[size:])
        return data[:size]


class SerialConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log = Path(temporary.name) / "capture" / "console.log"
        self.clock = Clock()
        self.serial = SerialFixture(self.clock)

    def console(self, **kwargs):
        return SerialConsole("fixture-port", self.log, serial_factory=lambda: self.serial,
                             clock=self.clock, **kwargs)

    def test_open_is_lazy_and_sets_modem_lines_before_open_without_reset(self):
        console = self.console()
        self.assertIsNone(self.serial.opened_with)
        with console:
            self.assertEqual(self.serial.opened_with,
                             ("fixture-port", 115200, False, False, 0.1, 0.1))
            self.assertEqual(self.serial.sent, b"")
        console.close()
        self.assertEqual(self.serial.close_calls, 1)
        self.assertEqual(self.log.read_bytes(), b"")

    def test_fragmented_handshake_consumes_stale_prompts_before_first_command(self):
        self.serial.fragment = 1
        with self.console() as console:
            console.synchronize()
            response = console.shell("uname")
            self.assertEqual(response.replace("\r", "").strip(), "uname\nNuttX\nnsh>")
            self.assertEqual(response.count("nsh>"), 1)
            self.assertNotIn("NXRS_CONSOLE_SYNC_", response)
            self.assertEqual(self.serial.commands[:3], [b"", b"", b""])
            self.assertTrue(self.serial.commands[3].startswith(b"echo NXRS_CONSOLE_SYNC_"))
            self.assertEqual(self.serial.commands[4:], [b"uname"])
            self.assertGreaterEqual(console.raw.count(b"nsh>"), 6)
            self.assertIn(b"old boot\r\n", console.raw)
        self.assertEqual(self.log.read_bytes(), console.raw)

    def test_nonce_command_echo_without_its_output_does_not_synchronize(self):
        self.serial.echo_nonce = False
        console = self.console(timeout=0.3)
        with self.assertRaisesRegex(TimeoutError, "NSH response"):
            with console:
                console.synchronize()
        self.assertEqual(self.serial.close_calls, 1)
        self.assertIn(b"echo NXRS_CONSOLE_SYNC_", self.log.read_bytes())
        self.assertLessEqual(self.clock.now, 0.301)

    def test_lf_crlf_and_inline_prompt_text_preserve_the_whole_response(self):
        self.serial.fragment = 2
        self.serial.reply = lambda command: command + b"\nvalue nsh> inside output\r\nOK\nnsh> "
        with self.console() as console:
            response = console.shell("status")
            self.assertEqual(response.strip(), "status\nvalue nsh> inside output\r\nOK\nnsh>")
            self.assertIn(b"\r\nOK\n", console.raw)

    def test_partial_writes_complete_each_command_once_with_bounded_io(self):
        self.serial.max_write = 2
        with self.console() as console:
            console.shell("uname")
            self.assertEqual(self.serial.commands.count(b"uname"), 1)
            self.assertEqual(self.serial.line, b"")
            self.assertTrue(all(0 < value <= 0.1 for value in self.serial.timeouts))
            self.assertTrue(all(0 < size <= 4096 for size in self.serial.read_sizes))
        self.assertEqual(self.serial.close_calls, 1)

    def test_stalled_or_failed_write_closes_without_replaying_a_partial_command(self):
        for failure in ("zero", "exception", "deadline"):
            with self.subTest(failure=failure):
                self.serial = SerialFixture(self.clock)
                console = self.console()
                console.synchronize()
                prior = bytes(self.serial.sent)
                if failure == "zero":
                    self.serial.max_write = 0
                elif failure == "exception":
                    self.serial.write_error = OSError("write deadline")
                else:
                    self.serial.max_write = 1
                    self.serial.write_delay = 0.1
                with self.assertRaises((RuntimeError, TimeoutError)):
                    console.shell("uname", timeout=0.05)
                self.assertTrue(console.closed)
                self.assertEqual(self.serial.close_calls, 1)
                self.assertEqual(bytes(self.serial.sent), prior + (b"u" if failure == "deadline" else b""))
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    console.shell("uname")
                self.assertEqual(self.log.read_bytes(), console.raw)

    def test_response_timeout_saves_partial_bytes_and_obeys_call_timeout(self):
        self.serial.reply = lambda command: command + b"\r\npartial result\r\n"
        console = self.console()
        console.synchronize()
        start = self.clock.now
        with self.assertRaisesRegex(TimeoutError, "NSH response"):
            console.shell("hang", timeout=0.25)
        self.assertAlmostEqual(self.clock.now - start, 0.25)
        self.assertTrue(console.closed)
        self.assertIn(b"partial result\r\n", self.log.read_bytes())
        self.assertEqual(self.log.read_bytes(), console.raw)

    def test_response_overflow_fails_instead_of_accepting_a_later_prompt(self):
        self.serial.reply = lambda command: command + b"\n" + b"x" * 160 + b"\nnsh> "
        console = self.console(max_response_bytes=160)
        with self.assertRaisesRegex(RuntimeError, "response limit"):
            console.shell("large")
        self.assertTrue(console.closed)
        self.assertEqual(self.serial.close_calls, 1)
        self.assertEqual(self.log.read_bytes(), console.raw)
        self.assertTrue(console.raw.endswith(b"x"))

    def test_transcript_overflow_across_commands_keeps_exact_bounded_log(self):
        self.serial.reply = lambda command: command + b"\n" + b"x" * 120 + b"\nnsh> "
        console = self.console(max_transcript_bytes=320)
        console.shell("first")
        before = console.raw
        with self.assertRaisesRegex(RuntimeError, "transcript limit"):
            console.shell("second")
        self.assertEqual(len(console.raw), 320)
        self.assertTrue(console.raw.startswith(before))
        self.assertEqual(self.log.read_bytes(), console.raw)
        self.assertEqual(self.serial.close_calls, 1)

    def test_disconnect_exception_or_closed_stream_retains_prior_output(self):
        for failure in (OSError("USB unplugged"), None):
            with self.subTest(failure=failure):
                self.serial = SerialFixture(self.clock)
                self.serial.reply = lambda command: [command + b"\npartial\n", failure]
                console = self.console()
                with self.assertRaisesRegex(RuntimeError, "disconnected"):
                    console.shell("read")
                self.assertEqual(self.serial.close_calls, 1)
                self.assertTrue(self.log.read_bytes().endswith(b"partial\n"))

    def test_enter_failure_closes_partial_open_and_records_a_log(self):
        self.serial.open_error = OSError("open failed after acquiring descriptor")
        console = self.console()
        with self.assertRaisesRegex(OSError, "open failed"):
            with console:
                self.fail("failed open cannot enter the body")
        self.assertEqual(self.serial.close_calls, 1)
        self.assertTrue(console.closed)
        self.assertEqual(self.log.read_bytes(), b"")

    def test_close_failure_still_saves_log_and_is_not_repeated(self):
        console = self.console()
        console.shell("uname")
        self.serial.close_error = OSError("close failed")
        with self.assertRaisesRegex(OSError, "close failed"):
            console.close()
        self.assertEqual(self.log.read_bytes(), console.raw)
        console.close()
        self.assertEqual(self.serial.close_calls, 1)

    def test_pyserial_import_is_lazy_and_missing_dependency_fails_closed(self):
        with patch.dict(sys.modules, {"serial": None}):
            console = SerialConsole("fixture-port", self.log)
            with self.assertRaisesRegex(RuntimeError, "pyserial==3.5"):
                with console:
                    self.fail("missing pyserial cannot open a port")
        self.assertTrue(console.closed)
        self.assertEqual(self.log.read_bytes(), b"")

    def test_invalid_settings_and_commands_have_no_serial_effects(self):
        for settings in ({"timeout": 0}, {"timeout": float("inf")},
                         {"timeout": float("nan")}, {"max_response_bytes": 0},
                         {"max_transcript_bytes": -1}, {"max_response_bytes": 1.5}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.console(**settings)
        console = self.console()
        for command in ("", " ", "uname\nreboot", "uname\r", "uname\0"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                console.shell(command)
        self.assertIsNone(self.serial.opened_with)
        self.assertEqual(self.serial.sent, b"")


if __name__ == "__main__":
    unittest.main()
