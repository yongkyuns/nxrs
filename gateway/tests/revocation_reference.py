#!/usr/bin/env python3
"""Real synthetic media revocation; loopback Linux only, not phone/VPN qualification.

All credentials, raw subprocess output and temporary certificates are private and
removed. No global certificate trust is changed. The management proxy below is
only an outage-injection fixture, not deployment software.
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import socket
import ssl
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web
from aiortc import RTCPeerConnection, RTCConfiguration, RTCSessionDescription, RTCRtpSender


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def private_write(path, contents):
    with open(path, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        stream.write(contents)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Reader:
    def __init__(self):
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.count = 0
        self.previous_pts = None
        self.task = None
        self.session_id = None
        self.session_url = None
        transceiver = self.peer.addTransceiver('video', direction='recvonly')
        transceiver.setCodecPreferences([c for c in RTCRtpSender.getCapabilities('video').codecs
                                        if c.mimeType.lower() == 'video/h264'])

        @self.peer.on('track')
        def track_received(track):
            if track.kind == 'video':
                self.task = asyncio.create_task(self.consume(track))

    async def consume(self, track):
        try:
            while True:
                frame = await track.recv()
                require((frame.width, frame.height) == (320, 240), 'decoded dimensions')
                if frame.pts != self.previous_pts:
                    self.count += 1
                    self.previous_pts = frame.pts
        except asyncio.CancelledError:
            raise
        except Exception:
            return  # A revoked remote peer ends reception. Progress assertions detect other failures.

    async def close(self):
        await self.peer.close()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


async def exercise(gateway, mediamtx):
    with tempfile.TemporaryDirectory(prefix='nxrs-revocation-') as directory:
        root = Path(directory)
        processes, handles, readers = [], [], []
        relay = None
        control = None
        used = set()
        def port():
            while True:
                with socket.socket() as s:
                    s.bind(('127.0.0.1', 0))
                    value = s.getsockname()[1]
                if value not in used:
                    used.add(value)
                    return value
        api, auth, media, rtsp, management, proxy, ice = [port() for _ in range(7)]
        tokens = {name: secrets.token_hex(32) for name in ['a', 'b', 'c', 'publisher', 'management']}
        base = f'http://127.0.0.1:{api}'
        internal = f'http://127.0.0.1:{auth}'
        media_origin = f'https://127.0.0.1:{media}'
        admin_origin = f'http://127.0.0.1:{management}'
        state_path = root / 'revocations.json'
        private_write(state_path, json.dumps({'version': 1, 'revoked': []}))
        config = {
            'listen': f'127.0.0.1:{api}', 'auth_listen': f'127.0.0.1:{auth}',
            'credential_window': {'not_before': int(time.time())-1, 'expires_at': int(time.time())+3600},
            'grant_ttl_seconds': 60, 'max_grants': 128, 'media_origins': [media_origin],
            'cameras': [dict(camera_id='cam_reference', gateway_id='gw_reference', site_id='site_reference',
                            name='SYNTHETIC revocation reference', source='synthetic', stream_path='reference',
                            whep_endpoint=f'{media_origin}/reference/whep')],
            'viewers': [dict(viewer_id=f'viewer_{name}', site_id='site_reference', camera_ids=['cam_reference'],
                             token_sha256=digest(tokens[name])) for name in ['a', 'b', 'c']],
            'publishers': [dict(username='publisher', camera_id='cam_reference', token_sha256=digest(tokens['publisher']))],
        }
        private_write(root / 'config.json', json.dumps(config))
        private_write(root / 'security.json', json.dumps({
            'revocations_file': str(state_path), 'media_api': f'127.0.0.1:{proxy}',
            'media_api_token': tokens['management'],
        }))
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(root/'key.pem'), '-out', str(root/'cert.pem'), '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        os.chmod(root/'key.pem', 0o600)
        tls = ssl.create_default_context(cafile=str(root/'cert.pem'))
        mtx = {
            'logLevel': 'warn', 'authMethod': 'http',
            'authHTTPAddress': f'{internal}/internal/mediamtx/auth', 'authHTTPExclude': [],
            'api': True, 'apiAddress': f'127.0.0.1:{management}',
            'metrics': False, 'pprof': False, 'playback': False,
            'rtsp': True, 'rtspAddress': f'127.0.0.1:{rtsp}', 'rtspTransports': ['tcp'], 'rtspAuthMethods': ['basic'],
            'rtmp': False, 'hls': False, 'srt': False, 'moq': False,
            'webrtc': True, 'webrtcAddress': f'127.0.0.1:{media}', 'webrtcEncryption': True,
            'webrtcServerKey': str(root/'key.pem'), 'webrtcServerCert': str(root/'cert.pem'),
            'webrtcLocalUDPAddress': f'127.0.0.1:{ice}', 'webrtcIPsFromInterfaces': False,
            'webrtcAdditionalHosts': ['127.0.0.1'], 'webrtcICEServers2': [],
            'paths': {'reference': {'source': 'publisher', 'overridePublisher': False, 'maxReaders': 8}},
        }
        private_write(root/'mediamtx.yml', json.dumps(mtx))
        available = {'management': True}
        def launch(command, name):
            log = open(root / name, 'wb')
            os.chmod(root / name, 0o600)
            handles.append(log)
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            processes.append(child)
            return child
        def start_control():
            return launch([str(gateway), str(root/'config.json'), '--security', str(root/'security.json')],
                          f'control-{len(processes)}.log')
        async def stop(child):
            child.terminate()
            await asyncio.to_thread(child.wait, timeout=10)
        async def until(predicate, message, timeout=12):
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                if await predicate():
                    return
                await asyncio.sleep(0.15)
            raise AssertionError(message)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as http:
            async def request(method, url, token=None, body=None):
                headers = {'Authorization': f'Bearer {token}'} if token else {}
                async with http.request(method, url, headers=headers, json=body, allow_redirects=False, ssl=tls) as response:
                    data = await response.read()
                    return response.status, json.loads(data) if data else None
            async def status_ready(expected=True):
                try:
                    status, body = await request('GET', internal+'/internal/viewers/viewer_c/revocation')
                    return status == 200 and body['media_control_ready'] is expected
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    return False
            async def listed():
                status, body = await request('GET', admin_origin+'/v3/webrtcsessions/list', tokens['management'])
                require(status == 200, 'authenticated session observation')
                return {item['id'] for item in body['items']}
            async def absent(reader):
                return reader.session_id not in await listed()
            async def progress(reader, target):
                return reader.count >= target
            async def connect(name):
                reader = Reader()
                readers.append(reader)
                await reader.peer.setLocalDescription(await reader.peer.createOffer())
                status, grant = await request('POST', base+'/v1/cameras/cam_reference/playback-grants', tokens[name], {})
                require(status == 201, 'grant creation for non-revoked viewer')
                async with http.post(grant['endpoint'], data=reader.peer.localDescription.sdp,
                        headers={'Authorization': 'Bearer '+grant['access_token'], 'Content-Type': 'application/sdp'},
                        ssl=tls, allow_redirects=False) as response:
                    require(response.status == 201, f'authorized WHEP session creation (HTTP {response.status})')
                    answer = await response.text()
                    reader.session_id = response.headers['ID']
                    reader.session_url = urljoin(grant['endpoint'], response.headers['Location'])
                    require(urlsplit(reader.session_url).netloc == urlsplit(grant['endpoint']).netloc, 'same-origin session')
                await reader.peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
                await until(lambda: progress(reader, 5), 'advancing H264 frames')
                return reader
            async def frozen(reader):
                await asyncio.sleep(1.5)  # The continuously draining reader consumes any packets already buffered.
                count = reader.count
                await asyncio.sleep(1)
                require(reader.count == count, 'revoked reader must stop receiving frames')
            try:
                async def forward(req):
                    if not available['management']:
                        return web.json_response({'status': 'unavailable'}, status=503)
                    try:
                        async with http.request(req.method, admin_origin+str(req.rel_url),
                                headers={'Authorization': req.headers.get('Authorization', '')},
                                data=await req.read(), allow_redirects=False) as response:
                            return web.Response(status=response.status, body=await response.read(), content_type='application/json')
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        # Startup/outage is expected: never print a request or raw exception containing credentials.
                        return web.json_response({'status': 'unavailable'}, status=503)
                application = web.Application(client_max_size=1024)
                application.router.add_route('*', '/{path:.*}', forward)
                relay = web.AppRunner(application, access_log=None)
                await relay.setup()
                await web.TCPSite(relay, '127.0.0.1', proxy).start()
                launch([str(mediamtx), str(root/'mediamtx.yml')], 'mediamtx.log')
                control = start_control()
                await until(status_ready, 'media controller startup')
                print('PASS: strict mode becomes ready through authenticated private management API', flush=True)
                status, _ = await request('GET', admin_origin+'/v3/webrtcsessions/list', tokens['a'])
                require(status == 401, 'viewer cannot use management API')
                encoder = launch(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-re', '-f', 'lavfi',
                    '-i', 'testsrc2=size=320x240:rate=10', '-an', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-tune', 'zerolatency', '-profile:v', 'baseline', '-bf', '0', '-g', '10', '-pix_fmt', 'yuv420p',
                    '-f', 'rtsp', '-rtsp_transport', 'tcp',
                    f"rtsp://publisher:{tokens['publisher']}@127.0.0.1:{rtsp}/reference"], 'encoder.log')
                async def source_ready():
                    require(encoder.poll() is None, 'synthetic publisher stays running before WHEP admission')
                    status, body = await request('GET', admin_origin+'/v3/paths/get/reference', tokens['management'])
                    require(status in (200,404), 'authenticated source readiness observation')
                    return status == 200 and body.get('ready') is True
                # A process launch or elapsed delay does not establish an active
                # source. Poll only the idempotent management read; never retry
                # grant creation or a WHEP session after an admission failure.
                await until(source_ready, 'actual publisher readiness before WHEP session creation')
                a, b = await connect('a'), await connect('b')
                print('PASS: two independent authorized viewers decode advancing synthetic video', flush=True)
                status, _ = await request('POST', internal+'/internal/viewers/viewer_a/revoke', body={})
                require(status == 202, 'durably accepted revocation')
                await until(lambda: absent(a), 'revoked session disappears from MediaMTX')
                await frozen(a)
                target = b.count + 5
                await until(lambda: progress(b, target), 'unrelated viewer remains live')
                require(b.session_id in await listed() and encoder.poll() is None, 'other viewer and publisher retained')
                print('PASS: active revoked viewer stops while unrelated viewer and publisher continue', flush=True)
                status, _ = await request('GET', base+'/v1/cameras', tokens['a'])
                require(status == 403, 'revoked account denied')
                available['management'] = False
                await until(lambda: status_ready(False), 'management outage observed')
                status, _ = await request('POST', base+'/v1/cameras/cam_reference/playback-grants', tokens['c'], {})
                require(status == 503, 'new viewing fails closed during management outage')
                status, _ = await request('POST', internal+'/internal/viewers/viewer_b/revoke', body={})
                require(status == 202, 'denial persists despite pending remote termination')
                status, observed = await request('GET', internal+'/internal/viewers/viewer_b/revocation')
                require(status == 200 and observed['revoked'] and not observed['media_control_ready']
                        and observed['tracked_sessions'] == 1, 'pending termination is observable')
                available['management'] = True
                await until(lambda: absent(b), 'queued revocation recovers after management outage')
                await frozen(b)
                await until(status_ready, 'management recovery')
                print('PASS: outage denies new admission, retains durable denial, and retries active termination', flush=True)
                c = await connect('c')
                await stop(control)
                control = start_control()
                await until(status_ready, 'controller restart')
                await until(lambda: absent(c), 'pre-restart orphan retired')
                for name in ['a', 'b']:
                    status, _ = await request('GET', base+'/v1/cameras', tokens[name])
                    require(status == 403, 'revocation survives control restart')
                c2 = await connect('c')
                require(encoder.poll() is None, 'publisher survives control restart')
                print('PASS: restart restores denials, retires orphan viewing, and permits healthy reconnection', flush=True)
                await stop(control)
                saved = state_path.read_bytes()
                state_path.write_text('{broken')
                broken = start_control()
                await asyncio.to_thread(broken.wait, timeout=10)
                require(broken.returncode != 0, 'corrupt state must refuse startup')
                state_path.write_bytes(saved)
                control = start_control()
                await until(status_ready, 'recovery from operator-restored valid state')
                await until(lambda: absent(c2), 'restored controller retires second orphan')
                require(set(json.loads(state_path.read_text())['revoked']) == {'viewer_a', 'viewer_b'}, 'durable denial contents')
                print('PASS: corrupt state fails closed without erasing prior revocations', flush=True)
            finally:
                cleanup_failed = False
                for reader in readers:
                    try:
                        await reader.close()
                    except Exception:
                        cleanup_failed = True
                if relay is not None:
                    try:
                        await relay.cleanup()
                    except Exception:
                        cleanup_failed = True
                for child in reversed(processes):
                    if child.poll() is None:
                        child.terminate()
                        try:
                            await asyncio.to_thread(child.wait, timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                            cleanup_failed = True
                for handle in handles:
                    handle.close()
                require(not cleanup_failed, 'complete fixture cleanup')
        print('REVOCATION REFERENCE PASS: real synthetic H264 and persistent application denial; Linux loopback only', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gateway', required=True, type=Path)
    parser.add_argument('--mediamtx', required=True, type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(exercise(args.gateway.resolve(), args.mediamtx.resolve()))
    except Exception as error:
        print('REVOCATION REFERENCE FAILED:', str(error) if isinstance(error, AssertionError) else type(error).__name__, flush=True)
        raise SystemExit(1) from None
