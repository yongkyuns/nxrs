#!/usr/bin/env python3
"""Opt-in local fixture proxy using EXISTING enrollment and certificates.

For deployment use the generated nginx profile. This helper never generates
credentials, changes trust, starts media services or logs request headers.
"""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import socket
import ssl
import threading


def handler(upstream):
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def forward(self):
            if not (self.path == '/healthz' or self.path.startswith('/v1/')):
                self.send_error(404); return
            if (len(self.headers.get_all('Authorization', [])) > 1
                    or len(self.headers.get_all('Content-Length', [])) > 1
                    or self.headers.get('Transfer-Encoding')):
                self.send_error(400); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self.send_error(400); return
            if not 0 <= length <= 16384:
                self.send_error(413); return
            connection = http.client.HTTPConnection(*upstream, timeout=10)
            sent = False
            try:
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() in ('authorization', 'content-type', 'last-event-id', 'accept')}
                connection.request(self.command, self.path, body=self.rfile.read(length), headers=headers)
                response = connection.getresponse()
                streaming = (response.status == 200 and response.getheader('Content-Type', '').split(';')[0] == 'text/event-stream')
                body = None if streaming else response.read(1_048_577)
                if body is not None and len(body) > 1_048_576:
                    self.send_error(502); return
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() in ('content-type', 'cache-control', 'www-authenticate', 'x-content-type-options'):
                        self.send_header(k, v)
                if streaming:
                    self.send_header('Connection', 'close')
                else:
                    self.send_header('Content-Length', str(len(body)))
                self.end_headers(); sent = True
                if streaming:
                    while True:
                        chunk = response.read1(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk); self.wfile.flush()
                else:
                    self.wfile.write(body)
            except (OSError, http.client.HTTPException):
                if not sent:
                    self.send_error(503)
            finally:
                connection.close()
        do_GET = forward
        do_POST = forward
    return Proxy


class DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--upstream-port', type=int, required=True)
    parser.add_argument('--certificate', type=Path, required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--profile-file', type=Path)
    parser.add_argument('--profile-host')
    parser.add_argument('--profile-port', type=int, default=18000)
    args = parser.parse_args()
    for port in (args.port, args.upstream_port, args.profile_port):
        if not 1 <= port <= 65535:
            parser.error('invalid port')
    server = DualStackServer(('::', args.port), handler(('127.0.0.1', args.upstream_port)))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(args.certificate, args.key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    servers = [server]
    if args.profile_file:
        if not args.profile_host:
            parser.error('explicit local profile host required')
        body = args.profile_file.read_bytes()
        class Profile(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path != '/local-test.mobileconfig':
                    self.send_error(404); return
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-apple-aspen-config')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
        profile = ThreadingHTTPServer((args.profile_host, args.profile_port), Profile)
        profile.daemon_threads = True
        servers.append(profile)
    stopped = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stopped.set())
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        stopped.wait()
    finally:
        for s in servers:
            s.shutdown(); s.server_close()


if __name__ == '__main__':
    main()
