#!/usr/bin/env python3
"""Actual Rust ingest TLS controls; synthetic local CA and encoder witness only.

Uses the host OpenSSL CLI and stdlib TLS. No installed trust, device or live
service changes. This does not qualify native decoding or device TLS/entropy.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import ingest_service

TLS_CAMERA = None


def private(path, data):
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o600)


class Credentials:
    def __init__(self, root, *, server_csr=None, server_ip='127.0.0.1', site='site', camera='cam', gateway='gateway'):
        self.root = root
        self.site, self.camera, self.gateway = site, camera, gateway
        self.server_ip = server_ip
        self.openssl = os.environ.get('NXRS_TEST_OPENSSL', 'openssl')
        (root/'issued').mkdir()
        private(root/'index', '')
        private(root/'serial', '1000\n')
        private(root/'crlnumber', '01\n')
        # Host CLIs differ in their default EC encoding; the embedded parser
        # supports named P-256 parameters, not explicit domain descriptions.
        self.call('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256',
                  '-pkeyopt', 'ec_param_enc:named_curve', '-out', root/'ca.key')
        # Host request defaults can append duplicate issuer extensions to -addext.
        private(root/'request.cnf', '[req]\ndistinguished_name=subject\n[subject]\n')
        self.call('req', '-new', '-x509', '-key', root/'ca.key', '-sha256', '-days', '1',
                  '-config', root/'request.cnf',
                  '-subj', '/CN=Synthetic TLS fixture', '-out', root/'ca.pem',
                  '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
                  '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-addext', 'subjectKeyIdentifier=hash')
        private(root/'ca.cnf', f'''[ca]
default_ca=fixture
[fixture]
database={root}/index
serial={root}/serial
crlnumber={root}/crlnumber
new_certs_dir={root}/issued
private_key={root}/ca.key
certificate={root}/ca.pem
default_md=sha256
default_days=1
default_crl_hours=1
policy=policy
unique_subject=no
[policy]
commonName=supplied
[crl]
authorityKeyIdentifier=keyid:always
''')
        self.issue('server', csr=server_csr)
        self.issue('client')
        self.crl()

    def call(self, *args):
        subprocess.run([self.openssl, *map(str, args)], check=True, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def issue(self, name, *, client=False, camera=None, expired=False, csr=None):
        client = client or name == 'client'
        camera = self.camera if camera is None else camera
        root = self.root
        if csr is None:
            self.call('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256',
                      '-pkeyopt', 'ec_param_enc:named_curve', '-out', root/(name+'.key'))
            self.call('req', '-new', '-config', root/'request.cnf', '-key', root/(name+'.key'),
                      '-subj', '/CN=Synthetic '+name, '-out', root/(name+'.csr'))
            csr = root/(name+'.csr')
        uri = f'urn:nxrs:ingest:{self.site}:{self.gateway}:{camera}' if client else f'urn:nxrs:camera:{self.site}:{camera}'
        private(root/'leaf.cnf', '[leaf]\nbasicConstraints=critical,CA:FALSE\n'
                'keyUsage=critical,digitalSignature\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n'
                f'extendedKeyUsage={"clientAuth" if client else "serverAuth"}\n'
                f'subjectAltName=URI:{uri}'+('' if client else f',IP:{self.server_ip}')+'\n')
        dates = ['-startdate', '20000101000000Z', '-enddate', '20000102000000Z'] if expired else []
        self.call('ca', '-config', root/'ca.cnf', '-batch', '-notext', '-extfile', root/'leaf.cnf',
                  '-extensions', 'leaf', '-in', csr, '-out', root/(name+'.pem'), *dates)
        for path in root.iterdir():
            if path.is_file(): path.chmod(0o600)

    def crl(self, seconds=3600):
        self.call('ca', '-config', self.root/'ca.cnf', '-gencrl', '-crlexts', 'crl',
                  '-crlsec', str(seconds), '-out', self.root/'crl.pem')
        (self.root/'crl.pem').chmod(0o600)

    def config(self):
        root = self.root
        der = ssl.PEM_cert_to_DER_cert((root/'ca.pem').read_text())
        return dict(site_id=self.site, camera_id=self.camera, gateway_id=self.gateway,
            issuer_sha256=hashlib.sha256(der).hexdigest(), enrollment_generation=1,
            **{field: str(root/name) for field, name in [
                ('ca_certificate_file', 'ca.pem'), ('camera_certificate_file', 'server.pem'),
                ('client_certificate_file', 'client.pem'), ('client_key_file', 'client.key'), ('crl_file', 'crl.pem')]})

    def device_policy(self):
        # Synthetic enrollment inputs; production trust must be provisioned independently.
        private(self.root/'issuer.sha256', hashlib.sha256(
            ssl.PEM_cert_to_DER_cert((self.root/'ca.pem').read_text())).digest())
        private(self.root/'own.uri', f'urn:nxrs:camera:{self.site}:{self.camera}')
        private(self.root/'peer.uri', f'urn:nxrs:ingest:{self.site}:{self.gateway}:{self.camera}')


class CredentialTests(unittest.TestCase):
    def test_generated_certificates_use_named_p256_parameters(self):
        # SubjectPublicKeyInfo prefix: id-ecPublicKey + named prime256v1,
        # followed by an uncompressed 65-byte public point.
        prefix = bytes.fromhex('3059301306072a8648ce3d020106082a8648ce3d03010703420004')
        with tempfile.TemporaryDirectory(prefix='nxrs-named-curve-') as directory:
            root = Path(directory)
            credentials = Credentials(root)
            for name in ('ca', 'server', 'client'):
                with self.subTest(certificate=name):
                    public = subprocess.check_output([
                        credentials.openssl, 'x509', '-in', str(root/(name+'.pem')),
                        '-pubkey', '-noout'], timeout=15)
                    der = subprocess.run([
                        credentials.openssl, 'pkey', '-pubin', '-outform', 'DER'],
                        input=public, capture_output=True, check=True, timeout=15).stdout
                    self.assertEqual(len(der), len(prefix) + 64)
                    self.assertTrue(der.startswith(prefix))


class TlsProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nxrs-tls-process-')
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.addCleanup(self.temp.cleanup)
        self.credentials = Credentials(self.root)
        self.process = self.server = None
        self.handlers = set()
        self.loop_errors = []
        def transport_error(loop, context):
            error = context.get('exception')
            if not isinstance(error, (ssl.SSLError, ConnectionResetError, ConnectionAbortedError)):
                self.loop_errors.append(type(error).__name__)
        asyncio.get_running_loop().set_exception_handler(transport_error)
        self.requests = self.connections = self.active = self.peak = 0
        self.plaintext_http = False
        self.negotiated = set()
        self.marker = self.root/'received.jpeg'
        self.encoder_pid = self.root/'encoder.pid'
        self.publisher_pid = self.root/'publisher.pid'
        encoder = self.root/'encoder'
        private(encoder, f'#!{sys.executable}\nimport os,sys\n'
                f'with open({str(self.encoder_pid)!r},"w") as pid: pid.write(str(os.getpid()))\n'
                f'with open({str(self.marker)!r},"wb") as out:\n'
                ' n=0\n while data:=sys.stdin.buffer.read(1):\n'
                '  out.write(data);out.flush();sys.stdout.buffer.write(data);sys.stdout.buffer.flush()\n'
                '  n+=1;print("frame="+str(n),file=sys.stderr,flush=True)\n')
        encoder.chmod(0o700)
        publisher = self.root/'publisher'
        private(publisher, f'#!{sys.executable}\nimport os,sys\n'
                f'with open({str(self.publisher_pid)!r},"w") as pid: pid.write(str(os.getpid()))\n'
                'assert sys.stdin.buffer.read(64)==b"1"*64\n'
                'n=0\nwhile data:=sys.stdin.buffer.read(1):\n'
                ' n+=1;print("packet="+str(n),flush=True)\n')
        publisher.chmod(0o700)
        private(self.root/'publisher.token', '1'*64+'\n')
        self.jpeg = subprocess.check_output([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi',
            '-i', 'color=c=blue:s=320x240', '-frames:v', '1', '-c:v', 'mjpeg', '-threads', '1',
            '-f', 'image2pipe', 'pipe:1'], timeout=15)
        self.config = dict(camera='127.0.0.1:1', allow_insecure_camera_http=False,
            camera_tls=self.credentials.config(), rtsp='127.0.0.1:8554', stream_path='reference',
            publisher_user='publisher', publisher_token_file=str(self.root/'publisher.token'),
            ffmpeg=str(encoder), publisher=str(publisher), width=320, height=240, frame_timeout_ms=1000)

    async def asyncTearDown(self):
        if self.process and self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
            try: await asyncio.wait_for(self.process.communicate(), 6)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.communicate()
                self.fail('owned ingest shutdown deadline')
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.handlers)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.loop_errors, [])
        for path in [self.encoder_pid, self.publisher_pid]:
            if path.exists():
                with self.assertRaises(ProcessLookupError): os.kill(int(path.read_text()), 0)

    async def serve(self, *, certificate='server', mode='tls', tls12=False):
        self.assertIn(mode, ['tls', 'update', 'silent', 'cleartext'])
        context = None
        if mode in ('tls', 'update'):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if tls12: context.maximum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.root/(certificate+'.pem'), self.root/(certificate+'.key'))
            context.load_verify_locations(cafile=self.root/'ca.pem')
            context.verify_mode = ssl.CERT_REQUIRED
        async def accept(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            self.connections += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if mode not in ('tls', 'update'):
                    first = await reader.read(8192)
                    self.plaintext_http |= first.startswith(b'GET ')
                    if mode == 'cleartext':
                        writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
                        await writer.drain()
                        return
                    while await reader.read(8192): pass
                    return
                self.negotiated.add(writer.get_extra_info('ssl_object').version())
                if mode == 'update':
                    self.assertEqual(await reader.readexactly(4), b'NXU1')
                    writer.write(b'NXRD' + (64).to_bytes(4, 'big'))
                    await writer.drain()
                    length = int.from_bytes(await reader.readexactly(4), 'big')
                    self.updated = await reader.readexactly(length)
                    self.assertEqual(await reader.readexactly(4), b'DONE')
                    writer.write(b'NXOK')
                    await writer.drain()
                    self.assertEqual(await reader.readexactly(4), b'CMIT')
                    return
                request = await reader.readuntil(b'\r\n\r\n')
                self.assertTrue(request.startswith(b'GET /stream HTTP/1.1\r\n'))
                self.requests += 1
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n\r\n')
                sequence = 0
                while True:
                    sequence += 1
                    writer.write(f'--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(self.jpeg)}\r\n'
                        f'X-Sequence: {sequence}\r\nX-Capture-Ms: {sequence*100}\r\n\r\n'.encode()+self.jpeg+b'\r\n')
                    await writer.drain()
                    await asyncio.sleep(.1)
            except (ConnectionError, asyncio.CancelledError): pass
            finally:
                writer.close()
                try: await asyncio.wait_for(writer.wait_closed(), 1)
                except (OSError, asyncio.TimeoutError): pass
                self.active -= 1
                self.handlers.discard(task)
        self.server = await asyncio.start_server(accept, '127.0.0.1', 0, ssl=context, limit=8192)
        self.config['camera'] = f'127.0.0.1:{self.server.sockets[0].getsockname()[1]}'

    async def launch(self, *, check=False):
        private(self.root/'config.json', json.dumps(self.config))
        self.process = await asyncio.create_subprocess_exec(str(INGEST), str(self.root/'config.json'),
            '--require-camera-tls', *(['--check-config'] if check else []),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self.process

    async def test_authenticated_firmware_upload_does_not_start_media_workers(self):
        await self.serve(mode='update')
        image = self.root/'firmware.bin'
        payload = bytes.fromhex('3db8f396') + b'x' * 28
        private(image, payload)
        private(self.root/'config.json', json.dumps(self.config))
        self.process = await asyncio.create_subprocess_exec(str(INGEST), str(self.root/'config.json'),
            '--require-camera-tls', '--update', str(image), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = await asyncio.wait_for(self.process.communicate(), 5)
        self.assertEqual(self.process.returncode, 0, error)
        self.assertEqual(self.updated, payload)
        self.assertIn(b'installation and trial confirmation are pending', output)
        self.assertFalse(self.encoder_pid.exists())
        self.assertFalse(self.publisher_pid.exists())
        image.unlink()
        os.mkfifo(image)
        self.process = await asyncio.create_subprocess_exec(str(INGEST), str(self.root/'config.json'),
            '--update', str(image), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await asyncio.wait_for(self.process.communicate(), 5)
        self.assertEqual(self.process.returncode, 1)  # Refuse FIFO without a blocking open.

    async def test_mbed_tls_product_camera_delivers_exact_jpegs_to_rustls_ingest(self):
        if TLS_CAMERA is None:
            self.skipTest('select --camera-source for actual Mbed TLS interoperability')
        from tls_camera import TlsCamera
        source = await TlsCamera.start(TLS_CAMERA, self.root/'device',
            site='site_reference', camera='cam_reference', gateway='gw_reference')
        self.addAsyncCleanup(source.close)
        self.config.update(camera=f'127.0.0.1:{source.port}', camera_tls=source.credentials.config())
        images = [(source.directory/f'rust-frame-{i}.jpg').read_bytes() for i in [1, 2]]
        process = await self.launch()
        await self.until(lambda: self.marker.exists() and self.marker.stat().st_size >= 2 * max(map(len, images)))
        with self.marker.open('rb') as stream:
            data = stream.read(32768)
        end = data.index(b'\xff\xd9') + 2
        self.assertIn(data[:end], images)
        end2 = data.index(b'\xff\xd9', end) + 2
        self.assertIn(data[end:end2], images)
        counters = await source.command('status')
        self.assertEqual((counters['active'], counters['peak'], counters['connections']), (1, 1, 1))
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.communicate(), 6)
        self.assertEqual(process.returncode, 0)

    async def checked(self, expected=1):
        process = await self.launch(check=True)
        output = await asyncio.wait_for(process.communicate(), 3)
        self.assertEqual(process.returncode, expected)
        self.assertFalse(self.marker.exists())
        return output

    async def test_rendered_tls_credentials_stage_and_preflight_without_network(self):
        # Execute the rendered unit's staging/check commands in a disposable root;
        # this checks host file admission, not systemd sandbox behavior.
        import shlex
        self.credentials.issue('server', camera='cam_reference')
        self.credentials.issue('client', camera='cam_reference')
        enrollment = {k: v for k, v in self.credentials.config().items() if k not in ingest_service.TLS_FILES}
        enrollment['camera_id'] = 'cam_reference'
        self.config = ingest_service.configuration('127.0.0.1:1', camera_tls=enrollment)
        runtime, loaded = self.root/'runtime', self.root/'loaded'
        runtime.mkdir(mode=0o700)
        loaded.mkdir(mode=0o700)
        self.config['camera_tls'] = {k: v.replace('/run/nxrs-ingest/', str(runtime)+'/')
                                    if isinstance(v, str) else v for k, v in self.config['camera_tls'].items()}
        self.config['publisher_token_file'] = str(runtime/'publisher.token')
        private(loaded/'config', json.dumps(self.config))
        private(loaded/'publisher', '1'*64+'\n')
        for field, name in ingest_service.TLS_FILES.items():
            private(loaded/name, Path(self.credentials.config()[field]).read_bytes())
        unit = ingest_service.service(require_tls=True)
        commands = [line.split('=', 1)[1] for line in unit.splitlines() if line.startswith('ExecStartPre=')]
        for command in commands:
            command = command.replace('%d/', str(loaded)+'/').replace('/run/nxrs-ingest/', str(runtime)+'/')
            command = command.replace('/usr/local/libexec/nxrs-camera-ingest', str(INGEST))
            process = await asyncio.create_subprocess_exec(*shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _, error = await asyncio.wait_for(process.communicate(), 3)
            self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(self.requests, 0)
        self.assertFalse(self.encoder_pid.exists())
        for path in runtime.iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        (runtime/'camera-client.key').unlink()
        process = await asyncio.create_subprocess_exec(*shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await asyncio.wait_for(process.communicate(), 3)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(self.encoder_pid.exists())
        # A modified config cannot turn the TLS unit into an HTTP unit.
        del self.config['camera_tls']
        self.config['allow_insecure_camera_http'] = True
        await self.checked(1)

    async def until(self, test, seconds=4):
        deadline = asyncio.get_running_loop().time()+seconds
        while not test():
            self.assertLess(asyncio.get_running_loop().time(), deadline, 'owned fixture progress deadline')
            await asyncio.sleep(.02)

    async def test_mutual_tls_frame_delivery_tls12_and_joined_shutdown(self):
        await self.serve(tls12=True)
        await self.checked(0)
        self.assertEqual(self.requests, 0)
        process = await self.launch()
        await self.until(lambda: self.marker.exists() and self.marker.stat().st_size >= len(self.jpeg))
        self.assertTrue(self.marker.read_bytes().startswith(self.jpeg))
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual(process.returncode, 0)
        await self.until(lambda: self.active == 0)
        self.assertEqual(self.peak, 1)
        self.assertEqual(self.negotiated, {'TLSv1.2'})

    async def test_required_tls_and_identity_policy_refuse_before_source_or_encoder(self):
        original = json.dumps(self.config)
        for field, value in [('site_id', 'other'), ('camera_id', 'other'), ('gateway_id', 'other'),
                ('issuer_sha256', '0'*64), ('enrollment_generation', 2), ('client_key_file', str(self.root/'server.key'))]:
            with self.subTest(field=field):
                self.config = json.loads(original)
                self.config['camera_tls'][field] = value
                await self.checked()
        self.config = json.loads(original)
        self.config['allow_insecure_camera_http'] = True
        await self.checked()
        del self.config['camera_tls']
        await self.checked()

    async def test_expired_or_wrong_scope_leaf_and_unsafe_file_refuse(self):
        for name, args in [('expired', dict(expired=True)), ('other', dict(camera='other'))]:
            self.credentials.issue(name, **args)
            self.config['camera_tls']['camera_certificate_file'] = str(self.root/(name+'.pem'))
            await self.checked()
        self.config['camera_tls'] = self.credentials.config()
        original_crl = (self.root/'crl.pem').read_bytes()
        der = bytearray(base64.b64decode(b''.join(original_crl.splitlines()[1:-1])))
        der[-1] ^= 1
        private(self.root/'crl.pem', b'-----BEGIN X509 CRL-----\n'+base64.encodebytes(der)+b'-----END X509 CRL-----\n')
        await self.checked()
        private(self.root/'crl.pem', original_crl)
        (self.root/'client.key').chmod(0o644)
        await self.checked()
        (self.root/'client.key').chmod(0o600)
        (self.root/'linked.key').symlink_to(self.root/'client.key')
        self.config['camera_tls']['client_key_file'] = str(self.root/'linked.key')
        await self.checked()
        self.config['camera_tls'] = self.credentials.config()
        os.mkfifo(self.root/'token.fifo', 0o600)
        self.config['publisher_token_file'] = str(self.root/'token.fifo')
        await self.checked()

    async def test_unapproved_same_identity_remote_leaf_never_receives_http(self):
        self.credentials.issue('replacement')
        await self.serve(certificate='replacement')
        process = await self.launch()
        await asyncio.wait_for(process.communicate(), 4)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(self.requests, 0)
        self.assertFalse(self.marker.exists())

    async def test_revoked_client_and_camera_refuse(self):
        # Reissue the unrevoked counterpart so each refusal has one revoked role.
        for role in ['client', 'server']:
            if role == 'server':
                self.credentials.issue('client')
            self.credentials.call('ca', '-config', self.root/'ca.cnf', '-revoke', self.root/(role+'.pem'))
            generation = int((self.root/'crlnumber').read_text(), 16)
            self.credentials.crl()
            self.config['camera_tls']['enrollment_generation'] = generation
            await self.checked()

    async def test_handshake_timeout_reconnect_and_cancel_keep_one_owner(self):
        await self.serve(mode='silent')
        process = await self.launch()
        await self.until(lambda: self.connections == 2, seconds=8)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.communicate(), 4)
        self.assertEqual(process.returncode, 0)
        await self.until(lambda: self.active == 0)
        self.assertEqual(self.peak, 1)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.plaintext_http)

    async def retire_after_child_exit(self, victim):
        await self.serve()
        process = await self.launch()
        paths = [self.encoder_pid, self.publisher_pid]
        def current():
            try: return [int(p.read_text()) for p in paths]
            except (FileNotFoundError, ValueError): return None
        await self.until(lambda: current() is not None and self.marker.exists())
        pids = current()
        os.kill(pids[victim], signal.SIGKILL)
        await self.until(lambda: (now := current()) is not None and all(a != b for a, b in zip(now, pids)))
        for pid in pids:
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
        self.assertEqual(self.peak, 1)
        self.assertTrue(b'1'*64 not in self.marker.read_bytes(), 'credential reached image encoder input')
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.communicate(), 6)
        self.assertEqual(process.returncode, 0)

    async def test_encoder_exit_reaps_publisher_before_reconnect(self):
        await self.retire_after_child_exit(0)

    async def test_publisher_exit_reaps_encoder_before_reconnect(self):
        await self.retire_after_child_exit(1)

    async def test_plaintext_peer_is_terminal_without_http_fallback(self):
        await self.serve(mode='cleartext')
        process = await self.launch()
        await asyncio.wait_for(process.communicate(), 4)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(self.plaintext_http)
        self.assertFalse(self.marker.exists())

    async def test_crl_expiry_retires_live_connection_and_encoder(self):
        generation = int((self.root/'crlnumber').read_text(), 16)
        self.credentials.crl(seconds=4)
        self.config['camera_tls']['enrollment_generation'] = generation
        await self.serve()
        process = await self.launch()
        await self.until(lambda: self.marker.exists() and self.marker.stat().st_size >= len(self.jpeg))
        await asyncio.wait_for(process.communicate(), 7)
        self.assertEqual(process.returncode, 1)
        await self.until(lambda: self.active == 0)
        self.assertEqual(self.negotiated, {'TLSv1.3'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ingest', type=Path, required=True)
    parser.add_argument('--camera-source', type=Path, help='actual TLS/LiveCamera fixture executable')
    args = parser.parse_args()
    INGEST = args.ingest.resolve()
    TLS_CAMERA = args.camera_source.resolve() if args.camera_source else None
    unittest.main(argv=[sys.argv[0]], verbosity=2)
