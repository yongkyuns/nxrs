#!/usr/bin/env python3
"""Synthetic pixels independently qualify H.264/SDK frame identity and box clearing.

Private loopback services only; no hardware, system trust, existing enrollment or
running gateway is modified. The native probe scopes an ephemeral CA to its own
URLSessions. Requires the pinned media tools and actual Rust camera fixture.
"""
import argparse
import asyncio
import hashlib
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
from socketserver import TCPServer
import ssl
import subprocess
import tempfile
import threading
import urllib.request
from rust_camera import RustCamera

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_fixture', ROOT/'clients/ios/ci/native_reference.py')
native = importlib.util.module_from_spec(spec); spec.loader.exec_module(native)
spec = importlib.util.spec_from_file_location('fixture_proxy', ROOT/'gateway/tools/local_https_proxy.py')
proxy = importlib.util.module_from_spec(spec); spec.loader.exec_module(proxy)


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer normally reverse-resolves its listening address. This
        # private loopback fixture needs no DNS or host-network discovery.
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]



def require(value, stage):
    if not value: raise AssertionError(stage)


async def exercise(gateway, ingest, mediamtx, camera_source, probe):
    with tempfile.TemporaryDirectory(prefix='nxrs-overlay-') as directory:
        root = Path(directory); root.chmod(0o700)
        used = set()
        def port():
            while True:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0)); n = sock.getsockname()[1]
                if n not in used: used.add(n); return n
        control, auth, rtsp, media, management, tls_api, ice = [port() for _ in range(7)]
        origins = [f'https://localhost:{tls_api}', f'https://localhost:{media}']
        tokens = {name: secrets.token_hex(32) for name in ['viewer','publisher','management']}
        def write(name, value):
            with open(root/name, 'x', opener=lambda p,f: os.open(p,f,0o600)) as stream:
                stream.write(value if isinstance(value,str) else json.dumps(value))
        digest = lambda s: hashlib.sha256(s.encode()).hexdigest()
        native.create_certificates(root)
        write('viewer.token', tokens['viewer']+'\n'); write('publisher.token', tokens['publisher']+'\n')
        write('revocations.json', {'version':1,'revoked':[]})
        write('security.json', {'revocations_file':str(root/'revocations.json'), 'media_api':f'127.0.0.1:{management}', 'media_api_token':tokens['management']})
        write('control.json', {'listen':f'127.0.0.1:{control}','auth_listen':f'127.0.0.1:{auth}',
            'grant_ttl_seconds':60,'max_grants':64,'media_origins':[origins[1]],
            'cameras':[{'camera_id':'cam_reference','gateway_id':'gateway','site_id':'site','name':'SYNTHETIC red/blue codec witness',
                'source':'synthetic','stream_path':'reference','whep_endpoint':origins[1]+'/reference/whep'}],
            'viewers':[{'viewer_id':'viewer','site_id':'site','camera_ids':['cam_reference'],'token_sha256':digest(tokens['viewer'])}],
            'publishers':[{'username':'publisher','camera_id':'cam_reference','token_sha256':digest(tokens['publisher'])}]})
        write('media.json', {'logLevel':'error','authMethod':'http','authHTTPAddress':f'http://127.0.0.1:{auth}/internal/mediamtx/auth','authHTTPExclude':[],
            'api':True,'apiAddress':f'127.0.0.1:{management}','metrics':False,'pprof':False,'playback':False,'rtmp':False,'hls':False,'srt':False,'moq':False,
            'rtsp':True,'rtspAddress':f'127.0.0.1:{rtsp}','rtspTransports':['tcp'],'rtspAuthMethods':['basic'],
            'webrtc':True,'webrtcAddress':f'127.0.0.1:{media}','webrtcEncryption':True,'webrtcServerKey':str(root/'key.pem'),'webrtcServerCert':str(root/'cert.pem'),
            'webrtcLocalUDPAddress':f'127.0.0.1:{ice}','webrtcIPsFromInterfaces':False,'webrtcAdditionalHosts':['127.0.0.1'],'webrtcICEServers2':[],
            'paths':{'reference':{'source':'publisher','overridePublisher':False,'maxReaders':4}}})
        processes=[]; handles=[]; labels=[]; source=None; https=None; https_thread=None
        readiness={'media_ready':False,'media_status':None,'api_ready':False,'api_status':None}
        def launch(args, label):
            handle=(root/(label+'.log')).open('wb');handles.append(handle)
            child=subprocess.Popen(list(map(str,args)),stdin=subprocess.DEVNULL,stdout=handle,stderr=subprocess.STDOUT);processes.append(child);labels.append(label);return child
        async def ready():
            request=urllib.request.Request(f'http://127.0.0.1:{management}/v3/paths/get/reference',headers={'Authorization':'Bearer '+tokens['management']})
            try:
                def get():
                    with urllib.request.urlopen(request,timeout=1) as response:
                        readiness['media_status']=response.status
                        return json.load(response).get('ready') is True
                readiness['media_ready']=await asyncio.to_thread(get)
            except OSError as error:
                readiness['media_status']=getattr(error,'code',None)
                readiness['media_ready']=False
            return readiness['media_ready']
        async def api_ready():
            # A catalog response alone does not mean the strict media-control
            # reconciler is ready to admit the probe's playback grant.
            request=urllib.request.Request(origins[0]+'/v1/cameras/cam_reference/playback-grants',data=b'{}',
                headers={'Authorization':'Bearer '+tokens['viewer'],'Content-Type':'application/json'})
            try:
                def get():
                    with urllib.request.urlopen(request,context=ssl.create_default_context(cafile=str(root/'root.pem')),timeout=1) as response:
                        readiness['api_status']=response.status
                        return response.status==201
                readiness['api_ready']=await asyncio.to_thread(get)
            except OSError as error:
                readiness['api_status']=getattr(error,'code',None)
                readiness['api_ready']=False
            return readiness['api_ready']
        try:
            source=await RustCamera.start(camera_source,root,detections=True,colors=True)
            write('ingest.json',{'camera':f'127.0.0.1:{source.port}','allow_insecure_camera_http':True,'rtsp':f'127.0.0.1:{rtsp}',
                'stream_path':'reference','publisher_user':'publisher','publisher_token_file':str(root/'publisher.token'),'ffmpeg':shutil.which('ffmpeg'),
                'publisher':str(args.ingest.with_name('nxrs-rtsp-publisher')),
                'width':320,'height':240,'frame_timeout_ms':2000,'detections':{'control':f'127.0.0.1:{auth}','camera_id':'cam_reference'}})
            launch([gateway,root/'control.json','--security',root/'security.json'],'control')
            launch([mediamtx,root/'media.json'],'media')
            https=LoopbackServer(('127.0.0.1',tls_api),proxy.handler(('127.0.0.1',control)))
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version=ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(root/'cert.pem',root/'key.pem')
            https.socket=context.wrap_socket(https.socket,server_side=True)
            https_thread=threading.Thread(target=https.serve_forever,daemon=True)
            https_thread.start()
            launch([ingest,root/'ingest.json'],'ingest')
            deadline=asyncio.get_running_loop().time()+30
            while not (await ready() and await api_ready()):
                require(asyncio.get_running_loop().time()<deadline,'annotation publication readiness')
                require(all(p.poll() is None for p in processes),'fixture process startup')
                await asyncio.sleep(.1)
            write('probe.json',{'gateway':origins[0],'mediaOrigin':origins[1],'credentialFile':str(root/'viewer.token'),'cameraID':'cam_reference',
                'rootCertificate':str(root/'root.der'),'wrongRootCertificate':str(root/'wrong-root.der'),'leafCertificate':str(root/'cert.der'),
                'verifySyntheticColors':True})
            reports=[]
            for iteration in range(2):
                child=await asyncio.create_subprocess_exec(str(probe),'--detection-overlay',str(root/'probe.json'),'15',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                try: out,err=await asyncio.wait_for(child.communicate(),75)
                except BaseException:
                    child.kill();await child.wait();raise
                rows=[json.loads(row) for row in out.decode().splitlines() if row.startswith('{')]
                if child.returncode != 0:
                    for row in out.decode().splitlines():
                        if row.startswith(('OVERLAY SAMPLE','PIXEL SAMPLE','{')):print(row,flush=True)
                require(child.returncode==0 and rows and rows[-1]['result']=='PASS','native synthetic color / metadata correspondence')
                report=rows[-1];require(report['pixel_identity_mismatches']==0 and report['red_person_frames']>=20 and report['blue_clear_frames']>=20,'positive/negative frame identity')
                reports.append(report)
                if iteration==0:
                    await source.command('pause')
                    deadline=asyncio.get_running_loop().time()+10
                    while await ready():
                        require(asyncio.get_running_loop().time()<deadline,'source stall retires annotated publication');await asyncio.sleep(.1)
                    await source.command('resume')
                    deadline=asyncio.get_running_loop().time()+30
                    while not await ready():
                        require(asyncio.get_running_loop().time()<deadline,'annotated publication recovery');await asyncio.sleep(.1)
            print(json.dumps({'result':'PASS','qualification':'synthetic pixels with actual Rust stream, H264 publisher and native SDK','runs':reports},sort_keys=True))
        except BaseException:
            # Report only structural facts and fixed error categories. Private
            # child logs/configs contain credentials and are never printed.
            diagnostics={'readiness':readiness,'process_exit_codes':dict(zip(labels,[p.poll() for p in processes]))}
            if source:
                try:diagnostics['source_counters']=await source.command('status')
                except Exception:diagnostics['source_counters']='unavailable'
            ingest_log=root/'ingest.log'
            if ingest_log.exists():
                text=ingest_log.read_text(errors='replace')
                categories=['Configuration','Io','Protocol','Frame','Stale','Timeout','Encoder','Stopped','Cleanup']
                diagnostics['ingest_retirements']={category:text.count(f'retired source/encoder ({category})') for category in categories}
            print(json.dumps({'fixture_diagnostics':diagnostics},sort_keys=True),flush=True)
            raise
        finally:
            failures=[]
            if https:
                try:
                    if https_thread and https_thread.is_alive():
                        await asyncio.to_thread(https.shutdown)
                        https_thread.join(timeout=1)
                        require(not https_thread.is_alive(),'HTTPS fixture not retired')
                    https.server_close()
                except Exception:failures.append('https')
            for child in reversed(processes):
                try:
                    if child.poll() is None:
                        child.terminate()
                        try:await asyncio.to_thread(child.wait,timeout=12)
                        except subprocess.TimeoutExpired:child.kill();await asyncio.to_thread(child.wait,timeout=5)
                    require(child.poll() is not None,'child not retired')
                except Exception:failures.append('owned child')
            if source:
                try:await source.close()
                except Exception:failures.append('source')
            for handle in handles:handle.close()
            require(not failures,'fixture cleanup')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ['gateway','ingest','mediamtx','camera-source','probe']:parser.add_argument('--'+arg,type=Path,required=True)
    args=parser.parse_args()
    try:asyncio.run(exercise(*[getattr(args,n.replace('-','_')).resolve() for n in ['gateway','ingest','mediamtx','camera-source','probe']]))
    except Exception as error:
        print(json.dumps({'result':'FAIL','stage':str(error) if isinstance(error,AssertionError) else type(error).__name__}));raise SystemExit(1) from None
