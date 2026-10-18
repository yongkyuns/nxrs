"""Host-only tests for the identity-free BLE scan witness reader."""

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "nuttx-std/radio/witness.py"
SPEC = importlib.util.spec_from_file_location("radio_witness", MODULE_PATH)
WITNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WITNESS)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeConsole:
    def __init__(self, data=b"", clock=None):
        self.data = bytearray(data)
        self.writes = []
        self.clock = clock

    def write(self, value):
        self.writes.append(value)
        return len(value)

    def read(self, size):
        if self.data:
            value = bytes(self.data[:size])
            del self.data[:size]
            if self.clock:
                self.clock.now += 0.001
            return value
        if self.clock:
            self.clock.now += 0.1
        return b""


def transcript(seen=(), done=None, ready=0, prompt=b"nsh> "):
    lines = [b"unrelated console banner", f"NXRS_BLE_WITNESS_READY elapsed_ms={ready}".encode()]
    lines.extend(f"NXRS_BLE_WITNESS_SEEN elapsed_ms={elapsed}".encode() for elapsed in seen)
    if done is not None:
        lines.append(f"NXRS_BLE_WITNESS_DONE matches={done} limited=false shutdown=joined".encode())
    return b"\r\n".join(lines) + b"\r\n" + prompt + b"echo $?\r\n0\r\nnsh> "


class RadioWitnessTests(unittest.TestCase):
    def run_witness(self, data, clock=None, timeout=25):
        clock = clock or FakeClock()
        console = FakeConsole(data, clock)
        result = WITNESS.read_witness(console, timeout=timeout, clock=clock)
        return result, console

    def test_valid_zero_one_and_multiple_matches(self):
        for seen, count in (((), 0), ((8,), 1), ((8, 12, 20), 3)):
            with self.subTest(count=count):
                clock = FakeClock()
                result, console = self.run_witness(transcript(seen, count), clock)
                self.assertEqual(result.matches, count)
                self.assertFalse(result.limited)
                self.assertEqual(len(result.seen_at), count)
                self.assertEqual(console.writes, [b"ble_test fixture\n", b"echo $?\n"])

    def test_limited_true_and_ansi_prompt(self):
        data = (b"NXRS_BLE_WITNESS_READY elapsed_ms=2\r\n"
                b"NXRS_BLE_WITNESS_DONE matches=0 limited=true shutdown=joined\r\n"
                b"\x1b[32mnsh\x1b[K> echo $?\r\n0\r\nnsh> ")
        result, _ = self.run_witness(data)
        self.assertTrue(result.limited)

    def test_malformed_duplicate_out_of_order_and_nonmonotonic_records(self):
        invalid = (
            b"NXRS_BLE_WITNESS_READY elapsed_ms=x\r\n",
            b"NXRS_BLE_WITNESS_READY elapsed_ms=1\r\nNXRS_BLE_WITNESS_READY elapsed_ms=2\r\n",
            b"NXRS_BLE_WITNESS_SEEN elapsed_ms=1\r\n",
            b"NXRS_BLE_WITNESS_DONE matches=0 limited=false shutdown=joined\r\n",
            b"NXRS_BLE_WITNESS_READY elapsed_ms=4\r\nNXRS_BLE_WITNESS_SEEN elapsed_ms=3\r\n",
            b"NXRS_BLE_WITNESS_READY elapsed_ms=0\r\nNXRS_BLE_WITNESS_DONE matches=1 limited=false shutdown=joined\r\n",
            b"NXRS_BLE_WITNESS_READY elapsed_ms=0\r\nNXRS_BLE_WITNESS_DONE matches=33 limited=false shutdown=joined\r\n",
            b"NXRS_BLE_SCAN_FAIL private diagnostic text\r\n",
        )
        for records in invalid:
            with self.subTest(records=records[:40]), self.assertRaises(RuntimeError):
                self.run_witness(records + b"nsh> ")

    def test_missing_done_timeout_and_prompt_before_done(self):
        clock = FakeClock()
        console = FakeConsole(b"NXRS_BLE_WITNESS_READY elapsed_ms=0\r\n", clock)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            WITNESS.read_witness(console, timeout=0.25, clock=clock)
        for data in (b"NXRS_BLE_WITNESS_READY elapsed_ms=0\r\nnsh> ",
                     b"NXRS_BLE_WITNESS_READY elapsed_ms=0\r\n"):
            with self.assertRaises(RuntimeError):
                self.run_witness(data, timeout=0.25)

    def test_bad_or_missing_nsh_status_is_not_success(self):
        prefix = (b"NXRS_BLE_WITNESS_READY elapsed_ms=0\r\n"
                  b"NXRS_BLE_WITNESS_DONE matches=0 limited=false shutdown=joined\r\n"
                  b"nsh> ")
        for trailer in (b"echo $?\r\n1\r\nnsh> ",
                        b"echo $?\r\n0\r\n1\r\nnsh> ",
                        b"echo $?\r\n0\r\n0\r\nnsh> ",
                        b"echo $?\r\n-1\r\n0\r\nnsh> ",
                        b"echo $?\r\nnsh> ",
                        b"echo $?\r\n0\r\n"):
            with self.subTest(trailer=trailer), self.assertRaises(RuntimeError):
                self.run_witness(prefix + trailer, timeout=0.25)

    def test_unknown_console_lines_and_secrets_never_appear_in_errors(self):
        secret = b"private-device-id-or-payload"
        data = (b"unknown " + secret + b"\r\n"
                b"NXRS_BLE_WITNESS_READY elapsed_ms=0\r\n"
                b"NXRS_BLE_WITNESS_DONE matches=1 limited=false shutdown=joined\r\n"
                b"nsh> ")
        with self.assertRaises(RuntimeError) as error:
            self.run_witness(data)
        self.assertNotIn(secret.decode(), str(error.exception))

    def test_known_prefix_malformed_after_done_is_rejected(self):
        data = (b"NXRS_BLE_WITNESS_READY elapsed_ms=0\r\n"
                b"NXRS_BLE_WITNESS_DONE matches=0 limited=false shutdown=joined\r\n"
                b"NXRS_BLE_WITNESS_SEEN elapsed_ms=1\r\nnsh> ")
        with self.assertRaises(RuntimeError):
            self.run_witness(data)

    def test_unbounded_console_line_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'line exceeded limit'):
            self.run_witness(b'x' * 8193, timeout=100)

    def test_console_errors_do_not_expose_backend_text(self):
        class BrokenConsole(FakeConsole):
            def read(self, size):
                raise OSError('private board identifier')

        with self.assertRaisesRegex(RuntimeError, '^BLE witness console read failed$'):
            WITNESS.read_witness(BrokenConsole(), clock=FakeClock())


if __name__ == "__main__":
    unittest.main()
