"""Host-only composition and negative controls, without a Bluetooth adapter."""

import importlib.util
from pathlib import Path
import unittest
from unittest import mock

from test_gatt_client_runner import FakeConsole, client_transcript


SPEC = importlib.util.spec_from_file_location(
    "portable_radio_client", Path(__file__).resolve().parents[1] / "nuttx-std/radio/client.py")
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)

READY = b"NXRS_WIFI_CREDENTIALS_READY"
DHCP = b"NXRS_WIFI_ASSOCIATED_DHCP"
OVERLAP = b"NXRS_RADIO_CLIENT_OVERLAP first_ms=5000 last_ms=15000 first_echoes=0 last_echoes=20"
TRAFFIC = b"NXRS_RADIO_TRAFFIC_PASS gatt=100 udp=21"


def transcript(records=(OVERLAP, TRAFFIC, CLIENT._PASS)):
    gatt, _, trailer = client_transcript().partition(b"nsh> ")
    return (READY + b"\r\n" + DHCP + b"\r\n" + gatt
            + b"\r\n".join(records) + b"\r\nnsh> " + trailer)


class PortableRadioClientTests(unittest.TestCase):
    def exchange(self, data, password=b"private-passphrase"):
        console = FakeConsole(data)
        evidence = CLIENT.Evidence(console, b"fixture", password, "udp")
        CLIENT.GATT._client_exchange(console, "full", observe=evidence.observe,
                                     validate=evidence.validate, client_limit=0.2)
        return console, evidence

    def test_credentials_and_exact_complete_proof(self):
        console, evidence = self.exchange(transcript())
        self.assertEqual(evidence.count, 21)
        self.assertEqual(console.writes[1], b"\x07\x12fixtureprivate-passphrase")

    def test_missing_duplicate_and_reordered_evidence_fails(self):
        for records in (
            (), (OVERLAP, TRAFFIC), (TRAFFIC, OVERLAP, CLIENT._PASS),
            (OVERLAP, OVERLAP, TRAFFIC, CLIENT._PASS),
            (OVERLAP, TRAFFIC, TRAFFIC, CLIENT._PASS),
            (OVERLAP, TRAFFIC, CLIENT._PASS, CLIENT._PASS),
        ):
            with self.subTest(records=records), self.assertRaises(CLIENT.GATT.ProtocolError):
                self.exchange(transcript(records))

    def test_exact_gatt_oracle_cannot_be_replaced_by_radio_proofs(self):
        for data in (
            transcript().replace(b"phase=discovered", b"phase=connected"),
            transcript().replace(b"pairs=100 bytes_per_pair", b"pairs=99 bytes_per_pair"),
            transcript().replace(b"native_close=confirmed", b"native_close=pending"),
            transcript().replace(b"echo $?\r\n0", b"echo $?\r\n1"),
        ):
            with self.subTest(), self.assertRaises(CLIENT.GATT.ProtocolError):
                self.exchange(data)

    def test_gatt_cannot_start_before_association(self):
        with self.assertRaisesRegex(CLIENT.GATT.ProtocolError, "association lifetime"):
            self.exchange(transcript().replace(DHCP + b"\r\n", b""))

    def test_duplicate_or_out_of_order_credential_stages_fail(self):
        for data in (transcript().replace(READY, READY + b"\r\n" + READY),
                     transcript().replace(READY + b"\r\n" + DHCP, DHCP + b"\r\n" + READY),
                     transcript().replace(DHCP, DHCP + b"\r\n" + DHCP)):
            with self.subTest(), self.assertRaises(CLIENT.GATT.ProtocolError):
                self.exchange(data)

    def test_overlap_is_only_verified_progress_inside_gatt(self):
        for record in (
            OVERLAP.replace(b"last_ms=15000", b"last_ms=4999"),
            OVERLAP.replace(b"last_echoes=20", b"last_echoes=5"),
            OVERLAP.replace(b"first_echoes=0", b"first_echoes=1"),
            OVERLAP + b" extra",
        ):
            with self.subTest(record=record), self.assertRaises(CLIENT.GATT.ProtocolError):
                self.exchange(transcript((record, TRAFFIC, CLIENT._PASS)))

    def test_transport_final_count_and_explicit_cleanup_must_agree(self):
        for records in (
            (OVERLAP, TRAFFIC.replace(b"udp=21", b"tcp=21"), CLIENT._PASS),
            (OVERLAP, TRAFFIC.replace(b"udp=21", b"udp=19"), CLIENT._PASS),
            (OVERLAP, TRAFFIC, CLIENT._PASS.replace(b"worker=joined", b"worker=pending")),
        ):
            with self.subTest(), self.assertRaises(CLIENT.GATT.ProtocolError):
                self.exchange(transcript(records))

    def test_failure_and_secret_echo_are_sanitized_even_after_pass(self):
        for line in (b"private-passphrase", b"NXRS_RADIO_FAIL private-device-id",
                     b"NXRS_WIFI_STATION_FAIL private-device-id"):
            for data in (line + b"\r\n" + transcript(),
                         transcript().replace(b"echo $?\r\n0", b"echo $?\r\n" + line + b"\r\n0")):
                with self.subTest(line=line), self.assertRaises(CLIENT.GATT.ProtocolError) as error:
                    self.exchange(data)
                self.assertNotIn("private-", str(error.exception))

    def test_echo_is_joined_and_validated_before_client_completion(self):
        events = []

        class Echo:
            port = 1234

            def __init__(self, host, transport):
                events.append("created")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                events.append("joined")

            def validate(self, expected_count, minimum):
                self_outer.assertEqual(minimum, 6)
                events.append(("validated", expected_count))

        def qualify(*args, exchange, **kwargs):
            exchange(FakeConsole(transcript()), "full", CLIENT.GATT.time.monotonic)
            events.append("client_complete")
            return 0

        self_outer = self
        with mock.patch.object(CLIENT.GATT, "qualify", qualify):
            self.assertEqual(CLIENT.qualify("c", "p", "127.0.0.1", b"fixture", b"",
                                            "udp", echo_type=Echo), 0)
        self.assertEqual(events, ["created", "joined", ("validated", 21), "client_complete"])

    def test_network_cleanup_runs_after_gatt_failure_without_success_validation(self):
        events = []

        class Echo:
            port = 1234

            def __init__(self, *args):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                events.append("joined")

            def validate(self, **kwargs):
                raise AssertionError("failed exchange may not validate success")

        def qualify(*args, exchange, **kwargs):
            exchange(FakeConsole(b"NXRS_RADIO_FAIL private-id\r\n"), "full",
                     CLIENT.GATT.time.monotonic)

        with mock.patch.object(CLIENT.GATT, "qualify", qualify), \
                self.assertRaisesRegex(CLIENT.GATT.ProtocolError, "radio failure"):
            CLIENT.qualify("c", "p", "127.0.0.1", b"fixture", b"", "udp", echo_type=Echo)
        self.assertEqual(events, ["joined"])

    def test_exact_device_pass_does_not_override_missing_host_echoes(self):
        events = []

        class Echo:
            port = 1234

            def __init__(self, *args):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                events.append("joined")

            def validate(self, **kwargs):
                raise RuntimeError("private-network-id count mismatch")

        def qualify(*args, exchange, **kwargs):
            exchange(FakeConsole(transcript()), "full", CLIENT.GATT.time.monotonic)

        with mock.patch.object(CLIENT.GATT, "qualify", qualify), \
                self.assertRaisesRegex(CLIENT.GATT.ProtocolError, "host echo evidence invalid") as error:
            CLIENT.qualify("c", "p", "127.0.0.1", b"fixture", b"", "udp", echo_type=Echo)
        self.assertEqual(events, ["joined"])
        self.assertNotIn("private-network-id", str(error.exception))


if __name__ == "__main__":
    unittest.main()
