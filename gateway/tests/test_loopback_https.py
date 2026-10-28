"""Real verified TLS, stalled-peer isolation and request-owner retirement."""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.request

from detection_video_reference import native
from loopback_https import LoopbackServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()


class ObservedServer(LoopbackServer):
    def __init__(self, *args):
        self.worker_started = threading.Event()
        self.workers = []
        super().__init__(*args)

    def finish_request(self, *args):
        self.workers.append(threading.current_thread())
        self.worker_started.set()
        super().finish_request(*args)

    def handle_error(self, *_):
        # Interrupted/wrong-trust peers are deliberate negative controls.
        pass


class LoopbackHttpsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='nxrs-https-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        native.create_certificates(self.root)
        self.server = ObservedServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.root/'cert.pem', self.root/'key.pem')
        self.server.use_tls(context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(all(not worker.is_alive() for worker in self.server.workers))

    def test_unfinished_handshake_cannot_block_an_independently_verified_client(self):
        with socket.create_connection(self.server.server_address, timeout=1):
            self.assertTrue(self.server.worker_started.wait(1))
            trust = ssl.create_default_context(cafile=str(self.root/'root.pem'))
            url = 'https://localhost:'+str(self.server.server_port)+'/probe'
            with urllib.request.urlopen(url, context=trust, timeout=2) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.read(), b'')

    def test_shutdown_joins_a_worker_with_an_unfinished_handshake(self):
        with socket.create_connection(self.server.server_address, timeout=1):
            self.assertTrue(self.server.worker_started.wait(1))
            self.close_server()

    def test_shutdown_joins_an_authenticated_client_that_has_not_sent_http(self):
        trust = ssl.create_default_context(cafile=str(self.root/'root.pem'))
        with trust.wrap_socket(socket.socket(), server_hostname='localhost') as client:
            client.settimeout(2)
            client.connect(self.server.server_address)
            self.assertTrue(self.server.worker_started.wait(1))
            self.close_server()

    def test_wrong_root_is_still_rejected(self):
        trust = ssl.create_default_context(cafile=str(self.root/'wrong-root.pem'))
        with trust.wrap_socket(socket.socket(), server_hostname='localhost') as client:
            client.settimeout(2)
            with self.assertRaises(ssl.SSLCertVerificationError):
                client.connect(self.server.server_address)


if __name__ == '__main__':
    unittest.main()
