#!/usr/bin/env python3
"""Synthetic device-wire MJPEG -> real Rust ingest -> FFmpeg -> MediaMTX -> two WebRTC readers.

This fixture matches service/stream's framing, not a physical camera or iPhone.
No image, secret, raw child output or credential-bearing argv leaves the temp dir.
"""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import urljoin

import aiohttp
from aiortc import RTCSessionDescription
from revocation_reference import Reader, digest, private_write, require


async def until(test, message, seconds=25):
    deadline=asyncio.get_running_loop().time()+seconds
    while asyncio.get_running_loop().time()<deadline:
        if await test(): return
        await asyncio.sleep(.1)
    raise AssertionError(message)


async def exercise(gateway, ingest, mediamtx):
    require(os.name=='posix' and Path('/proc/self').exists(), 'Linux process-lifecycle fixture required')
    with tempfile.TemporaryDirectory(prefix='nxrs-ingest-') as directory:
        root=Path(directory)
        used=set()
        def port(udp=False):
            while True:
                with socket.socket(type=socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
                    sock.bind(('127.0.0.1',0)); value=sock.getsockname()[1]
                if value not in used: used.add(value); return value
        api,auth,media,rtsp,management=[port() for _ in range(5)]
        ice=port(True)
        tokens={name:secrets.token_hex(32) for name in ['viewer','publisher','management']}
        base=f'http://127.0.0.1:{api}'
        origin=f'https://127.0.0.1:{media}'
        internal=f'http://127.0.0.1:{auth}'
        private_write(root/'state.json',json.dumps({'version':1,'revoked':[]}))
        private_write(root/'security.json',json.dumps({'revocations_file':str(root/'state.json'),
            'media_api':f'127.0.0.1:{management}','media_api_token':tokens['management']}))
        private_write(root/'publisher.token',tokens['publisher']+'\n')
        private_write(root/'control.json',json.dumps({
            'listen':f'127.0.0.1:{api}','auth_listen':f'127.0.0.1:{auth}',
            'grant_ttl_seconds':60,'max_grants':128,'media_origins':[origin],
            'cameras':[dict(camera_id='cam_reference',gateway_id='gw_reference',site_id='site_reference',
                name='SYNTHETIC device-wire ingest fixture',source='synthetic',stream_path='reference',
                whep_endpoint=origin+'/reference/whep')],
            'viewers':[dict(viewer_id='viewer',site_id='site_reference',camera_ids=['cam_reference'],
                token_sha256=digest(tokens['viewer']))],
            'publishers':[dict(username='publisher',camera_id='cam_reference',token_sha256=digest(tokens['publisher']))]
        }))
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem'),'-subj','/CN=localhost',
            '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost'],check=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
        (root/'key.pem').chmod(0o600)
        tls=ssl.create_default_context(cafile=str(root/'cert.pem'))
        private_write(root/'media.json',json.dumps({
            'logLevel':'error','authMethod':'http','authHTTPAddress':internal+'/internal/mediamtx/auth',
            'authHTTPExclude':[],'api':True,'apiAddress':f'127.0.0.1:{management}',
            'metrics':False,'pprof':False,'playback':False,'rtmp':False,'hls':False,'srt':False,'moq':False,
            'rtsp':True,'rtspAddress':f'127.0.0.1:{rtsp}','rtspTransports':['tcp'],'rtspAuthMethods':['basic'],
            'webrtc':True,'webrtcAddress':f'127.0.0.1:{media}','webrtcEncryption':True,
            'webrtcServerKey':str(root/'key.pem'),'webrtcServerCert':str(root/'cert.pem'),
            'webrtcLocalUDPAddress':f'127.0.0.1:{ice}','webrtcIPsFromInterfaces':False,
            'webrtcAdditionalHosts':['127.0.0.1'],'webrtcICEServers2':[],
            'paths':{'reference':{'source':'publisher','overridePublisher':False,'maxReaders':4}}
        }))
        subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i',
            'testsrc2=size=320x240:rate=10','-frames:v','2','-threads','1','-c:v','mjpeg',
            '-q:v','5',str(root/'frame-%d.jpg')],check=True,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,timeout=30)
        images=[(root/f'frame-{i}.jpg').read_bytes() for i in [1,2]]
        require(all(len(i)<=153600 for i in images),'bounded generated fixtures')
        state=dict(active=0,peak=0,connections=0,frames=0,paused=False)
        tasks=set()
        async def device(reader,writer):
            task=asyncio.current_task(); tasks.add(task)
            admitted=False
            try:
                if state['active']: return  # Actual firmware permits only one admitted stream.
                admitted=True; state['active']+=1; state['connections']+=1
                state['peak']=max(state['peak'],state['active'])
                request=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
                require(request.startswith(b'GET /stream HTTP/1.1\r\n') and len(request)<1024,'camera endpoint contract')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n')
                await writer.drain()
                sequence=0
                while True:
                    if not state['paused']:
                        sequence+=1; image=images[sequence%2]
                        writer.write((f'--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(image)}\r\n'
                            f'X-Sequence: {sequence}\r\nX-Capture-Ms: {int(time.monotonic()*1000)}\r\n\r\n').encode()+image+b'\r\n')
                        await writer.drain(); state['frames']+=1
                    try:
                        # Observe client EOF even during a stalled camera; no overlapping source sessions.
                        if await asyncio.wait_for(reader.read(1),.1) == b'': break
                        raise AssertionError('unexpected camera client bytes')
                    except asyncio.TimeoutError: pass
            except (ConnectionError,asyncio.IncompleteReadError,asyncio.TimeoutError): pass
            finally:
                if admitted: state['active']-=1
                writer.close()
                with contextlib.suppress(ConnectionError,asyncio.TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(),2)
                tasks.discard(task)
        server=await asyncio.start_server(device,'127.0.0.1',0,limit=1024)
        camera_port=server.sockets[0].getsockname()[1]
        private_write(root/'ingest.json',json.dumps({
            'camera':f'127.0.0.1:{camera_port}','allow_insecure_camera_http':True,
            'rtsp':f'127.0.0.1:{rtsp}','stream_path':'reference','publisher_user':'publisher',
            'publisher_token_file':str(root/'publisher.token'),'ffmpeg':shutil.which('ffmpeg'),
            'width':320,'height':240,'frame_timeout_ms':2000
        }))
        processes,handles,readers=[],[],[]
        def launch(args,label):
            handle=open(root/(label+'.log'),'wb'); os.chmod(root/(label+'.log'),0o600); handles.append(handle)
            child=subprocess.Popen(args,stdout=handle,stderr=subprocess.STDOUT); processes.append(child); return child
        def children(process):
            file=Path(f'/proc/{process.pid}/task/{process.pid}/children')
            return set(map(int,file.read_text().split())) if file.exists() else set()
        try:
            launch([str(gateway),str(root/'control.json'),'--security',str(root/'security.json')],'control')
            launch([str(mediamtx),str(root/'media.json')],'mediamtx')
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                async def ready():
                    try:
                        async with http.get(internal+'/internal/viewers/viewer/revocation') as r:
                            return r.status==200 and (await r.json())['media_control_ready']
                    except (aiohttp.ClientError,asyncio.TimeoutError):return False
                await until(ready,'media authorization ready')
                adapter=launch([str(ingest),str(root/'ingest.json')],'ingest')
                async def publishing():
                    async with http.get(f'http://127.0.0.1:{management}/v3/paths/get/reference',
                            headers={'Authorization':'Bearer '+tokens['management']}) as r:
                        return r.status==200 and (await r.json()).get('ready') is True
                await until(publishing,'MJPEG encoded and published')
                async def connect():
                    reader=Reader(); readers.append(reader)
                    await reader.peer.setLocalDescription(await reader.peer.createOffer())
                    async with http.post(base+'/v1/cameras/cam_reference/playback-grants',json={},
                            headers={'Authorization':'Bearer '+tokens['viewer']}) as r:
                        require(r.status==201,'scoped playback grant'); grant=await r.json()
                    async with http.post(grant['endpoint'],data=reader.peer.localDescription.sdp,
                            headers={'Authorization':'Bearer '+grant['access_token'],'Content-Type':'application/sdp'},
                            ssl=tls,allow_redirects=False) as r:
                        require(r.status==201,'MJPEG-source WHEP negotiation')
                        answer=await r.text(); reader.session_url=urljoin(grant['endpoint'],r.headers['Location'])
                    await reader.peer.setRemoteDescription(RTCSessionDescription(sdp=answer,type='answer'))
                    async def advancing():return reader.count>=5
                    await until(advancing,'five advancing decoded H264 frames per reader')
                    return reader
                a,b=await connect(),await connect()
                require(state['connections']==1 and state['peak']==1,'two viewers must share one device connection')
                child_before=children(adapter); require(len(child_before)==1,'one encoder owned')
                print('PASS: two WebRTC viewers decode advancing 320x240 H264 from one bounded device-wire MJPEG reader',flush=True)
                state['paused']=True
                async def retired():return child_before.isdisjoint(children(adapter)) and not await publishing()
                await until(retired,'source stall retires encoder/publication',seconds=10)
                await asyncio.sleep(1.5); counts=(a.count,b.count); await asyncio.sleep(1)
                require((a.count,b.count)==counts,'stalled source must not manufacture live frames')
                print('PASS: open-but-stalled source removes the encoder and stops continuously drained video',flush=True)
                state['paused']=False
                await until(publishing,'publication recovers after source reconnect')
                c=await connect()
                require(c.count>=5 and state['connections']>=2 and state['peak']==1,'sequence reset accepted only on new source connection')
                require(len(children(adapter))==1,'recovery owns exactly one encoder')
                print('PASS: source reconnect accepts a new sequence epoch and restores actual decoded video',flush=True)
                child_before=children(adapter)
                adapter.terminate(); await asyncio.to_thread(adapter.wait,timeout=8)
                require(adapter.returncode==0,'graceful adapter termination')
                async def disconnected():return state['active']==0
                await until(disconnected,'source socket closed on shutdown',seconds=5)
                require(all(not Path(f'/proc/{pid}').exists() for pid in child_before),'encoder reaped on shutdown')
                print('PASS: SIGTERM closes source and reaps encoder before exit',flush=True)
        finally:
            for reader in readers: await reader.close()
            for child in reversed(processes):
                if child.poll() is None:
                    child.terminate()
                    try: await asyncio.to_thread(child.wait,timeout=8)
                    except subprocess.TimeoutExpired: child.kill(); await asyncio.to_thread(child.wait,timeout=5)
            server.close(); await server.wait_closed()
            for task in list(tasks):task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            for handle in handles:handle.close()
        print('INGEST REFERENCE PASS: synthetic protocol fixture, not physical camera/iPhone/VPN qualification',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['gateway','ingest','mediamtx']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    try:asyncio.run(exercise(args.gateway.resolve(),args.ingest.resolve(),args.mediamtx.resolve()))
    except Exception as error:
        print('INGEST REFERENCE FAILED:',str(error) if isinstance(error,AssertionError) else type(error).__name__,flush=True)
        raise SystemExit(1) from None
