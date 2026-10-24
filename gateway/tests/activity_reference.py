#!/usr/bin/env python3
"""Isolated real MediaMTX/FFmpeg motion recording. Synthetic fixture only.

No personal camera, USB port, system trust or existing gateway is touched.
Temporary credentials, footage and raw process logs are private and removed.
"""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from credential_process import assert_boundary


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def request_with_busy_retry(send, method):
    # Match the client's bounded idempotent-read policy. Do not retry writes,
    # authorization errors, service failures or an indefinitely busy store.
    for attempt in range(3):
        result = send()
        if result[0] != 429 or method != 'GET' or attempt == 2:
            return result
        time.sleep(.15 * (attempt + 1))


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def private(path, value):
    with open(path, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as file:
        file.write(value if isinstance(value, str) else json.dumps(value))


def recorder_probe(ffmpeg, port, token):
    # Synthetic fixture only: do not write the input URL to argv or a file.
    playlist=(f"ffconcat version 1.0\nfile 'rtsp://recorder:{token}@127.0.0.1:{port}/one'\n"
              'option rtsp_transport tcp\noption timeout 5000000\n')
    command=[str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-f','concat','-safe','0',
             '-protocol_whitelist','pipe,rtsp,tcp','-i','pipe:0','-t','1','-f','null','-']
    result=subprocess.run(command,input=playlist.encode(),env={'PATH':'/usr/bin:/bin','LANG':'C'},
                          capture_output=True,timeout=8)
    return result.returncode, b'401' in result.stderr


def run(gateway, worker, mediamtx, ffmpeg, *, volume=None, reader=None, media_sandbox=None):
    reader = reader or worker.parent/"nxrs-rtsp-reader"
    parent = Path(volume["mount"])/"qualification" if volume else None
    with tempfile.TemporaryDirectory(dir=parent, prefix='nxrs-activity-reference-') as temp:
        root = Path(temp).resolve()
        api, auth, rtsp, https = [port() for _ in range(4)]
        tokens = {role: secrets.token_hex(32) for role in ['owner', 'reader', 'other', 'publisher', 'recorder']}
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        storage = dict(directory=str(root/'activity'), camera_ids=['cam_one'], retention_seconds=259200,
                       max_bytes=64*1024*1024, max_events=100)
        if volume:
            storage.update(directory=str(Path(volume["mount"])/"activity"), protection=volume)
        config = dict(listen=f'127.0.0.1:{api}', auth_listen=f'127.0.0.1:{auth}',
                      grant_ttl_seconds=60, max_grants=64, media_origins=['https://media.test'],
                      cameras=[dict(camera_id='cam_'+name, gateway_id='gw', site_id='site', name='SYNTHETIC '+name,
                                    source='synthetic', stream_path=name, whep_endpoint=f'https://media.test/{name}/whep')
                               for name in ['one', 'two']],
                      viewers=[dict(viewer_id=role, site_id='site', camera_ids=['cam_two' if role == 'other' else 'cam_one'],
                                    token_sha256=digest(tokens[role]), manage_recordings=role == 'owner')
                               for role in ['owner', 'reader', 'other']],
                      publishers=[dict(username='publisher', camera_id='cam_one', token_sha256=digest(tokens['publisher']))],
                      recorders=[dict(username='recorder', camera_id='cam_one', token_sha256=digest(tokens['recorder']))],
                      activity=storage)
        private(root/'gateway.json', config)
        private(root/'recorder.token', tokens['recorder'])
        worker_config = dict(storage=storage, camera_id='cam_one', rtsp=f'127.0.0.1:{rtsp}', stream_path='one',
                             username='recorder', token_file=str(root/'recorder.token'), ffmpeg=str(ffmpeg),
                             reader=str(reader),media_sandbox=str(media_sandbox) if media_sandbox else None,
                             motion_percent=2, pre_roll_seconds=4, post_roll_seconds=4, max_clip_seconds=20)
        private(root/'worker.json', worker_config)
        private(root/'mediamtx.json', dict(logLevel='warn', authMethod='http', authHTTPExclude=[],
            authHTTPAddress=f'http://127.0.0.1:{auth}/internal/mediamtx/auth', api=False, metrics=False, pprof=False,
            playback=False, rtsp=True, rtspAddress=f'127.0.0.1:{rtsp}', rtspTransports=['tcp'], rtspAuthMethods=['basic'],
            rtmp=False, hls=False, webrtc=False, srt=False, moq=False,
            paths={'one':dict(source='publisher', overridePublisher=False)}))
        result = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(root/'key.pem'), '-out', str(root/'cert.pem'), '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=IP:127.0.0.1'], capture_output=True, timeout=30)
        require(result.returncode == 0, 'fixture TLS setup')
        os.chmod(root/'key.pem', 0o600)
        processes, files = [], []
        def launch(args, name):
            file = open(root/name, 'wb'); os.chmod(root/name, 0o600); files.append(file)
            process = subprocess.Popen([str(v) for v in args],stdout=file,stderr=subprocess.STDOUT,
                                       env={'PATH':'/usr/bin:/bin','LANG':'C'})
            processes.append(process); return process
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def forward(self):
                if not self.path.startswith('/v1/'):
                    self.send_error(404); return
                conn = http.client.HTTPConnection('127.0.0.1', api, timeout=10)
                try:
                    count = int(self.headers.get('Content-Length', '0'))
                    if count > 16384:
                        self.send_error(413); return
                    headers = {k:v for k,v in self.headers.items() if k.lower() in ['authorization','content-type']}
                    conn.request(self.command, self.path, body=self.rfile.read(count) if count else None, headers=headers)
                    response = conn.getresponse(); payload = response.read(33_554_433)
                    self.send_response(response.status)
                    for k,v in response.getheaders():
                        if k.lower() in ['content-type','cache-control','www-authenticate']:
                            self.send_header(k,v)
                    self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
                except OSError:
                    self.send_response(503); self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', '0'); self.end_headers()
                finally:
                    conn.close()
            do_GET = forward
            do_POST = forward
            do_DELETE = forward
        server = ThreadingHTTPServer(('127.0.0.1', https), Proxy)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(root/'cert.pem', root/'key.pem')
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        trust = ssl.create_default_context(cafile=str(root/'cert.pem'))
        def single_request(path, role='owner', method='GET', body=None):
            headers = {'Authorization':'Bearer '+tokens[role]} if role else {}
            if body is not None:
                body = json.dumps(body).encode(); headers['Content-Type'] = 'application/json'
            req = urllib.request.Request(f'https://127.0.0.1:{https}'+path, headers=headers, method=method, data=body)
            try:
                response = urllib.request.urlopen(req, context=trust, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                data = response.read(33_554_433)
                require(response.headers.get('Cache-Control') == 'no-store', 'history is private and not cached')
                return response.status, data
        def request(path, role='owner', method='GET', body=None):
            return request_with_busy_retry(lambda: single_request(path, role, method, body), method)
        base = '/v1/cameras/cam_one/activity'
        def activity_page():
            status, data = request(base)
            require(status == 200, f'activity page must succeed after bounded busy retry (HTTP {status})')
            return json.loads(data)
        try:
            control = launch([gateway, root/'gateway.json'], 'gateway.log')
            media = launch([mediamtx, root/'mediamtx.json'], 'mediamtx.log')
            for _ in range(100):
                try:
                    if request(base)[0] == 200:
                        break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(.1)
            require(control.poll() is None and media.poll() is None, 'reference processes stay running')
            for role, expected in [(None,401), ('publisher',401), ('recorder',401), ('other',404)]:
                require(request(base, role)[0] == expected, 'history role/camera isolation')
            print('PASS: authenticated camera-scoped history over verified HTTPS', flush=True)
            pattern = ('color=c=gray:s=320x240:r=15:d=6[a];'
                       'testsrc2=s=320x240:r=15:d=10[b];'
                       'color=c=gray:s=320x240:r=15:d=18[c];[a][b][c]concat=n=3:v=1:a=0')
            publisher = launch([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-re', '-f', 'lavfi', '-i', pattern,
                '-an', '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p', '-g', '15', '-bf', '0', '-f', 'rtsp', '-rtsp_transport', 'tcp',
                f'rtsp://publisher:{tokens["publisher"]}@127.0.0.1:{rtsp}/one'], 'publisher.log')
            time.sleep(.5)
            code, unauthorized = recorder_probe(ffmpeg,rtsp,secrets.token_hex(32))
            require(code!=0 and unauthorized,'actual media rejects the wrong recorder credential')
            code, unauthorized = recorder_probe(ffmpeg,rtsp,tokens['publisher'])
            require(code!=0 and unauthorized,'publisher credential cannot supplement the recorder role')
            print('PASS: real RTSP authorization rejects wrong and cross-role recorder credentials',flush=True)
            observer = launch([worker, root/'worker.json', *(['--require-media-isolation'] if media_sandbox else [])], 'worker.log')
            checked = False
            inspected = False
            event = None
            deadline = time.monotonic()+40
            while time.monotonic() < deadline:
                require(observer.poll() is None, 'observer remains running')
                inspected = bool(assert_boundary(observer.pid,tokens.values())) or inspected
                status, data = request(base)
                if status == 200:
                    page = json.loads(data)
                    if page['events']:
                        event = page['events'][0]; break
                    if not checked and (page.get('health') or {}).get('reconnects', 0) >= 2:
                        checked = True
                        code, unauthorized = recorder_probe(ffmpeg,rtsp,tokens['recorder'])
                        print('DIAGNOSTIC: source probe', dict(publisher_exit=publisher.poll(),exit=code,
                                                             unauthorized=unauthorized),flush=True)
                time.sleep(.5)
            # Failure diagnostics remain private: never print process arguments or raw logs.
            if event is None:
                status, data = request(base)
                health = json.loads(data).get('health') if status == 200 else None
                print('DIAGNOSTIC: observer health', health, flush=True)
            require(event is not None, 'confirmed movement produces an automatic clip')
            require(inspected,'actual decoder metadata inspected')
            assert_boundary(observer.pid,tokens.values())
            print('PASS: actual recorder source/finalizer argv and environments contain no credentials',flush=True)
            duration = (event['ended_at_ms']-event['started_at_ms'])/1000
            require(6 <= duration <= 24, 'clip includes bounded pre/post roll')
            status, clip = request(base+'/'+event['event_id']+'/clip')
            require(status == 200 and len(clip) == event['clip_bytes'], 'authorized original-video download')
            parts = []
            for number in range((len(clip)+524287)//524288):
                status, piece = request(base+'/'+event['event_id']+f'/clip/parts/{number}')
                require(status == 200 and len(piece) <= 524288, 'bounded authenticated clip pieces')
                parts.append(piece)
            require(b''.join(parts) == clip, 'clip pieces reconstruct the exact original')
            require(request(base+'/'+event['event_id']+'/clip/parts/64')[0] == 404, 'out of range media rejected')
            private(root/'download.mp4', '')
            (root/'download.mp4').write_bytes(clip)
            decoded = subprocess.run([str(ffmpeg), '-nostdin', '-v', 'error', '-i', str(root/'download.mp4'), '-f', 'null', '-'],
                                     capture_output=True, timeout=20)
            require(decoded.returncode == 0, 'saved H264 clip fully decodes')
            status, image = request(base+'/'+event['event_id']+'/thumbnail')
            require(status == 200 and image.startswith(b'\xff\xd8'), 'real clip thumbnail')
            require(all(value.encode() not in clip and value.encode() not in image for value in tokens.values()),
                    'recorded media does not contain credential strings')
            require(request(base+'/'+event['event_id']+'/keep', 'reader', 'POST', {'kept':True})[0] == 403, 'view-only cannot change retention')
            require(request(base+'/'+event['event_id']+'/keep', method='POST', body={'kept':True})[0] == 200, 'owner can keep footage')
            require(request(base+'/'+event['event_id']+'/clip', 'other')[0] == 404, 'clip never crosses camera scope')
            print('PASS: motion pre/post roll, decodable H264, thumbnail and retention permissions', flush=True)
            previous_run = activity_page()['health']['progress']['observer_run']
            observer.send_signal(signal.SIGTERM); observer.wait(timeout=30)
            require(observer.returncode == 0, 'observer joins readers and encoder on shutdown')
            observer = launch([worker, root/'worker.json', *(['--require-media-isolation'] if media_sandbox else [])], 'worker-restarted.log')
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                require(observer.poll() is None, 'restarted observer remains running')
                page = activity_page(); health = page['health']
                if health['progress']['observer_run']>previous_run and health['state']=='monitoring':
                    break
                time.sleep(.2)
            require(health['progress']['observer_run']>previous_run and health['state']=='monitoring',
                    'restarted observer opens a new timing interval and decodes before source-loss injection')
            require(any(e['event_id']==event['event_id'] and e['kept'] for e in page['events']),
                    'kept clip survives observer restart')
            print('PASS: orderly shutdown, observer restart and kept-clip persistence', flush=True)
            control.terminate(); control.wait(timeout=10)
            control = launch([gateway, root/'gateway.json'], 'gateway-restarted.log')
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                try:
                    if request(base)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(.1)
            require(request(base+'/'+event['event_id']+'/clip')[0] == 200, 'recordings survive controller restart')
            print('PASS: gateway restart preserves authenticated kept footage', flush=True)
            publisher.terminate(); publisher.wait(timeout=10)
            deadline=time.monotonic()+15
            initial_reconnects=activity_page()['health']['reconnects']
            while time.monotonic()<deadline:
                health=activity_page()['health']
                if health['reconnects']>initial_reconnects:
                    break
                time.sleep(.2)
            require(health['reconnects']>initial_reconnects,'source loss is observed and recovery begins')
            before=health['frames_observed']
            publisher=launch([ffmpeg,'-nostdin','-hide_banner','-loglevel','error','-re','-f','lavfi','-i','testsrc2=s=320x240:r=15',
                '-an','-c:v','libx264','-threads','2','-preset','ultrafast','-tune','zerolatency','-pix_fmt','yuv420p',
                '-g','15','-bf','0','-f','rtsp','-rtsp_transport','tcp',
                f'rtsp://publisher:{tokens["publisher"]}@127.0.0.1:{rtsp}/one'],'publisher-recovered.log')
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                require(observer.poll() is None,'observer survives reader completion and source restart')
                health=activity_page()['health']
                if health['frames_observed']>=before+12 and health['state']=='monitoring':
                    break
                time.sleep(.2)
            require(health['frames_observed']>=before+12,'real decoder samples resume after source recovery')
            timing=health['progress']
            require(timing['recoveries']>=1 and 0<timing['last_recovery_ms']<=timing['max_recovery_ms']
                    <=timing['max_frame_gap_ms'],'monotonic source recovery duration reaches authenticated health')
            publisher.terminate(); publisher.wait(timeout=10)
            deadline=time.monotonic()+15
            partial=None
            while time.monotonic()<deadline:
                page=activity_page()
                partial=next((e for e in page['events'] if e['interrupted']),None)
                if partial:
                    break
                time.sleep(.2)
            require(partial is not None,'interrupted movement preserves a completed playable partial clip')
            status,payload=request(base+'/'+partial['event_id']+'/clip')
            require(status==200,'interrupted recording remains authenticated')
            (root/'download.mp4').write_bytes(payload)
            decoded=subprocess.run([str(ffmpeg),'-nostdin','-v','error','-i',str(root/'download.mp4'),'-f','null','-'],capture_output=True,timeout=20)
            require(decoded.returncode==0,'interrupted recording fully decodes')
            assert_boundary(observer.pid,tokens.values())
            print('PASS: source loss/recovery, joined readers and playable interrupted motion',flush=True)
            require(request(base+'/'+event['event_id']+'/delete', 'reader', 'POST', {})[0] == 403, 'read-only cannot delete footage')
            require(request(base+'/'+event['event_id']+'/delete', method='POST', body={})[0] == 204, 'explicit owner deletion')
            require(request(base+'/'+event['event_id']+'/clip')[0] == 404, 'deleted footage is no longer exposed')
            print('PASS: explicit owner deletion and post-delete media isolation', flush=True)
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            for file in files:
                file.close()
            for log in root.glob('*.log'):
                data=log.read_bytes()
                require(tokens['recorder'].encode() not in data,'recorder credential in owned logs')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gateway', required=True, type=Path)
    parser.add_argument('--worker', required=True, type=Path)
    parser.add_argument('--mediamtx', required=True, type=Path)
    parser.add_argument('--reader',type=Path)
    parser.add_argument('--media-sandbox',type=Path)
    parser.add_argument('--ffmpeg', type=Path, default=Path(shutil.which('ffmpeg') or '/usr/bin/ffmpeg'))
    parser.add_argument("--storage-volume",type=Path)
    parser.add_argument("--luks-uuid")
    args = parser.parse_args()
    if bool(args.storage_volume) != bool(args.luks_uuid): parser.error("volume and UUID must be supplied together")
    volume = dict(layout_version=2,mount=str(args.storage_volume.resolve()),luks_uuid=args.luks_uuid) if args.storage_volume else None
    run(args.gateway.resolve(), args.worker.resolve(), args.mediamtx.resolve(), args.ffmpeg.resolve(),volume=volume,reader=args.reader.resolve() if args.reader else None,
        media_sandbox=args.media_sandbox.resolve() if args.media_sandbox else None)
