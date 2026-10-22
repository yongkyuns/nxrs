import importlib.util
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest

spec = importlib.util.spec_from_file_location('detection_proxy', Path(__file__).resolve().parents[1] / 'tools/local_https_proxy.py')
proxy = importlib.util.module_from_spec(spec); spec.loader.exec_module(proxy)


class DetectionProxyTests(unittest.TestCase):
    def test_sse_is_forwarded_before_upstream_closes_and_cursor_is_preserved(self):
        release = threading.Event(); observed = []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                observed.append(self.headers.get('Last-Event-ID'))
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                self.wfile.write(b'event: detection\ndata: {}\n\n'); self.wfile.flush()
                release.wait(3)
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        frontend = ThreadingHTTPServer(('127.0.0.1', 0), proxy.handler(upstream.server_address))
        upstream.daemon_threads = frontend.daemon_threads = True
        for s in (upstream, frontend): threading.Thread(target=s.serve_forever, daemon=True).start()
        connection = http.client.HTTPConnection(*frontend.server_address, timeout=1.5)
        try:
            connection.request('GET', '/v1/cameras/cam_one/detections', headers={'Last-Event-ID': 'none:0'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'data: {}', response.read1(4096))
            self.assertEqual(observed, ['none:0'])
            self.assertFalse(release.is_set())
        finally:
            release.set(); connection.close()
            for s in (frontend, upstream): s.shutdown(); s.server_close()
