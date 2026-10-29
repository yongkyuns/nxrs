"""Real SDK server against independent OpenSSL TLS; private synthetic PKI only."""
import argparse
import os
from pathlib import Path
import select
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'gateway/tests'))
from tls_reference import Credentials, private


class Sessions(unittest.TestCase):
    binary = ROOT/'target/debug/tls-endpoint'

    def start(self, *, age=30000, case='echo'):
        directory = tempfile.TemporaryDirectory(prefix='nxrs-sdk-session-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        root.chmod(0o700)
        error = (root/'endpoint.log').open('w+')
        self.addCleanup(error.close)
        process = subprocess.Popen([str(self.binary), str(root), str(age), case], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=error, text=True)
        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            remaining = process.stdout.read(1024).strip()
            if remaining:
                print('endpoint result: '+remaining, file=sys.stderr)
            error.seek(0)
            diagnostic = error.read(2048).strip()
            if diagnostic:
                print('endpoint diagnostic: '+diagnostic, file=sys.stderr)
            process.stdin.close(); process.stdout.close()
        self.addCleanup(stop)
        self.assertEqual(self.line(process), 'CSR_READY')
        credentials = Credentials(root, server_csr=root/'request.csr')
        return root, process, credentials, error

    def line(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 15)[0], 'endpoint output deadline')
        return process.stdout.readline().strip()

    def load(self, root, process, credentials, *, corrupt_crl=False):
        for pem, der in [('server.pem', 'server.der'), ('ca.pem', 'issuer.der'), ('client.pem', 'peer.der')]:
            private(root/der, ssl.PEM_cert_to_DER_cert((root/pem).read_text()))
        credentials.call('crl', '-in', root/'crl.pem', '-outform', 'DER', '-out', root/'revocations.der')
        if corrupt_crl:
            data = bytearray((root/'revocations.der').read_bytes()); data[-1] ^= 1
            private(root/'revocations.der', bytes(data))
        process.stdin.write('load\n'); process.stdin.flush()
        line = self.line(process)
        self.assertTrue(line.startswith('LISTEN '), line)
        host, port = line.split()[1].rsplit(':', 1)
        return host, int(port)

    def context(self, root, version, *, peer='client'):
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(root/'ca.pem'))
        context.minimum_version = context.maximum_version = version
        if peer:
            context.load_cert_chain(str(root/(peer+'.pem')), str(root/(peer+'.key')))
        return context

    def result(self, root, process, credentials, error, *, accepted):
        line = self.line(process)
        process.wait(timeout=5)
        error.seek(0)
        diagnostic = error.read()[-2000:]
        self.assertEqual(process.returncode, 0, diagnostic)
        self.assertEqual(line == 'RESULT Ok(())', accepted, line+' '+diagnostic)
        # A retired connection leaves the same non-exportable signing key alive.
        keys = [subprocess.check_output([credentials.openssl, 'req', '-in', str(root/name),
                                        '-noout', '-pubkey'], timeout=10)
                for name in ['request.csr', 'retired.csr']]
        self.assertTrue(keys[0] == keys[1], 'connection retirement replaced enrolled key')

    def test_tls12_and_tls13_with_fragmented_plaintext(self):
        for version in [ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3]:
            with self.subTest(version=version.name):
                root, process, credentials, error = self.start()
                address = self.load(root, process, credentials)
                with socket.create_connection(address, timeout=5) as transport:
                    with self.context(root, version).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                        for byte in b'ping\n':
                            client.sendall(bytes([byte]))
                        self.assertEqual(client.recv(5), b'pong\n')
                self.result(root, process, credentials, error, accepted=True)

    def test_missing_or_different_valid_peer_cannot_publish(self):
        for peer in [None, 'other']:
            with self.subTest(peer=peer):
                root, process, credentials, error = self.start()
                credentials.issue('other', client=True)
                address = self.load(root, process, credentials)
                try:
                    with socket.create_connection(address, timeout=5) as transport:
                        with self.context(root, ssl.TLSVersion.TLSv1_3, peer=peer).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                            client.sendall(b'ping\n')
                            self.assertNotEqual(client.recv(5), b'pong\n')
                except (ssl.SSLError, ConnectionError):
                    pass
                self.result(root, process, credentials, error, accepted=False)

    def test_invalid_revocation_material_is_not_ignored(self):
        for case in ['revoked', 'wrong-issuer', 'corrupt', 'expired']:
            with self.subTest(case=case):
                root, process, credentials, error = self.start()
                if case == 'revoked':
                    credentials.call('ca', '-config', root/'ca.cnf', '-revoke', root/'client.pem')
                    credentials.crl()
                elif case == 'wrong-issuer':
                    other_root = root/'other-ca'; other_root.mkdir()
                    other = Credentials(other_root)
                    private(root/'crl.pem', (other_root/'crl.pem').read_bytes())
                elif case == 'expired':
                    credentials.crl(seconds=1)
                    time.sleep(1.2)
                address = self.load(root, process, credentials, corrupt_crl=case == 'corrupt')
                try:
                    with socket.create_connection(address, timeout=5) as transport:
                        with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                            client.sendall(b'ping\n')
                            self.assertNotEqual(client.recv(5), b'pong\n')
                except (ssl.SSLError, ConnectionError):
                    pass
                self.result(root, process, credentials, error, accepted=False)

    def test_expired_time_observation_stops_an_authenticated_connection(self):
        root, process, credentials, error = self.start(age=2000)
        address = self.load(root, process, credentials)
        with socket.create_connection(address, timeout=5) as transport:
            with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                time.sleep(2.2)
                try:
                    client.sendall(b'ping\n')
                    self.assertNotEqual(client.recv(5), b'pong\n')
                except (ssl.SSLError, ConnectionError):
                    pass
        self.result(root, process, credentials, error, accepted=False)

    def test_time_refresh_extends_valid_io_but_rollback_is_sticky(self):
        for case in ['refresh', 'rollback']:
            with self.subTest(case=case):
                root, process, credentials, error = self.start(age=2500, case=case)
                address = self.load(root, process, credentials)
                with socket.create_connection(address, timeout=5) as transport:
                    with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                        time.sleep(1.1)
                        client.sendall(b'ping\n')
                        if case == 'refresh':
                            self.assertEqual(self.line(process), 'REFRESHED')
                            time.sleep(2)
                            client.sendall(b'ping\n')
                            self.assertEqual(client.recv(5), b'pong\n')
                        else:
                            try:
                                self.assertNotEqual(client.recv(5), b'pong\n')
                            except (ssl.SSLError, ConnectionError):
                                pass
                self.result(root, process, credentials, error, accepted=case == 'refresh')

    def test_crl_deadline_stops_io_before_a_longer_time_lease(self):
        root, process, credentials, error = self.start()
        credentials.crl(seconds=4)
        address = self.load(root, process, credentials)
        with socket.create_connection(address, timeout=5) as transport:
            with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                time.sleep(4.2)
                try:
                    client.sendall(b'ping\n')
                    self.assertNotEqual(client.recv(5), b'pong\n')
                except (ssl.SSLError, ConnectionError):
                    pass
        self.result(root, process, credentials, error, accepted=False)

    def test_unusable_own_certificate_cannot_serve(self):
        for case in ['different-key', 'expired']:
            with self.subTest(case=case):
                root, process, credentials, error = self.start()
                credentials.issue('server', expired=case == 'expired',
                                  csr=root/'request.csr' if case == 'expired' else None)
                address = self.load(root, process, credentials)
                try:
                    with socket.create_connection(address, timeout=5) as transport:
                        with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                            client.sendall(b'ping\n')
                            self.assertNotEqual(client.recv(5), b'pong\n')
                except (ssl.SSLError, ConnectionError):
                    pass
                self.result(root, process, credentials, error, accepted=False)

    def test_plaintext_http_has_no_fallback(self):
        root, process, credentials, error = self.start()
        address = self.load(root, process, credentials)
        with socket.create_connection(address, timeout=5) as client:
            client.sendall(b'GET / HTTP/1.1\r\nHost: fixture\r\n\r\n')
            try:
                data = client.recv(256)
            except ConnectionError:
                data = b''
            self.assertFalse(data.startswith(b'HTTP/'))
            self.assertNotIn(b'pong', data)
        self.result(root, process, credentials, error, accepted=False)

    def test_silent_peer_has_an_absolute_handshake_deadline(self):
        root, process, credentials, error = self.start()
        address = self.load(root, process, credentials)
        with socket.create_connection(address, timeout=7) as client:
            started = time.monotonic()
            self.assertEqual(client.recv(5), b'')
            self.assertGreater(time.monotonic() - started, 4)
            self.assertLess(time.monotonic() - started, 6.5)
        self.result(root, process, credentials, error, accepted=False)

    def test_real_socket_backpressure_preserves_write_retry_ownership(self):
        for case in ['bulk', 'changed-retry']:
            with self.subTest(case=case):
                root, process, credentials, error = self.start(case=case)
                address = self.load(root, process, credentials)
                with socket.create_connection(address, timeout=5) as transport:
                    with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                        client.sendall(b'ping\n')
                        self.assertEqual(self.line(process), 'BLOCKED')
                        if case == 'bulk':
                            remaining = 16 * 1024 * 1024
                            while remaining:
                                data = client.recv(min(65536, remaining))
                                self.assertTrue(data, 'truncated encrypted bulk response')
                                self.assertEqual(data, b'Z'*len(data))
                                remaining -= len(data)
                            self.assertEqual(client.recv(5), b'pong\n')
                self.result(root, process, credentials, error, accepted=case == 'bulk')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Sessions.binary)
    args = parser.parse_args()
    Sessions.binary = args.binary.resolve()
    unittest.main(argv=[sys.argv[0]], verbosity=2)
