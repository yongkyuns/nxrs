#!/usr/bin/env python3
"""Isolated native client qualification: three real Rust stream owners, gateway
and H264 ingest, independent epochs, ephemeral TLS and a test-owned runner.
No production enrollment, machine trust or running gateway is modified.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'gateway/tests'))
from rust_camera import RustCamera
from detection_video_reference import LoopbackServer, native, proxy


def require(value, message):
    if not value: raise AssertionError(message)


async def run_runner(command, timeout=240):
    """Retire the runner and its descendants before deleting private fixtures."""
    runner=await asyncio.create_subprocess_exec(*map(str,command),start_new_session=True)
    try:
        return await asyncio.wait_for(runner.wait(),timeout)
    except BaseException:
        try:os.killpg(runner.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:
            await asyncio.wait_for(runner.wait(),8)
        finally:
            # The parent can exit while a native test child ignores TERM.
            try:os.killpg(runner.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            await runner.wait()
        raise


async def exercise(args):
    with tempfile.TemporaryDirectory(prefix='nxrs-multicamera-') as directory:
        root = Path(directory); root.chmod(0o700)
        used = set()
        def port():
            while True:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0)); value = sock.getsockname()[1]
                if value not in used: used.add(value); return value
        control, auth, rtsp, media, management, tls, ice = [port() for _ in range(7)]
        def write(name, value):
            path = root/name
            with open(path, 'x', opener=lambda p,f: os.open(p,f,0o600)) as stream:
                stream.write(value if isinstance(value,str) else json.dumps(value))
        tokens = {name: secrets.token_hex(32) for name in ['viewer', 'management', 'a', 'b', 'c']}
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        native.create_certificates(root)
        cameras = [{'camera_id':f'cam_{key}', 'gateway_id':'gateway', 'site_id':'site', 'name':f'SYNTHETIC camera {key.upper()}',
                    'source':'synthetic', 'stream_path':key, 'whep_endpoint':f'https://localhost:{media}/{key}/whep'} for key in 'abc']
        write('control.json', {'listen':f'127.0.0.1:{control}', 'auth_listen':f'127.0.0.1:{auth}',
            'grant_ttl_seconds':60, 'max_grants':128, 'media_origins':[f'https://localhost:{media}'], 'cameras':cameras,
            'viewers':[{'viewer_id':'viewer', 'site_id':'site', 'camera_ids':[c['camera_id'] for c in cameras], 'token_sha256':digest(tokens['viewer'])}],
            'publishers':[{'username':f'publisher_{key}', 'camera_id':f'cam_{key}', 'token_sha256':digest(tokens[key])} for key in 'abc']})
        write('revocations.json', {'version':1, 'revoked':[]})
        write('security.json', {'revocations_file':str(root/'revocations.json'), 'media_api':f'127.0.0.1:{management}', 'media_api_token':tokens['management']})
        write('media.json', {'logLevel':'error', 'authMethod':'http', 'authHTTPAddress':f'http://127.0.0.1:{auth}/internal/mediamtx/auth', 'authHTTPExclude':[],
            'api':True,'apiAddress':f'127.0.0.1:{management}', 'metrics':False,'pprof':False,'playback':False,'rtmp':False,'hls':False,'srt':False,'moq':False,
            'rtsp':True,'rtspAddress':f'127.0.0.1:{rtsp}','rtspTransports':['tcp'],'rtspAuthMethods':['basic'],
            'webrtc':True,'webrtcAddress':f'127.0.0.1:{media}','webrtcEncryption':True,'webrtcServerKey':str(root/'key.pem'),'webrtcServerCert':str(root/'cert.pem'),
            'webrtcLocalUDPAddress':f'0.0.0.0:{ice}','webrtcIPsFromInterfaces':False,
            'webrtcAdditionalHosts':['127.0.0.1','10.0.2.2'],'webrtcICEServers2':[],
            'paths':{key:{'source':'publisher','overridePublisher':False,'maxReaders':4} for key in 'abc'}})
        children=[]; logs=[]; sources=[]; server=None
        def launch(command, label):
            handle=(root/(label+'.log')).open('wb'); os.chmod(handle.name,0o600); logs.append(handle)
            child=subprocess.Popen(list(map(str,command)),stdin=subprocess.DEVNULL,stdout=handle,stderr=subprocess.STDOUT); children.append(child); return child
        try:
            for key in 'abc':
                target=root/key; target.mkdir(mode=0o700)
                source=await RustCamera.start(args.camera_source,target,detections=True,colors=True,cadence_17fps=True); sources.append(source)
                write(f'{key}.token',tokens[key])
                write(f'{key}.json', {'camera':f'127.0.0.1:{source.port}','allow_insecure_camera_http':True,'rtsp':f'127.0.0.1:{rtsp}',
                    'stream_path':key,'publisher_user':f'publisher_{key}','publisher_token_file':str(root/f'{key}.token'),'ffmpeg':shutil.which('ffmpeg'),
                    'publisher':str(args.ingest.resolve().with_name('nxrs-rtsp-publisher')),
                    'width':320,'height':240,'frame_timeout_ms':2000,'detections':{'control':f'127.0.0.1:{auth}','camera_id':f'cam_{key}'}})
            launch([args.gateway,root/'control.json','--security',root/'security.json'],'control')
            launch([args.mediamtx,root/'media.json'],'media')
            loop=asyncio.get_running_loop()
            class Control(proxy.handler(('127.0.0.1',control))):
                def do_POST(self):
                    if not self.path.startswith('/_fixture/'):
                        return super().do_POST()
                    parts=self.path.split('/')
                    if (len(parts)!=5 or parts[2]!='camera' or parts[3] not in ['cam_a','cam_b','cam_c']
                            or parts[4] not in ['pause','resume'] or self.headers.get('Content-Length','0')!='0'
                            or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+tokens['viewer'])):
                        self.send_error(403);return
                    try:
                        future=asyncio.run_coroutine_threadsafe(sources['abc'.index(parts[3][-1])].command(parts[4]),loop)
                        future.result(timeout=5)
                        self.send_response(204);self.send_header('Content-Length','0');self.end_headers()
                    except Exception:self.send_error(503)
            server=LoopbackServer(('127.0.0.1',tls),Control)
            trust=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); trust.minimum_version=ssl.TLSVersion.TLSv1_2
            trust.load_cert_chain(root/'cert.pem',root/'key.pem');server.socket=trust.wrap_socket(server.socket,server_side=True)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            for key in 'abc': launch([args.ingest,root/f'{key}.json'],f'ingest-{key}')
            async def ready():
                def get():
                    for key in 'abc':
                        request=urllib.request.Request(f'http://127.0.0.1:{management}/v3/paths/get/{key}',headers={'Authorization':'Bearer '+tokens['management']})
                        with urllib.request.urlopen(request,timeout=1) as response:
                            if json.load(response).get('ready') is not True:return False
                    request=urllib.request.Request(f'https://localhost:{tls}/v1/cameras/cam_a/playback-grants',data=b'{}',headers={'Authorization':'Bearer '+tokens['viewer'],'Content-Type':'application/json'})
                    with urllib.request.urlopen(request,context=ssl.create_default_context(cafile=str(root/'root.pem')),timeout=1) as response:return response.status==201
                try:return await asyncio.to_thread(get)
                except OSError:return False
            deadline=asyncio.get_running_loop().time()+40
            while not await ready():
                require(asyncio.get_running_loop().time()<deadline,'three-camera publication readiness')
                require(all(child.poll() is None for child in children),'fixture child startup')
                await asyncio.sleep(.1)
            write('runner.json', {'gateway':f'https://localhost:{tls}','mediaOrigin':f'https://localhost:{media}', 'token':tokens['viewer'],
                'rootCertificateBase64':base64.b64encode((root/'root.der').read_bytes()).decode(),
                'rootCertificate':str(root/'root.der'), 'ports':[tls,media], 'qualificationCase':args.case, 'minimumFPS':14.0 if args.case=='throughput' else 4.0})
            code=await run_runner([args.runner,root/'runner.json'])
            if code:
                for key,source in zip('abc',sources):
                    try:print('FIXTURE DIAGNOSTIC:',key,await source.command('status'),flush=True)
                    except Exception as error:print('FIXTURE DIAGNOSTIC:',key,type(error).__name__,flush=True)
                print('FIXTURE DIAGNOSTIC: child exits',[child.poll() for child in children],flush=True)
                categories=['Configuration','Io','Protocol','Frame','Stale','Timeout','Encoder','Stopped','Cleanup']
                for key in 'abc':
                    with (root/f'ingest-{key}.log').open(errors='replace') as log:text=log.read(65536)
                    print('FIXTURE DIAGNOSTIC: ingest',key,
                        {category:text.count(f'retired source/encoder ({category})') for category in categories},flush=True)
                with (root/'control.log').open(errors='replace') as log:control_log=log.read(65536)
                for line in control_log.splitlines():
                    if line in ['nxrs-gateway media control ready','nxrs-gateway media control unavailable; new viewing denied']:
                        print('FIXTURE DIAGNOSTIC:',line,flush=True)
            require(code==0,'native multi-camera qualification runner')
        finally:
            failures=[]
            if server:
                try:await asyncio.to_thread(server.shutdown);server.server_close()
                except Exception:failures.append('HTTPS fixture')
            for child in reversed(children):
                try:
                    if child.poll() is None:
                        child.terminate()
                        try:await asyncio.to_thread(child.wait,8)
                        except subprocess.TimeoutExpired:child.kill();await asyncio.to_thread(child.wait,8)
                except Exception:failures.append('fixture process')
            for source in reversed(sources):
                try:await source.close()
                except Exception:failures.append('Rust source')
            for handle in logs:handle.close()
            require(not failures,'fixture cleanup: '+', '.join(failures))
    require(not Path(directory).exists(),'private fixture cleanup')
    print('MULTICAMERA FIXTURE PASS: three real Rust sources, independent gateway publications, private resources removed',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['gateway','ingest','mediamtx','camera-source','runner']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--case',choices=['functional','throughput'],default='functional')
    args=parser.parse_args()
    try:asyncio.run(exercise(args))
    except Exception as error:
        print('MULTICAMERA FIXTURE FAILED:',str(error) if isinstance(error,AssertionError) else type(error).__name__,flush=True)
        raise SystemExit(1) from None
