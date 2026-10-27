"""Private native-client fixture server; no deployment or machine trust policy."""
from http.server import ThreadingHTTPServer
import socket
from socketserver import TCPServer
import ssl
import threading


class LoopbackServer(ThreadingHTTPServer):
    # server_close joins request owners after interrupting their private sockets.
    daemon_threads = False

    def __init__(self, *args, **kwargs):
        self._connections = set()
        self._connections_lock = threading.Lock()
        self._closing = False
        super().__init__(*args, **kwargs)

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]

    def use_tls(self, context):
        # A listening SSLSocket otherwise handshakes inside accept(), before
        # ThreadingHTTPServer can assign a worker to the connection.
        self.socket = context.wrap_socket(self.socket, server_side=True,
                                         do_handshake_on_connect=False)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        with self._connections_lock:
            if self._closing:
                request.close()
                raise OSError('fixture server is closing')
            self._connections.add(request)
        return request, address

    def finish_request(self, request, address):
        if isinstance(request, ssl.SSLSocket):
            request.do_handshake()
        super().finish_request(request, address)

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self._connections_lock:
                self._connections.discard(request)

    def server_close(self):
        # Owners call shutdown() first to join the accept loop. Closing the
        # admitted sockets also wakes idle TLS/HTTP readers before thread joins.
        with self._connections_lock:
            self._closing = True
            connections = tuple(self._connections)
        for request in connections:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()
        super().server_close()
