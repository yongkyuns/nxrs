import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from detection_video_reference import LoopbackServer


class LoopbackReadinessTests(unittest.TestCase):
    def test_fixture_binds_without_reverse_dns(self):
        # A stalled resolver must not prevent a private loopback TLS fixture
        # from becoming ready, even on an otherwise healthy hosted runner.
        with patch('socket.getfqdn', side_effect=AssertionError('unexpected DNS')):
            with self.assertRaisesRegex(AssertionError, 'unexpected DNS'):
                ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
            server = LoopbackServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        try:
            self.assertEqual(server.server_address[0], '127.0.0.1')
            self.assertEqual(server.server_name, 'localhost')
            self.assertEqual(server.server_port, server.server_address[1])
        finally:
            server.server_close()
