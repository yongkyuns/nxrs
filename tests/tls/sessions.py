"""Real SDK server against independent OpenSSL TLS; private synthetic PKI only."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import select
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'gateway/tests'))
from tls_reference import Credentials, private


class Sessions(unittest.TestCase):
    binary = Path(os.environ.get('NXRS_TLS_ENDPOINT', ROOT/'target/debug/tls-endpoint'))

    def test_pinned_bootstrap_needs_no_utc_and_rejects_other_peers_before_credentials(self):
        for wrong_pin, delayed in ((False, False), (True, False), (False, True)):
            with self.subTest(wrong_pin=wrong_pin, delayed=delayed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                credentials = Credentials(root)
                credentials.issue('expired', expired=True)
                certificate = ssl.PEM_cert_to_DER_cert((root/'expired.pem').read_text())
                pin = hashlib.sha256(certificate).digest()
                if wrong_pin: pin = bytes([pin[0] ^ 1]) + pin[1:]
                private(root/'gateway-pin', pin)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
                context.load_cert_chain(root/'expired.pem', root/'expired.key')
                received, faults = [], []
                with socket.socket() as server:
                    server.bind(('127.0.0.1', 0));server.listen();server.settimeout(5)
                    private(root/'gateway-address', f'127.0.0.1:{server.getsockname()[1]}')
                    def serve():
                        try:
                            with server.accept()[0] as raw, context.wrap_socket(raw, server_side=True) as client:
                                client.settimeout(3)
                                received.append(client.recv(32))
                                if delayed: time.sleep(.2)
                                if not wrong_pin: client.sendall(bytes([42]))
                        except (ssl.SSLError, ConnectionError, OSError) as error:
                            if not wrong_pin and not delayed: faults.append(type(error).__name__)
                    thread = threading.Thread(target=serve);thread.start()
                    try:
                        result = subprocess.run([str(self.binary), str(root), '100' if delayed else '3000', 'bootstrap'],
                                                capture_output=True, text=True, timeout=6)
                    finally:
                        thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(faults, faults)
                    if wrong_pin:
                        self.assertEqual(received, [b''])
                        self.assertIn('Err(Authentication)', result.stdout)
                    else:
                        self.assertEqual(received, [b'credential'])
                        self.assertIn('Err(Time)' if delayed else 'Ok(())', result.stdout)

    def test_pinned_exchange_preserves_a_live_enrolled_connection_and_key(self):
        root, process, credentials, error = self.start(case='interleave')
        credentials.issue('bootstrap')
        private(root/'gateway-pin', hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'bootstrap.pem').read_text())).digest())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(root/'bootstrap.pem', root/'bootstrap.key')
        received = []
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0));server.listen();server.settimeout(5)
            private(root/'gateway-address', f'127.0.0.1:{server.getsockname()[1]}')
            def serve():
                with server.accept()[0] as raw, context.wrap_socket(raw, server_side=True) as client:
                    client.settimeout(3)
                    received.append(client.recv(32));client.sendall(bytes([42]))
            thread = threading.Thread(target=serve);thread.start()
            try:
                host, port = self.load(root, process, credentials)
                with socket.create_connection((host, port), timeout=3) as raw:
                    with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(raw, server_hostname='127.0.0.1') as client:
                        client.sendall(b'ping\n')
                        self.assertEqual(client.recv(5), b'pong\n')
                self.result(root, process, credentials, error, accepted=True)
            finally: thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(received, [b'credential'])

    def start(self, *, age=30000, case='echo'):
        directory = tempfile.TemporaryDirectory(prefix='nxrs-sdk-session-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        root.chmod(0o700)
        error = (root/'endpoint.log').open('w+')
        self.addCleanup(error.close)
        arguments = [str(self.binary), str(root), str(age), case]
        if case == 'product':
            arguments.append(os.environ.get('NXRS_TEST_TLS_STACK_BYTES', '65536'))
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE,
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
        credentials.device_policy()
        return root, process, credentials, error

    def line(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 15)[0], 'endpoint output deadline')
        return process.stdout.readline().strip()

    def load(self, root, process, credentials, *, corrupt_crl=False, expect_ready=True):
        for pem, der in [('server.pem', 'server.der'), ('ca.pem', 'issuer.der'), ('client.pem', 'peer.der')]:
            private(root/der, ssl.PEM_cert_to_DER_cert((root/pem).read_text()))
        credentials.call('crl', '-in', root/'crl.pem', '-outform', 'DER', '-out', root/'revocations.der')
        if corrupt_crl:
            data = bytearray((root/'revocations.der').read_bytes()); data[-1] ^= 1
            private(root/'revocations.der', bytes(data))
        process.stdin.write('load\n'); process.stdin.flush()
        line = self.line(process)
        if not expect_ready:
            return line
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
        return line

    def test_certificate_purpose_is_required_before_handshake(self):
        for name in ['server', 'client']:
            wrong = 'clientAuth' if name == 'server' else 'serverAuth'
            for purposes in ['', wrong, 'serverAuth,clientAuth']:
                with self.subTest(certificate=name, purposes=purposes):
                    root, process, credentials, error = self.start()
                    names = ('URI:urn:nxrs:camera:site:cam,IP:127.0.0.1' if name == 'server'
                             else 'URI:urn:nxrs:ingest:site:gateway:cam')
                    private(root/'purpose.cnf', '[leaf]\nbasicConstraints=critical,CA:FALSE\n'
                        'keyUsage=critical,digitalSignature\nsubjectKeyIdentifier=hash\n'
                        f'authorityKeyIdentifier=keyid:always\nsubjectAltName={names}\n'+
                        (f'extendedKeyUsage={purposes}\n' if purposes else ''))
                    csr = root/('request.csr' if name == 'server' else 'client.csr')
                    credentials.call('ca', '-config', root/'ca.cnf', '-batch', '-notext',
                        '-extfile', root/'purpose.cnf', '-extensions', 'leaf', '-in', csr,
                        '-out', root/(name+'.pem'))
                    address = self.load(root, process, credentials)
                    # No ClientHello: reject the configured role before TLS I/O,
                    # rather than relying on the remote peer to enforce it.
                    with socket.create_connection(address, timeout=2) as transport:
                        self.assertEqual(transport.recv(1), b'')
                    self.assertEqual(self.result(root, process, credentials, error, accepted=False),
                                     'RESULT Err(Authentication)')

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

    def test_enrollment_issuer_and_uri_scope_are_required(self):
        for case in ['issuer-pin', 'camera-scope', 'gateway-scope', 'ambiguous-uri', 'missing-uri',
                     'policy-input', 'policy-capacity']:
            with self.subTest(case=case):
                root, process, credentials, error = self.start()
                if case == 'issuer-pin':
                    private(root/'issuer.sha256', bytes(32))
                elif case.startswith('policy-'):
                    private(root/'peer.uri', '' if case == 'policy-input' else 'x'*257)
                elif case == 'camera-scope':
                    credentials.issue('server', camera='other', csr=root/'request.csr')
                else:
                    credentials.gateway = 'other' if case == 'gateway-scope' else 'gateway'
                    credentials.issue('client')
                    if case in ['ambiguous-uri', 'missing-uri']:
                        names = ('URI:urn:nxrs:ingest:site:gateway:cam,URI:urn:nxrs:ingest:site:other:cam'
                                 if case == 'ambiguous-uri' else 'DNS:cam.local')
                        text = (root/'leaf.cnf').read_text().replace(
                            'URI:urn:nxrs:ingest:site:gateway:cam', names)
                        private(root/'leaf.cnf', text)
                        credentials.call('ca', '-config', root/'ca.cnf', '-batch', '-notext',
                            '-extfile', root/'leaf.cnf', '-extensions', 'leaf', '-in', root/'client.csr',
                            '-out', root/'client.pem')
                address = self.load(root, process, credentials)
                try:
                    with socket.create_connection(address, timeout=5) as transport:
                        with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(
                                transport, server_hostname='127.0.0.1') as client:
                            client.sendall(b'ping\n')
                            self.assertNotEqual(client.recv(5), b'pong\n')
                except (ssl.SSLError, ConnectionError):
                    pass
                expected = 'InvalidInput' if case.startswith('policy-') else 'Authentication'
                self.assertEqual(self.result(root, process, credentials, error, accepted=False),
                                 f'RESULT Err({expected})')

    def test_failed_replacement_retires_previous_enrollment(self):
        root, process, credentials, error = self.start(case='changed-enrollment')
        address = self.load(root, process, credentials)
        with socket.create_connection(address, timeout=5) as transport:
            with self.assertRaises((ssl.SSLError, ConnectionError, OSError)):
                self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(
                    transport, server_hostname='127.0.0.1')
        self.assertEqual(self.result(root, process, credentials, error, accepted=False),
                         'RESULT Err(InvalidInput)')

    def test_subject_alternative_name_traversal_is_bounded(self):
        for count in [8, 9]:
            with self.subTest(names=count):
                root, process, credentials, error = self.start()
                uri = 'URI:urn:nxrs:ingest:site:gateway:cam'
                names = ','.join([f'DNS:n{i}.local' for i in range(count-1)] + [uri])
                private(root/'leaf.cnf', (root/'leaf.cnf').read_text().replace(uri, names))
                credentials.call('ca', '-config', root/'ca.cnf', '-batch', '-notext',
                    '-extfile', root/'leaf.cnf', '-extensions', 'leaf', '-in', root/'client.csr',
                    '-out', root/'client.pem')
                address = self.load(root, process, credentials)
                try:
                    with socket.create_connection(address, timeout=5) as transport:
                        with self.context(root, ssl.TLSVersion.TLSv1_3).wrap_socket(
                                transport, server_hostname='127.0.0.1') as client:
                            client.sendall(b'ping\n')
                            self.assertEqual(count, 8, 'unbounded SAN list admitted')
                            self.assertEqual(client.recv(5), b'pong\n')
                except (ssl.SSLError, ConnectionError):
                    if count == 8:
                        raise
                self.result(root, process, credentials, error, accepted=count == 8)

    def test_shared_http_session_delivers_exact_jpegs_after_mutual_tls(self):
        for version, peer in [(ssl.TLSVersion.TLSv1_2, 'client'),
                              (ssl.TLSVersion.TLSv1_3, 'client'),
                              (ssl.TLSVersion.TLSv1_3, None),
                              (ssl.TLSVersion.TLSv1_3, 'other')]:
            with self.subTest(version=version.name, peer=peer):
                root, process, credentials, error = self.start(case='http')
                if peer == 'other':
                    credentials.issue('other', client=True)
                address = self.load(root, process, credentials)
                try:
                    with socket.create_connection(address, timeout=5) as transport:
                        with self.context(root, version, peer=peer).wrap_socket(transport, server_hostname='127.0.0.1') as client:
                            client.sendall(b'GET /stream HTTP/1.1\r\nHost: fixture\r\n\r\n')
                            response = http.client.HTTPResponse(client)
                            response.begin()
                            self.assertEqual(peer, 'client', 'unapproved peer received HTTP')
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.getheader('Content-Type'),
                                             'multipart/x-mixed-replace; boundary=nxrs-frame')
                            for sequence, name in enumerate(['background.jpg', 'person.jpg'], 1):
                                expected = (ROOT/'tests/nuttx-std/detection'/name).read_bytes()
                                self.assertEqual(response.fp.readline(128), b'--nxrs-frame\r\n')
                                self.assertEqual(response.fp.readline(128), b'Content-Type: image/jpeg\r\n')
                                self.assertEqual(response.fp.readline(128), f'Content-Length: {len(expected)}\r\n'.encode())
                                self.assertEqual(response.fp.readline(128), f'X-Sequence: {sequence}\r\n'.encode())
                                self.assertRegex(response.fp.readline(128), rb'^X-Capture-Ms: [0-9]+\r\n$')
                                self.assertEqual(response.fp.readline(128), b'\r\n')
                                self.assertEqual(response.read(len(expected)), expected)
                                self.assertEqual(response.read(2), b'\r\n')
                            response.close()
                except (ssl.SSLError, ConnectionError, http.client.HTTPException):
                    if peer == 'client':
                        raise
                self.result(root, process, credentials, error, accepted=peer == 'client')

    def test_product_refuses_unusable_enrollment_before_capture(self):
        for case, failures in [('issuer-pin', ['Authentication']),
                               ('crl', ['Crypto', 'Authentication']), ('time', ['Time'])]:
            with self.subTest(case=case):
                root, process, credentials, error = self.start(case='product')
                if case == 'issuer-pin':
                    private(root/'issuer.sha256', bytes(32))
                elif case == 'time':
                    (root/'time-error').touch()
                line = self.load(root, process, credentials, corrupt_crl=case == 'crl',
                                 expect_ready=False)
                self.assertIn(line, [f'STARTUP Err(Tls({failure}))' for failure in failures])
                process.wait(timeout=5)
                error.seek(0)
                self.assertEqual(process.returncode, 0, error.read()[-2000:])
                self.assertFalse((root/'capture-started').exists())

    def test_product_capture_tls_reconnect_cancellation_and_join(self):
        root, process, credentials, error = self.start(case='product')
        credentials.issue('other', client=True)
        address = self.load(root, process, credentials)

        def state_until(predicate):
            deadline = time.monotonic() + 5
            state = None
            while time.monotonic() < deadline:
                if (root/'state.json').exists():
                    state = json.loads((root/'state.json').read_text())
                    if predicate(state):
                        return state
                time.sleep(.02)
            self.fail('product state deadline: '+repr(state))

        def connect(version=ssl.TLSVersion.TLSv1_3, peer='client'):
            client = self.context(root, version, peer=peer).wrap_socket(
                socket.create_connection(address, timeout=5), server_hostname='127.0.0.1')
            self.addCleanup(client.close)
            client.sendall(b'GET /stream HTTP/1.1\r\nHost: fixture\r\n\r\n')
            response = http.client.HTTPResponse(client)
            self.addCleanup(response.close)
            response.begin()
            return client, response

        def image(response):
            self.assertEqual(response.status, 200)
            self.assertEqual(response.fp.readline(128), b'--nxrs-frame\r\n')
            headers = http.client.parse_headers(response.fp)
            sequence = int(headers['X-Sequence'])
            name = 'background.jpg' if sequence & 1 else 'person.jpg'
            expected = (ROOT/'tests/nuttx-std/detection'/name).read_bytes()
            self.assertEqual(int(headers['Content-Length']), len(expected))
            self.assertEqual(response.read(len(expected)), expected)
            self.assertEqual(response.read(2), b'\r\n')
            return sequence

        for peer in [None, 'other']:
            with self.assertRaises((ssl.SSLError, ConnectionError, http.client.HTTPException)):
                connect(peer=peer)
        state_until(lambda s: s['failures'] >= 2)
        self.assertEqual(state_until(lambda s: s['captured'] > 0)['accepted'], 0)

        client, response = connect(ssl.TLSVersion.TLSv1_2)
        first = image(response)
        self.assertGreater(image(response), first)
        response.close(); client.close()
        state_until(lambda s: s['disconnected'] >= 1)

        client, response = connect()
        image(response)
        (root/'block-time').touch()
        self.assertEqual(self.line(process), 'TIME_BLOCKED')
        before = state_until(lambda s: s['busy'] > 0)
        state_until(lambda s: s['captured'] >= before['captured'] + 20)
        # An entire offline/online cycle occurs while the worker cannot poll.
        (root/'control').write_text('offline')
        self.assertEqual(self.line(process), 'OFFLINE')
        (root/'control').write_text('online')
        self.assertEqual(self.line(process), 'ONLINE')
        (root/'block-time').unlink()
        state_until(lambda s: s['disconnected'] >= 2)
        response.close(); client.close()

        client, response = connect()
        image(response)
        (root/'time-error').touch()
        state_until(lambda s: s['failures'] >= 3 and s['disconnected'] >= 3)
        response.close(); client.close()
        (root/'time-error').unlink()
        client, response = connect()
        image(response)
        # Stop joins TLS even when stopping the camera itself fails.
        (root/'fail-camera-stop').touch()
        (root/'control').write_text('stop')
        self.assertEqual(self.line(process), 'CAMERA_STOPPED')
        process.wait(timeout=5)
        error.seek(0)
        self.assertEqual(process.returncode, 0, error.read()[-2000:])
        keys = [subprocess.check_output([credentials.openssl, 'req', '-in', str(root/name),
                                        '-noout', '-pubkey'], timeout=10)
                for name in ['request.csr', 'replacement.csr']]
        self.assertNotEqual(*keys, 'retired worker retained its native key owner')

        for retirement in ['drop', 'confirm']:
            root, process, credentials, error = self.start(case='product')
            address = self.load(root, process, credentials)
            client, response = connect()
            image(response)
            (root/'control').write_text(retirement)
            if retirement == 'confirm':
                marker = self.line(process)
                if marker != 'STARTUP_RETIRED':
                    error.seek(0)
                    self.fail(f'healthy retirement marker {marker!r}: '+error.read()[-2000:])
            self.assertEqual(self.line(process), 'CAMERA_STOPPED')
            process.wait(timeout=5)
            error.seek(0)
            self.assertEqual(process.returncode, 0, error.read()[-2000:])

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
