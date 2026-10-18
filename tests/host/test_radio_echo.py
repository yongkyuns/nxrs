"""Local socket lifecycle and evidence checks for the radio echo peer."""
import importlib.util
from pathlib import Path
import socket
import time
import unittest

PATH = Path(__file__).resolve().parents[1] / 'nuttx-std/radio/run.py'
SPEC = importlib.util.spec_from_file_location('radio_echo_runner', PATH)
RADIO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RADIO)


class RadioEchoTests(unittest.TestCase):
    def test_live_worker_cannot_supply_a_shutdown_proof(self):
        with RADIO.EchoPeer('127.0.0.1', 'udp') as peer:
            peer.served.update(range(7))
            with self.assertRaisesRegex(RuntimeError, 'shutdown not confirmed'):
                peer.validate()

    def test_udp_rejects_malformed_and_duplicate_packets(self):
        with RADIO.EchoPeer('127.0.0.1', 'udp') as peer:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(1)
                target = ('127.0.0.1', peer.port)
                client.sendto(b'bad', target)
                for sequence in range(7):
                    packet = RADIO.WIFI.expected_packet(sequence)
                    client.sendto(packet, target)
                    self.assertEqual(client.recvfrom(513)[0], packet)
                duplicate = RADIO.WIFI.expected_packet(0)
                client.sendto(duplicate, target)
                final_packet = RADIO.WIFI.expected_packet(7)
                client.sendto(final_packet, target)
                self.assertEqual(client.recvfrom(513)[0], final_packet)
        self.assertEqual(peer.invalid, 2)
        self.assertFalse(peer._worker.is_alive())
        with self.assertRaisesRegex(RuntimeError, 'evidence invalid'):
            peer.validate()

    def test_udp_requires_exact_expected_count(self):
        with RADIO.EchoPeer('127.0.0.1', 'udp') as peer:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(1)
                for sequence in range(7):
                    packet = RADIO.WIFI.expected_packet(sequence)
                    client.sendto(packet, ('127.0.0.1', peer.port))
                    self.assertEqual(client.recvfrom(513)[0], packet)
        with self.assertRaisesRegex(RuntimeError, 'count mismatch'):
            peer.validate(expected_count=8)
        peer.validate(expected_count=7)

    def test_tcp_echo_requires_natural_eof_and_cleans_up_worker(self):
        with RADIO.EchoPeer('127.0.0.1', 'tcp') as peer:
            with socket.create_connection(('127.0.0.1', peer.port), timeout=1) as client:
                for sequence in range(7):
                    packet = RADIO.WIFI.expected_packet(sequence)
                    client.sendall(packet)
                    self.assertEqual(RADIO.recv_exact(
                        client, len(packet), time.monotonic() + 1), packet)
                client.shutdown(socket.SHUT_WR)
        self.assertTrue(peer.natural_eof.is_set())
        self.assertFalse(peer._worker.is_alive())
        peer.validate(expected_count=7)


if __name__ == '__main__':
    unittest.main()
