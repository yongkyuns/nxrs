#!/usr/bin/env python3
"""Real process/TLS/WHEP/decoded-frame test. Synthetic source only; no iPhone/VPN claim.

Requires a compiled gateway, verified MediaMTX binary, ffmpeg/ffprobe/openssl,
and media-requirements.txt. Secrets and raw process logs stay in a private
TemporaryDirectory and are removed even on failure; never upload that directory.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCRtpSender


def free_port(udp=False):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private_write(path, text):
    with open(path, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        f.write(text)


def require(condition, description):
    if not condition:
        raise AssertionError(description)  # Never include response bodies or credentials.


async def exercise(gateway, mediamtx):
    checks = []
    def passed(name):
        checks.append(name)
        print('PASS:', name, flush=True)

    with tempfile.TemporaryDirectory(prefix='nxrs-media-') as directory:
        root = Path(directory)
        ports = set()
        def port():
            while True:
                p = free_port()
                if p not in ports:
                    ports.add(p)
                    return p
        api, auth, rtsp, media, proxy = [port() for _ in range(5)]
        ice = free_port(udp=True)
        account, second, publisher = [secrets.token_hex(32) for _ in range(3)]
        media_origin = f'https://127.0.0.1:{media}'
        config = {
            'listen': f'127.0.0.1:{api}', 'auth_listen': f'127.0.0.1:{auth}',
            'grant_ttl_seconds': 5, 'max_grants': 3, 'media_origins': [media_origin],
            'cameras': [dict(camera_id=f'cam_{name}', gateway_id='gw_reference', site_id='site_reference',
                             name=f'SYNTHETIC {name}', source='synthetic', stream_path=name,
                             whep_endpoint=f'{media_origin}/{name}/whep') for name in ['reference', 'other']],
            'viewers': [dict(viewer_id='viewer_one', site_id='site_reference', camera_ids=['cam_reference'], token_sha256=digest(account)),
                        dict(viewer_id='viewer_two', site_id='site_reference', camera_ids=['cam_other'], token_sha256=digest(second))],
            'publishers': [dict(username='publisher_one', camera_id='cam_reference', token_sha256=digest(publisher))]
        }
        private_write(root/'config.json', json.dumps(config))
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(root/'key.pem'), '-out', str(root/'cert.pem'), '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        os.chmod(root/'key.pem', 0o600)
        client_tls = ssl.create_default_context(cafile=str(root/'cert.pem'))
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(root/'cert.pem', root/'key.pem')
        # JSON scalars/objects are valid YAML values; paths/secrets are not interpolated into commands.
        mtx = {'logLevel':'warn', 'authMethod':'http',
               'authHTTPAddress':f'http://127.0.0.1:{auth}/internal/mediamtx/auth', 'authHTTPExclude':[],
               'api':False, 'metrics':False, 'pprof':False, 'playback':False,
               'rtsp':True, 'rtspAddress':f'127.0.0.1:{rtsp}', 'rtspTransports':['tcp'], 'rtspAuthMethods':['basic'],
               'rtmp':False, 'hls':False, 'srt':False, 'moq':False,
               'webrtc':True, 'webrtcAddress':f'127.0.0.1:{media}', 'webrtcEncryption':True,
               'webrtcServerKey':str(root/'key.pem'), 'webrtcServerCert':str(root/'cert.pem'),
               'webrtcLocalUDPAddress':f'127.0.0.1:{ice}', 'webrtcIPsFromInterfaces':False,
               'webrtcAdditionalHosts':['127.0.0.1'], 'webrtcICEServers2':[],
               'paths':{'reference':{'source':'publisher','overridePublisher':False}, 'other':{'source':'publisher'}}}
        private_write(root/'mediamtx.yml', json.dumps(mtx))
        processes, files = [], []
        peer = None
        runner = None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            def launch(command, logname):
                f = open(root/logname, 'wb'); os.chmod(root/logname, 0o600); files.append(f)
                process = subprocess.Popen(command, stdout=f, stderr=subprocess.STDOUT)
                processes.append(process)
                return process
            def headers(token):
                return {'Authorization':f'Bearer {token}'} if token else {}
            base = f'https://127.0.0.1:{proxy}'
            async def request(method, path, token=None, body=None):
                async with http.request(method, base+path, headers=headers(token), json=body,
                                        ssl=client_tls, allow_redirects=False) as r:
                    data = await r.read()
                    require(r.headers.get('Cache-Control') == 'no-store', 'control no-store header')
                    return r.status, json.loads(data) if data else None
            async def grant(token=account, camera='cam_reference'):
                status, data = await request('POST', f'/v1/cameras/{camera}/playback-grants', token, {})
                require(status == 201, 'playback grant creation')
                require(data['camera_id'] == camera and data['protocol'] == 'whep', 'grant scope')
                return data
            async def media_post(endpoint, token, sdp):
                async with http.post(endpoint, data=sdp,
                    headers={**headers(token), 'Content-Type':'application/sdp'}, ssl=client_tls,
                    allow_redirects=False) as r:
                    return r.status, await r.text(), r.headers.get('Location')
            try:
                control = launch([str(gateway), str(root/'config.json')], 'control.log')
                launch([str(mediamtx), str(root/'mediamtx.yml')], 'mediamtx.log')
                # Test-only TLS reverse proxy: deliberately exposes only the public control routes.
                async def forward(req):
                    if not (req.path == '/healthz' or req.path.startswith('/v1/')):
                        return web.Response(status=404, headers={'Cache-Control':'no-store'})
                    async with http.request(req.method, f'http://127.0.0.1:{api}'+req.rel_url.path_qs,
                                            data=await req.read(), headers={k:v for k,v in req.headers.items()
                                                if k.lower() in ['authorization','content-type']}, allow_redirects=False) as r:
                        return web.Response(status=r.status, body=await r.read(), headers={k:v for k,v in r.headers.items()
                                             if k.lower() in ['content-type','cache-control','www-authenticate']})
                app=web.Application(client_max_size=32*1024); app.router.add_route('*','/{path:.*}',forward)
                runner=web.AppRunner(app, access_log=None); await runner.setup()
                await web.TCPSite(runner,'127.0.0.1',proxy,ssl_context=server_tls).start()
                for _ in range(80):
                    try:
                        status,_=await request('GET','/healthz')
                        if status==200: break
                    except (aiohttp.ClientError, json.JSONDecodeError): pass
                    await asyncio.sleep(0.1)
                else: raise AssertionError('gateway failed to start')
                passed('real Rust service behind verified HTTPS')
                try:
                    async with http.get(base+'/healthz') as r:
                        raise AssertionError('untrusted TLS unexpectedly accepted')
                except aiohttp.ClientConnectorCertificateError: pass
                passed('untrusted server certificate rejected')
                status,_=await request('GET','/v1/cameras'); require(status==401,'missing account denial')
                status,_=await request('GET','/v1/cameras',publisher); require(status==401,'publisher cannot list cameras')
                status,catalog=await request('GET','/v1/cameras',account)
                require(status==200 and len(catalog['cameras'])==1,'filtered catalog')
                require(catalog['cameras'][0]['source']=='synthetic' and catalog['cameras'][0]['availability']=='unknown','honest source/readiness')
                status,_=await request('POST','/v1/cameras/cam_other/playback-grants',account,{})
                require(status==404,'cross-camera grant denied')
                status,_=await request('POST','/v1/cameras/cam_reference/playback-grants',second,{})
                require(status==404,'other viewer denied')
                status,_=await request('GET','/internal/mediamtx/auth',account); require(status==404,'internal route isolation')
                status,_=await request('GET','/v1/cameras?token=forbidden',account); require(status==400,'query credentials refused')
                status,_=await request('POST','/v1/cameras/cam_reference/playback-grants',account,{'unexpected':True})
                require(status==400,'unknown grant request fields rejected')
                status,_=await request('POST','/v1/cameras/cam_reference/playback-grants',account,{'padding':'x'*20000})
                require(status==400,'oversized body rejected')
                passed('viewer/publisher separation, camera scope, request bounds and internal route isolation')
                grants=[await grant() for _ in range(3)]
                require(len({g['access_token'] for g in grants})==3,'independent random grants')
                status,_=await request('POST','/v1/cameras/cam_reference/playback-grants',account,{})
                require(status==429,'grant capacity bound')
                await asyncio.sleep(5.2)
                passed('bounded grant store and expiration cleanup')
                # A finite wrong-credential publication must fail through real MediaMTX -> Rust auth.
                async def rejected_publish(password):
                    command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=320x240:rate=10',
                        '-t','0.2','-an','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-bf','0','-pix_fmt','yuv420p',
                        '-f','rtsp','-rtsp_transport','tcp',f'rtsp://publisher_one:{password}@127.0.0.1:{rtsp}/reference']
                    p=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                    try: code=await asyncio.wait_for(p.wait(),15)
                    except BaseException:
                        p.kill(); await p.wait(); raise
                    require(code!=0,'unauthorized publish rejected')
                await rejected_publish(account)
                passed('real RTSP publisher authorization rejects account credential')
                launch(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-re','-f','lavfi','-i',
                    'testsrc2=size=320x240:rate=10','-an','-c:v','libx264','-preset','ultrafast','-tune','zerolatency',
                    '-profile:v','baseline','-bf','0','-g','10','-pix_fmt','yuv420p','-f','rtsp','-rtsp_transport','tcp',
                    f'rtsp://publisher_one:{publisher}@127.0.0.1:{rtsp}/reference'],'encoder.log')
                await asyncio.sleep(1.5)
                peer=RTCPeerConnection(RTCConfiguration(iceServers=[]))
                transceiver=peer.addTransceiver('video',direction='recvonly')
                transceiver.setCodecPreferences([c for c in RTCRtpSender.getCapabilities('video').codecs if c.mimeType.lower()=='video/h264'])
                track_ready=asyncio.get_running_loop().create_future()
                @peer.on('track')
                def on_track(track):
                    if track.kind=='video' and not track_ready.done(): track_ready.set_result(track)
                await peer.setLocalDescription(await peer.createOffer())
                sdp=peer.localDescription.sdp
                endpoint=f'{media_origin}/reference/whep'
                for token in [None, account, publisher, grants[0]['access_token']]:
                    status,_,_=await media_post(endpoint,token,sdp)
                    require(status==401,'MediaMTX must reject absent/wrong-role/expired token')
                g=await grant()
                status,_,_=await media_post(f'{media_origin}/other/whep',g['access_token'],sdp)
                require(status==401,'grant must not authorize another media path')
                passed('real WHEP rejects missing, wrong-role, expired and wrong-camera credentials')
                status,answer,location=await media_post(g['endpoint'],g['access_token'],sdp)
                require(status==201 and location,'authorized WHEP negotiation')
                session=urljoin(g['endpoint'],location)
                require(urlparse(session).netloc==urlparse(g['endpoint']).netloc,'same-origin session')
                await peer.setRemoteDescription(RTCSessionDescription(sdp=answer,type='answer'))
                track=await asyncio.wait_for(track_ready,15)
                frames=[await asyncio.wait_for(track.recv(),15) for _ in range(5)]
                require(all((f.width,f.height)==(320,240) for f in frames),'decoded H264 dimensions')
                require(len({f.pts for f in frames})==5,'advancing decoded frames')
                passed('five advancing 320x240 H264 frames decoded over encrypted WebRTC')
                async with http.delete(session,headers=headers(g['access_token']),ssl=client_tls) as r:
                    require(r.status in [200,204],'WHEP session deletion')
                await peer.close(); peer=None
                passed('WHEP DELETE and peer cleanup')
                fresh=await grant()
                async with http.post(f'http://127.0.0.1:{auth}/internal/viewers/viewer_one/revoke',json={}) as r:
                    require(r.status==204,'revoke viewer admission')
                status,_=await request('GET','/v1/cameras',account); require(status==403,'revoked viewer denied')
                status,_,_=await media_post(endpoint,fresh['access_token'],sdp); require(status==401,'outstanding grant revoked')
                passed('viewer revocation blocks new API/media admission (not active-session termination)')
                control.terminate(); await asyncio.to_thread(control.wait,timeout=10)
                status,_,_=await media_post(endpoint,fresh['access_token'],sdp)
                require(status>=400,'auth service outage must fail closed')
                passed('MediaMTX fails closed when authorization service is unavailable')
            finally:
                if peer is not None: await peer.close()
                if runner is not None: await runner.cleanup()
                for p in reversed(processes):
                    if p.poll() is None:
                        p.terminate()
                        try: await asyncio.to_thread(p.wait,timeout=10)
                        except subprocess.TimeoutExpired: p.kill(); p.wait()
                for f in files: f.close()
        print(f'REFERENCE INTEGRATION PASS: {len(checks)} checks; synthetic/Linux only',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--gateway',type=Path,required=True)
    parser.add_argument('--mediamtx',type=Path,required=True)
    args=parser.parse_args()
    # Tracebacks may include secret-bearing subprocess args. Emit only sanitized test assertions.
    try: asyncio.run(exercise(args.gateway.resolve(),args.mediamtx.resolve()))
    except Exception as error:
        if isinstance(error,AssertionError): print('REFERENCE INTEGRATION FAILED:',str(error),flush=True)
        else: print('REFERENCE INTEGRATION FAILED:',type(error).__name__,flush=True)
        raise SystemExit(1) from None
