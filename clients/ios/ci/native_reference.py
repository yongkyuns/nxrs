#!/usr/bin/env python3
"""Actual Swift/libWebRTC decoding of synthetic media on an ephemeral hosted Mac.

Only this test's one-use certificate is trusted and then removed. Personal and
self-hosted machines are refused. Raw logs, credentials and keys stay private.
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


def checked_command(command, stage, timeout=30):
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    check(result.returncode == 0, f'{stage} failed (exit {result.returncode})')
    return result.stdout


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


def remove_test_trust(certificate, fingerprint):
    """Attempt both removals even if trustd stalls. Never hide cleanup failure."""
    commands = [
        ('trust removal', ['sudo', '-n', 'security', 'remove-trusted-cert', '-d', str(certificate)]),
        ('certificate removal', ['sudo', '-n', 'security', 'delete-certificate', '-Z', fingerprint,
                                 '/Library/Keychains/System.keychain']),
    ]
    failures = []
    for stage, command in commands:
        completed = False
        # A busy hosted trustd can exceed 20 s. Keep each operation bounded and
        # retry once; the cleanup remains inside the disposable runner only.
        for attempt in range(2):
            try:
                result = subprocess.run(command, capture_output=True, timeout=60)
                if result.returncode == 0:
                    completed = True
                    break
                print(f'NATIVE REFERENCE CLEANUP: {stage} exit {result.returncode}', flush=True)
            except subprocess.TimeoutExpired:
                print(f'NATIVE REFERENCE CLEANUP: {stage} timed out on attempt {attempt + 1}', flush=True)
        if not completed:
            failures.append(stage)
    check(not failures, 'temporary trust cleanup failed: ' + ', '.join(failures))
    print('NATIVE REFERENCE CLEANUP: certificate trust and certificate removed', flush=True)


def main(gateway, mediamtx, probe):
    check(os.environ.get('GITHUB_ACTIONS') == 'true'
          and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted',
          'certificate trust test requires an ephemeral GitHub-hosted runner')
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
        def private(name, value):
            with open(root/name, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                stream.write(value)
        private('viewer.token', viewer)
        private('encoder.progress', '')
        config = {'listen': f'127.0.0.1:{api}', 'auth_listen': f'127.0.0.1:{auth}',
            'grant_ttl_seconds': 60, 'max_grants': 16, 'media_origins': [f'https://localhost:{media}'],
            'cameras': [{'camera_id': 'cam_reference', 'gateway_id': 'gw_reference', 'site_id': 'site_reference',
                'name': 'SYNTHETIC NATIVE TEST', 'source': 'synthetic', 'stream_path': 'reference',
                'whep_endpoint': f'https://localhost:{media}/reference/whep'}],
            'viewers': [{'viewer_id': 'viewer_reference', 'site_id': 'site_reference', 'camera_ids': ['cam_reference'],
                'token_sha256': hashlib.sha256(viewer.encode()).hexdigest()}],
            'publishers': [{'username': 'publisher_reference', 'camera_id': 'cam_reference',
                'token_sha256': hashlib.sha256(publisher.encode()).hexdigest()}]}
        private('config.json', json.dumps(config))
        private('probe.json', json.dumps({'gateway': f'https://localhost:{tls}',
            'mediaOrigin': f'https://localhost:{media}', 'credentialFile': str(root/'viewer.token'),
            'cameraID': 'cam_reference'}))
        private('openssl.cnf', '[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=extensions\n'
            '[dn]\nCN=nxrs-native-test\n[extensions]\n'
            'subjectAltName=DNS:localhost,IP:127.0.0.1\n'
            'basicConstraints=critical,CA:TRUE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n'
            'extendedKeyUsage=serverAuth\n')
        checked_command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '1',
            '-keyout', str(root/'key.pem'), '-out', str(root/'cert.pem'),
            '-config', str(root/'openssl.cnf')], 'test certificate generation')
        os.chmod(root/'key.pem', 0o600)
        fingerprint = checked_command(['openssl', 'x509', '-in', str(root/'cert.pem'),
            '-noout', '-fingerprint', '-sha1'], 'test certificate fingerprint').decode().strip().split('=')[1].replace(':', '')
        print('NATIVE REFERENCE SETUP: certificate generated', flush=True)
        private('mediamtx.yml', json.dumps({
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
        trust_attempted = False
        def launch(command, name):
            log = open(root/name, 'wb'); os.chmod(root/name, 0o600); logs.append(log)
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            processes.append(child)
            return child
        try:
            trust_attempted = True
            checked_command(['sudo', '-n', 'security', 'add-trusted-cert', '-d', '-r', 'trustRoot',
                '-k', '/Library/Keychains/System.keychain', str(root/'cert.pem')], 'temporary certificate trust', 60)
            print('NATIVE REFERENCE SETUP: temporary certificate trust installed', flush=True)
            launch([str(gateway), str(root/'config.json')], 'control.log')
            launch([str(mediamtx), str(root/'mediamtx.yml')], 'mediamtx.log')
            class Proxy(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def forward(self):
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
            trust = ssl.create_default_context(cafile=str(root/'cert.pem'))
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
                code = child.wait(timeout=60)
                text = (root/f'probe-{trial}.log').read_text(errors='replace')
                results = [line for line in text.splitlines() if line.startswith('NATIVE PLAYBACK PASS:')]
                failures = [line for line in text.splitlines() if line.startswith('NATIVE PLAYBACK FAILED:')]
                for line in failures: print(line, flush=True)
                check(code == 0, 'native Swift playback probe failed')
                check(len(results) == 1, 'native probe did not record decode/cleanup evidence')
                print(results[0], flush=True)
        finally:
            # A process cleanup error must not skip certificate cleanup.
            try:
                if server:
                    server.shutdown(); server.server_close()
                for child in reversed(processes):
                    if child.poll() is None:
                        child.terminate()
                        try: child.wait(timeout=8)
                        except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=8)
                for log in logs: log.close()
            finally:
                if trust_attempted:
                    remove_test_trust(root/'cert.pem', fingerprint)
        # Overall PASS requires cleanup to succeed, not just decoded frames.
        print('NATIVE REFERENCE PASS: two independent native macOS sessions; not physical iPhone/VPN', flush=True)


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
