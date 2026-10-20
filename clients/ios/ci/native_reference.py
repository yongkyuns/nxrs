#!/usr/bin/env python3
"""Synthetic Swift/libWebRTC test on a disposable hosted Mac.

Trust is scoped to the probe's URLSessions. No keychain or machine trust changes.
The app's production transport remains unchanged. All fixture files are private.
"""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def private(root, name, value):
    with open(root/name, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(value)


def checked_command(command, stage, timeout=30):
    result = subprocess.run(command, capture_output=True, timeout=timeout, umask=0o077)
    check(result.returncode == 0, f'{stage} failed (exit {result.returncode})')
    return result.stdout


def create_certificates(root):
    """One ephemeral CA, a different CA for rejection, and a one-day server leaf."""
    private(root, 'root.cnf', '[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=extensions\n'
            '[dn]\nCN=nxrs-reference-root\n[extensions]\nbasicConstraints=critical,CA:TRUE\n'
            'keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
    for name in ['root', 'wrong-root']:
        checked_command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '3',
            '-keyout', str(root/f'{name}.key'), '-out', str(root/f'{name}.pem'),
            '-config', str(root/'root.cnf')], 'reference CA generation')
    private(root, 'leaf.cnf', '[req]\nprompt=no\ndistinguished_name=dn\n'
            '[dn]\nCN=localhost\n[extensions]\nbasicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n'
            'subjectAltName=DNS:localhost\nauthorityKeyIdentifier=keyid,issuer\n')
    checked_command(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-sha256',
        '-keyout', str(root/'key.pem'), '-out', str(root/'leaf.csr'),
        '-config', str(root/'leaf.cnf')], 'reference leaf request')
    checked_command(['openssl', 'x509', '-req', '-in', str(root/'leaf.csr'), '-CA', str(root/'root.pem'),
        '-CAkey', str(root/'root.key'), '-set_serial', '2', '-days', '1', '-sha256',
        '-extfile', str(root/'leaf.cnf'), '-extensions', 'extensions',
        '-out', str(root/'cert.pem')], 'reference leaf signing')
    for name in ['root', 'wrong-root', 'cert']:
        checked_command(['openssl', 'x509', '-in', str(root/f'{name}.pem'), '-outform', 'DER',
            '-out', str(root/f'{name}.der')], 'reference DER encoding')


def wait_for_publisher(process, progress):
    """A running process or fixed sleep is not proof that RTSP publication exists."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        check(process.poll() is None, 'synthetic publisher exited before producing frames')
        with progress.open() as stream:
            text = stream.read(65536)
        frames = [int(line.split('=', 1)[1]) for line in text.splitlines()
                  if line.startswith('frame=') and line.split('=', 1)[1].strip().isdigit()]
        if frames and max(frames) >= 5:
            print('NATIVE REFERENCE SETUP: authenticated publisher produced frames', flush=True)
            return
        time.sleep(0.1)
    raise AssertionError('synthetic publisher readiness deadline exceeded')


def cleanup_resources(server, processes, logs):
    """Attempt every cleanup; report failure without leaking paths or credentials."""
    failures = []
    if server:
        for action in [server.shutdown, server.server_close]:
            try:
                action()
            except Exception:
                failures.append('HTTPS fixture')
    for child in reversed(processes):
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=8)
        except Exception:
            failures.append('child process')
    for log in logs:
        try:
            log.close()
        except Exception:
            failures.append('private log')
    check(not failures, 'reference cleanup failed: ' + ', '.join(sorted(set(failures))))


def main(gateway, mediamtx, probe):
    check(os.environ.get('GITHUB_ACTIONS') == 'true'
          and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted',
          'native reference requires an ephemeral GitHub-hosted runner')
    with tempfile.TemporaryDirectory(prefix='nxrs-native-') as directory:
        root = Path(directory)
        ports = set()
        def port():
            while True:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    value = sock.getsockname()[1]
                if value not in ports:
                    ports.add(value)
                    return value
        api, auth, rtsp, media, tls = [port() for _ in range(5)]
        with socket.socket(type=socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1', 0))
            ice = sock.getsockname()[1]
        viewer, publisher = secrets.token_hex(32), secrets.token_hex(32)
        private(root, 'viewer.token', viewer)
        private(root, 'encoder.progress', '')
        config = {'listen': f'127.0.0.1:{api}', 'auth_listen': f'127.0.0.1:{auth}',
            'grant_ttl_seconds': 60, 'max_grants': 16, 'media_origins': [f'https://localhost:{media}'],
            'cameras': [{'camera_id': 'cam_reference', 'gateway_id': 'gw_reference', 'site_id': 'site_reference',
                'name': 'SYNTHETIC NATIVE TEST', 'source': 'synthetic', 'stream_path': 'reference',
                'whep_endpoint': f'https://localhost:{media}/reference/whep'}],
            'viewers': [{'viewer_id': 'viewer_reference', 'site_id': 'site_reference', 'camera_ids': ['cam_reference'],
                'token_sha256': hashlib.sha256(viewer.encode()).hexdigest()}],
            'publishers': [{'username': 'publisher_reference', 'camera_id': 'cam_reference',
                'token_sha256': hashlib.sha256(publisher.encode()).hexdigest()}]}
        private(root, 'config.json', json.dumps(config))
        private(root, 'probe.json', json.dumps({'gateway': f'https://localhost:{tls}',
            'mediaOrigin': f'https://localhost:{media}', 'credentialFile': str(root/'viewer.token'),
            'cameraID': 'cam_reference', 'rootCertificate': str(root/'root.der'),
            'wrongRootCertificate': str(root/'wrong-root.der'), 'leafCertificate': str(root/'cert.der')}))
        create_certificates(root)
        print('NATIVE REFERENCE SETUP: private CA and signed leaf; no machine trust changes', flush=True)
        private(root, 'mediamtx.yml', json.dumps({
            'logLevel': 'warn', 'authMethod': 'http',
            'authHTTPAddress': f'http://127.0.0.1:{auth}/internal/mediamtx/auth', 'authHTTPExclude': [],
            'api': False, 'metrics': False, 'pprof': False, 'playback': False, 'rtmp': False, 'hls': False,
            'srt': False, 'moq': False, 'rtsp': True, 'rtspAddress': f'127.0.0.1:{rtsp}',
            'rtspTransports': ['tcp'], 'rtspAuthMethods': ['basic'],
            'webrtc': True, 'webrtcAddress': f'127.0.0.1:{media}', 'webrtcEncryption': True,
            'webrtcServerKey': str(root/'key.pem'), 'webrtcServerCert': str(root/'cert.pem'),
            'webrtcLocalUDPAddress': f'127.0.0.1:{ice}', 'webrtcIPsFromInterfaces': False,
            'webrtcAdditionalHosts': ['127.0.0.1'], 'webrtcICEServers2': [],
            'paths': {'reference': {'source': 'publisher', 'overridePublisher': False}}}))
        processes, logs = [], []
        server = None
        trap_seen = threading.Event()
        def launch(command, name):
            log = open(root/name, 'wb'); os.chmod(root/name, 0o600); logs.append(log)
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            processes.append(child)
            return child
        try:
            launch([str(gateway), str(root/'config.json')], 'control.log')
            launch([str(mediamtx), str(root/'mediamtx.yml')], 'mediamtx.log')
            class Proxy(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def forward(self):
                    if self.path == '/_fixture/redirect':
                        self.send_response(302)
                        self.send_header('Location', f'https://localhost:{tls}/_fixture/trap')
                        self.send_header('Content-Length', '0'); self.end_headers(); return
                    if self.path == '/_fixture/trap':
                        trap_seen.set(); self.send_error(500); return
                    if self.path in ['/_fixture/oversize', '/_fixture/streamed-oversize']:
                        self.send_response(200)
                        if self.path == '/_fixture/oversize':
                            self.send_header('Content-Length', '1048577')
                        self.end_headers()
                        try:
                            self.wfile.write(b'x' * 1_048_577)
                        except (OSError, ssl.SSLError):
                            pass
                        return
                    if not (self.path == '/healthz' or self.path.startswith('/v1/')):
                        self.send_error(404); return
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 16384:
                        self.send_error(413); return
                    connection = http.client.HTTPConnection('127.0.0.1', api, timeout=10)
                    try:
                        connection.request(self.command, self.path, body=self.rfile.read(length),
                            headers={k: v for k, v in self.headers.items()
                                if k.lower() in ['authorization', 'content-type']})
                        response = connection.getresponse()
                        body = response.read(1_048_577)
                        self.send_response(response.status)
                        for k, v in response.getheaders():
                            if k.lower() in ['content-type', 'cache-control', 'www-authenticate']:
                                self.send_header(k, v)
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers(); self.wfile.write(body)
                    finally:
                        connection.close()
                do_GET = forward
                do_POST = forward
            server = ThreadingHTTPServer(('127.0.0.1', tls), Proxy)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root/'cert.pem', root/'key.pem')
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            trust = ssl.create_default_context(cafile=str(root/'root.pem'))
            for _ in range(60):
                try:
                    with urllib.request.urlopen(f'https://localhost:{tls}/healthz', context=trust, timeout=1) as response:
                        if response.status == 200: break
                except (OSError, ValueError):
                    time.sleep(0.1)
            else:
                raise AssertionError('reference control service not ready')
            encoder = launch(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-progress', str(root/'encoder.progress'), '-stats_period', '0.1',
                '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=10', '-an', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-tune', 'zerolatency', '-profile:v', 'baseline', '-bf', '0',
                '-g', '10', '-pix_fmt', 'yuv420p', '-f', 'rtsp', '-rtsp_transport', 'tcp',
                f'rtsp://publisher_reference:{publisher}@127.0.0.1:{rtsp}/reference'], 'encoder.log')
            wait_for_publisher(encoder, root/'encoder.progress')
            for trial in range(2):
                child = launch([str(probe), str(root/'probe.json')], f'probe-{trial}.log')
                code = child.wait(timeout=90)
                text = (root/f'probe-{trial}.log').read_text(errors='replace')
                results = [line for line in text.splitlines() if line.startswith('NATIVE PLAYBACK PASS:')]
                tls_results = [line for line in text.splitlines() if line.startswith('NATIVE TLS PASS:')]
                failures = [line for line in text.splitlines() if line.startswith('NATIVE PLAYBACK FAILED:')]
                for line in failures: print(line, flush=True)
                check(code == 0, 'native Swift playback probe failed')
                check(len(results) == len(tls_results) == 1, 'native probe missing TLS or playback evidence')
                check(not trap_seen.is_set(), 'reference transport followed a redirect')
                print(tls_results[0], flush=True)
                print(results[0], flush=True)
        finally:
            cleanup_resources(server, processes, logs)
    # TemporaryDirectory has removed keys/logs too. Never report PASS before cleanup.
    check(not Path(directory).exists(), 'private fixture directory remains')
    print('NATIVE REFERENCE PASS: TLS controls, two native macOS sessions, private fixture cleanup; not physical iPhone/VPN', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', type=Path, required=True)
    parser.add_argument('--mediamtx', type=Path, required=True)
    parser.add_argument('--probe', type=Path, required=True)
    args = parser.parse_args()
    try:
        main(args.gateway.resolve(), args.mediamtx.resolve(), args.probe.resolve())
    except Exception as error:
        print('NATIVE REFERENCE FAILED:', str(error) if isinstance(error, AssertionError) else type(error).__name__, flush=True)
        raise SystemExit(1) from None
