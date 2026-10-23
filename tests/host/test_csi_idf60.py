"""IDF reference acceptance/privacy failures cannot become compatibility passes."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "idf60_host", ROOT / "tests/csi-radio/idf60/run.py"
)
HOST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOST)


def capture_lines():
    return [
        b"IDF60_CALLBACKS observed=100 foreign=0 legacy=0 ht=100 other=0 dropped=0",
        b"IDF60_RESULT elapsed_ms=60000 frames=100 factors=51 sent=10 echoes=10 raw_exported=10 force=0 transport_only=0",
        HOST.PASS,
    ]


class IdfReferenceTests(unittest.TestCase):
    def test_complete_transport_protocol_accepts_saved_byte_credentials(self):
        console = MagicMock()
        console.read.return_value = (
            b"IDF60_COMMAND_READY force=0\n"
            b"IDF60_OPTIONS seconds=30 transport_only=1 tcp=1 raw=0 force=0 peer_port=1234\n"
            b"IDF60_CREDENTIALS_READY\n"
            b"IDF60_PEER bssid=021122334455 channel=6\n"
            b"IDF60_RESULT elapsed_ms=30000 frames=0 factors=0 sent=10 echoes=10 raw_exported=0 force=0 transport_only=1\n"
            + HOST.PASS
            + b"\n"
        )
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.getsockname.return_value = ("192.0.2.1", 1234)

        def serve(_listener, _stopped, served, _maximum):
            served.update(range(10))
            return 0

        with patch.object(HOST.socket, "socket", return_value=listener), patch.object(
            HOST.RADIO, "serve_tcp_echo", side_effect=serve
        ), patch.object(HOST.RADIO, "write_console") as send:
            report = HOST.Client(console).qualify(
                "192.0.2.1", b"private-ssid", b"private-password", 30
            )
        self.assertTrue(report["host_echo_worker_joined"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            send.call_args.args[1],
            HOST.credential_frame(b"private-ssid", b"private-password")[0],
        )

    def test_saved_wifi_byte_credentials_match_text_framing(self):
        expected = HOST.credential_frame("private-ssid", "private-password")
        self.assertEqual(
            HOST.credential_frame(b"private-ssid", b"private-password"), expected
        )
        for ssid, password in (
            (b"", b"12345678"),
            (b"x" * 33, b"12345678"),
            (b"ssid", b"short"),
            (b"ssid", b"x" * 64),
        ):
            with self.assertRaises(ValueError):
                HOST.credential_frame(ssid, password)

    def test_valid_capture_leaves_private_gain_and_presence_unqualified(self):
        report = HOST.validate(capture_lines(), set(range(10)), 60, False, 10)
        self.assertEqual(report["frames"], 100)
        self.assertFalse(report["private_gain_metadata_qualified"])
        self.assertFalse(report["presence_qualified"])

    def test_short_lossy_or_incomplete_capture_is_rejected(self):
        for old, new in (
            (b"elapsed_ms=60000", b"elapsed_ms=59999"),
            (b"factors=51", b"factors=50"),
            (b"echoes=10", b"echoes=9"),
            (b"dropped=0", b"dropped=1"),
            (b"force=0", b"force=1"),
        ):
            with self.subTest(change=new):
                lines = [x.replace(old, new) for x in capture_lines()]
                with self.assertRaises(ValueError):
                    HOST.validate(lines, set(range(10)), 60, False, 10)
        with self.assertRaises(ValueError):
            HOST.validate(capture_lines(), set(range(9)), 60, False, 10)
        with self.assertRaises(ValueError):
            HOST.validate(capture_lines(), set(range(10)), 60, False, 9)

    def test_transport_success_cannot_qualify_capture(self):
        lines = [
            b"IDF60_RESULT elapsed_ms=30000 frames=0 factors=0 sent=10 echoes=10 raw_exported=0 force=0 transport_only=1",
            HOST.PASS,
        ]
        HOST.validate(lines, set(range(10)), 30, True, 0)
        with self.assertRaises(ValueError):
            HOST.validate(lines, set(range(10)), 30, False, 0)
        with self.assertRaises(ValueError):
            HOST.validate(capture_lines(), set(range(10)), 60, True, 10)

    def test_missing_or_duplicate_cleanup_and_observations_are_rejected(self):
        for lines in (
            capture_lines()[:-1],
            capture_lines() + [HOST.PASS],
            capture_lines()[1:],
            capture_lines() + [capture_lines()[0]],
        ):
            with self.assertRaises(ValueError):
                HOST.validate(lines, set(range(10)), 60, False, 10)

    def test_wrong_port_or_force_ack_withholds_credentials(self):
        for options in (
            b"IDF60_OPTIONS seconds=30 transport_only=1 tcp=1 raw=0 force=0 peer_port=1235",
            b"IDF60_OPTIONS seconds=30 transport_only=1 tcp=1 raw=0 force=1 peer_port=1234",
        ):
            console = MagicMock()
            console.read.return_value = (
                b"IDF60_COMMAND_READY force=0\n"
                + options
                + b"\nIDF60_CREDENTIALS_READY\n"
            )
            listener = MagicMock()
            listener.__enter__.return_value = listener
            listener.getsockname.return_value = ("192.0.2.1", 1234)
            with patch.object(
                HOST.socket, "socket", return_value=listener
            ), patch.object(HOST.RADIO, "serve_tcp_echo", return_value=0), patch.object(
                HOST.RADIO, "write_console"
            ) as send:
                with self.assertRaisesRegex(RuntimeError, "credentials withheld"):
                    HOST.Client(console).qualify(
                        "192.0.2.1", "private-ssid", "private-password", 30
                    )
                self.assertEqual(send.call_count, 1)
                self.assertNotIn(b"private-", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
