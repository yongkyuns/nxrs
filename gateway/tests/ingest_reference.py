#!/usr/bin/env python3
"""Actual Rust service/stream + synthetic JPEGs -> ingest -> FFmpeg -> two WebRTC readers.

No camera HAL provider/hardware is used. Optional hosted systemd mode executes the
generated ingest unit and proves whole-cgroup cleanup after abrupt main-process death.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import time
import shutil
import socket
import ssl
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiortc import RTCSessionDescription
from revocation_reference import Reader, digest, private_write, require
from rust_camera import RustCamera, validate_viewer_sharing


async def until(test, message, seconds=25):
    deadline = asyncio.get_running_loop().time()+seconds
    while asyncio.get_running_loop().time() < deadline:
        if await test():
            return
        await asyncio.sleep(.1)
    raise AssertionError(message)


async def exercise(gateway, ingest, mediamtx, camera_source, managed=False, secure=False, *, detections=False, recording=None, sdk_camera=False, media_sandbox=None, ffmpeg=None):
    require(os.name == 'posix' and Path('/proc/self').exists(), 'Linux process-lifecycle fixture required')
    if managed:
        from systemd_ingest import ManagedIngest, require_hosted
        require_hosted()  # Before creating files or modifying any runtime units.
    if sdk_camera:
        require(secure and not managed, 'SDK endpoint requires the unprivileged authenticated fixture')
    if recording:
        require(secure and not managed, 'encrypted recording requires the authenticated unprivileged fixture')
    ffmpeg=Path(ffmpeg or shutil.which('ffmpeg')).resolve()
    parent = str(Path(recording['volume']['mount'])/'qualification') if recording else ('/run' if managed else None)
    camera_id = 'cam_one' if recording else 'cam_reference'
    receipt = None
    with tempfile.TemporaryDirectory(prefix='nxrs-ingest-', dir=parent) as directory:
        root = Path(directory)
        if managed:
            root.chmod(0o755)  # Executable traversal only; secret files themselves remain 0600.
        # Executables may be traversed by the dynamic ingest user, but mutable
        # controller state must retain its own PRIVATE parent at every stage.
        state = root/'control-state'
        state.mkdir(mode=0o700)
        used = set()
        def port(udp=False):
            while True:
                with socket.socket(type=socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
                    sock.bind(('127.0.0.1', 0)); value = sock.getsockname()[1]
                if value not in used:
                    used.add(value); return value
        api, auth, media, rtsp, management = [port() for _ in range(5)]
        ice = port(True)
        tokens = {name: secrets.token_hex(32) for name in ['viewer', 'publisher', 'management', 'recorder']}
        base, origin = f'http://127.0.0.1:{api}', f'https://127.0.0.1:{media}'
        internal = f'http://127.0.0.1:{auth}'
        private_write(state/'revocations.json', json.dumps({'version': 1, 'revoked': []}))
        private_write(root/'security.json', json.dumps({'revocations_file': str(state/'revocations.json'),
            'media_api': f'127.0.0.1:{management}', 'media_api_token': tokens['management']}))
        private_write(root/'publisher.token', tokens['publisher']+'\n')
        private_write(root/'control.json', json.dumps({
            'listen': f'127.0.0.1:{api}', 'auth_listen': f'127.0.0.1:{auth}',
            'credential_window': {'not_before': int(time.time())-1, 'expires_at': int(time.time())+3600},
            'grant_ttl_seconds': 60, 'max_grants': 128, 'media_origins': [origin],
            'cameras': [dict(camera_id=camera_id, gateway_id='gw_reference', site_id='site_reference',
                name='SYNTHETIC images through actual Rust camera server', source='synthetic', stream_path='reference',
                whep_endpoint=origin+'/reference/whep')],
            'viewers': [dict(viewer_id='viewer', site_id='site_reference', camera_ids=[camera_id],
                token_sha256=digest(tokens['viewer']))],
            'publishers': [dict(username='publisher_reference', camera_id=camera_id, token_sha256=digest(tokens['publisher']))]
        }))
        recorder = None
        if recording:
            from recording_ingest import Recorder
            recorder = Recorder(root, recording, camera_id, tokens, rtsp)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(root/'key.pem'), '-out', str(root/'cert.pem'), '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        (root/'key.pem').chmod(0o600)
        tls = ssl.create_default_context(cafile=str(root/'cert.pem'))
        private_write(root/'media.json', json.dumps({
            'logLevel': 'error', 'authMethod': 'http', 'authHTTPAddress': internal+'/internal/mediamtx/auth',
            'authHTTPExclude': [], 'api': True, 'apiAddress': f'127.0.0.1:{management}',
            'metrics': False, 'pprof': False, 'playback': False, 'rtmp': False, 'hls': False, 'srt': False, 'moq': False,
            'rtsp': True, 'rtspAddress': f'127.0.0.1:{rtsp}', 'rtspTransports': ['tcp'], 'rtspAuthMethods': ['basic'],
            'webrtc': True, 'webrtcAddress': f'127.0.0.1:{media}', 'webrtcEncryption': True,
            'webrtcServerKey': str(root/'key.pem'), 'webrtcServerCert': str(root/'cert.pem'),
            'webrtcLocalUDPAddress': f'127.0.0.1:{ice}', 'webrtcIPsFromInterfaces': False,
            'webrtcAdditionalHosts': ['127.0.0.1'], 'webrtcICEServers2': [],
            'paths': {'reference': {'source': 'publisher', 'overridePublisher': False, 'maxReaders': 4}}
        }))
        processes, handles, readers = [], [], []
        source, service = None, None
        adapter = None
        control,media_process = None,None
        def launch(args, label):
            handle = open(root/(label+'.log'), 'wb'); os.chmod(root/(label+'.log'), 0o600); handles.append(handle)
            child = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT)
            processes.append(child)
            return child
        def children(process):
            pid = process.pid
            file = Path(f'/proc/{pid}/task/{pid}/children')
            return set(map(int, file.read_text().split())) if file.exists() else set()
        bridge = None
        try:
            if sdk_camera:
                from device_tls_camera import SdkCamera
                source = await SdkCamera.start(camera_source, root, root/'control.json', camera_id=camera_id, motion=bool(recording),
                    case='detection-metadata' if detections else 'video')
            else:
                source = await RustCamera.start(camera_source, root, detections=detections, motion=bool(recording))
            private_write(root/'ingest.json', json.dumps({
                'camera': f'127.0.0.1:{source.port}', 'allow_insecure_camera_http': True,
                'rtsp': f'127.0.0.1:{rtsp}', 'stream_path': 'reference', 'publisher_user': 'publisher_reference',
                'publisher_token_file': str(root/'publisher.token'), 'ffmpeg': str(ffmpeg),
                'publisher': str(ingest.parent/'nxrs-rtsp-publisher'),
                'media_sandbox': str(media_sandbox) if media_sandbox else None,
                'width': 320, 'height': 240, 'frame_timeout_ms': 2000
            }))
            if detections:
                config=json.loads((root/'ingest.json').read_text()); config['detections']={'control':f'127.0.0.1:{auth}','camera_id':camera_id}
                (root/'ingest.json').write_text(json.dumps(config))
            if secure:
                if sdk_camera:
                    staged = source.stage(root/'ingest.json')
                else:
                    from tls_camera import TlsCameraBridge
                    bridge = await TlsCameraBridge.start(root,source,root/'control.json',camera_id=camera_id)
                    config = json.loads((root/'ingest.json').read_text())
                    config['camera'] = f'127.0.0.1:{bridge.port}'
                    (root/'ingest.json').write_text(json.dumps(config))
                    staged = bridge.stage(root/'ingest.json')
                (root/'ingest.json').write_bytes(staged.read_bytes())
            control=launch([str(gateway), str(root/'control.json'), '--security', str(root/'security.json')]+(['--require-encrypted-storage'] if recording else []), 'control')
            media_process=launch([str(mediamtx), str(root/'media.json')], 'mediamtx')
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                async def ready():
                    try:
                        async with http.get(internal+'/internal/viewers/viewer/revocation') as r:
                            return r.status == 200 and (await r.json())['media_control_ready']
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        return False
                await until(ready, 'media authorization ready')
                if recorder:
                    await recorder.start(http, base, launch)
                if managed:
                    service = ManagedIngest(root, ingest)
                    service.start()
                    adapter = service
                else:
                    adapter = launch([str(ingest), str(root/'ingest.json')]+(['--require-camera-tls'] if secure else []), 'ingest')
                async def publishing():
                    async with http.get(f'http://127.0.0.1:{management}/v3/paths/get/reference',
                            headers={'Authorization': 'Bearer '+tokens['management']}) as r:
                        return r.status == 200 and (await r.json()).get('ready') is True
                await until(publishing, 'actual Rust camera output encoded and published')
                source_before_viewers = await source.command('status')
                require(source_before_viewers['active'] == 1 and source_before_viewers['peak'] == 1,
                        f'publication owns one actual source: {source_before_viewers}')
                async def connect():
                    reader = Reader(); readers.append(reader)
                    await reader.peer.setLocalDescription(await reader.peer.createOffer())
                    async with http.post(base+f'/v1/cameras/{camera_id}/playback-grants', json={},
                            headers={'Authorization': 'Bearer '+tokens['viewer']}) as r:
                        require(r.status == 201, 'scoped playback grant'); grant = await r.json()
                    async with http.post(grant['endpoint'], data=reader.peer.localDescription.sdp,
                            headers={'Authorization': 'Bearer '+grant['access_token'], 'Content-Type': 'application/sdp'},
                            ssl=tls, allow_redirects=False) as r:
                        require(r.status == 201, 'MJPEG-source WHEP negotiation')
                        answer = await r.text(); reader.session_url = urljoin(grant['endpoint'], r.headers['Location'])
                        require(urlsplit(reader.session_url).netloc == urlsplit(origin).netloc, 'same-origin media session')
                    await reader.peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
                    async def advancing(): return reader.count >= 5
                    await until(advancing, 'five advancing decoded H264 frames per reader')
                    return reader
                a, b = await connect(), await connect()
                confirmed_detection=[];unavailable_detection=[]
                async def detection_presence(presence,previous=None):
                    from detection_observer import observe
                    value=await observe(http,base+f'/v1/cameras/{camera_id}/detections',tokens['viewer'],camera_id,
                        presence,different_from=previous)
                    (confirmed_detection if presence=='person' else unavailable_detection).append(value)
                    return value
                if sdk_camera and detections:await detection_presence('person')
                counters = await source.command('status')
                validate_viewer_sharing(source_before_viewers, counters)
                child_before = children(adapter); require(len(child_before) == 2, 'one encoder and one publisher owned')
                for pid in child_before | {adapter.pid}:
                    for entry in ['cmdline','environ']:
                        data=Path(f'/proc/{pid}/{entry}').read_bytes()
                        require(all(value.encode() not in data for value in tokens.values()),
                                'no credential in actual process argv/environment')
                print('PASS: actual encoder, publisher and adapter argv/environment contain no credentials',flush=True)
                if service:
                    service.assert_sandbox()
                    print('PASS: generated ingest and encoder share an unprivileged bounded service cgroup', flush=True)
                print('PASS: actual Rust service/stream serves two advancing 320x240 WebRTC readers through one ingest connection', flush=True)
                if recorder:
                    receipt = await recorder.observe(http, base)
                lifecycle=None
                if sdk_camera:
                    initial_generation=(await source.command('status'))['control_generation']
                    async def advancing_pair():
                        start=(a.count,b.count)
                        async def advanced():return a.count>=start[0]+3 and b.count>=start[1]+3
                        await until(advanced,'both decoded viewers advance after live SDK control')
                    for candidate in [b'invalid CRL',source.initial_crl[:-1]+bytes([source.initial_crl[-1]^1])]:
                        status=await source.apply_crl(candidate)
                        require(status['control_result']=='refused' and status['control_generation']==initial_generation,
                                'malformed/forged live CRL cannot replace healthy trust')
                        await advancing_pair()
                    status=await source.command('refresh')
                    require(status['control_result']=='refreshed' and status['control_refreshes']==1,'live SDK entropy refresh executed')
                    await advancing_pair()
                    before=children(adapter)
                    status=await source.advance_trust()
                    require(status['control_result']=='applied' and status['control_generation']>initial_generation,
                            'signed live SDK trust generation accepted')
                    async def policy_recovered():return before.isdisjoint(children(adapter)) and await publishing()
                    await until(policy_recovered,'signed SDK trust update retires old publication and reconnects')
                    await a.close();await b.close()
                    a,b=await connect(),await connect()
                    if detections:await detection_presence('person',confirmed_detection[-1]['session'])
                    generation=status['control_generation']
                    status=await source.apply_crl(source.initial_crl)
                    require(status['control_result']=='refused' and status['control_generation']==generation,'live SDK rollback refused')
                    await advancing_pair()
                    counters=await source.command('status')
                    require(counters['control_applied']==1 and counters['control_refused']==3
                            and counters['control_refreshes']==1 and counters['peak']==1,'all live SDK controls executed')
                    child_before=children(adapter)
                    require(len(child_before)==2,'live-policy recovery owns one encoder and publisher')
                    lifecycle=dict(malformed_forged_rollback_refusals=3,entropy_refreshes=1,
                                   signed_update_reconnect_executed=True)
                    print('PASS: malformed/forged/rollback CRLs refused with decoded video advancing; signed live trust reconnects through the real SDK',flush=True)
                await source.command('pause')
                async def retired(): return child_before.isdisjoint(children(adapter)) and not await publishing()
                await until(retired, 'source stall retires encoder/publication', seconds=10)
                await asyncio.sleep(1.5); counts = (a.count, b.count); await asyncio.sleep(1)
                require((a.count, b.count) == counts, 'stalled source must not manufacture live frames')
                print('PASS: actual server source stall removes encoder/publication and stops video', flush=True)
                if sdk_camera and detections:await detection_presence('unavailable')
                await source.command('resume')
                await until(publishing, 'publication recovers after source reconnect')
                c = await connect()
                counters = await source.command('status')
                # Real TCP half-close retains a response until idle/write timeout.
                # A rejected retry is not an admission. Per-poll conservation and
                # unchanged recovery deadlines enforce the actual ownership contract.
                print('SOURCE RECOVERY COUNTERS:', json.dumps(counters, sort_keys=True), flush=True)
                require(c.count >= 5 and counters['connections'] >= 2
                        and counters['active'] == 1 and counters['peak'] == 1,
                        'new capture sequence epoch restored with one admitted source')
                require(len(children(adapter)) == 2, 'recovery owns exactly one encoder and publisher')
                print('PASS: new source sequence epoch restores decoded video without overlapping source admissions', flush=True)
                if sdk_camera and detections:await detection_presence('person',confirmed_detection[-1]['session'])
                child_before = children(adapter)
                adapter.terminate(); await asyncio.to_thread(adapter.wait, timeout=12)
                require(adapter.returncode == 0, 'graceful adapter termination')
                async def disconnected(): return (await source.command('status'))['active'] == 0
                await until(disconnected, 'source socket closed on shutdown', seconds=5)
                require(all(not Path(f'/proc/{pid}').exists() for pid in child_before), 'encoder reaped on shutdown')
                print('PASS: SIGTERM closes actual source and reaps encoder before exit', flush=True)
                if sdk_camera:
                    await a.close();await b.close();await c.close()
                    adapter=launch([str(ingest),str(root/'ingest.json'),'--require-camera-tls'],'ingest-revocation')
                    await until(publishing,'explicit SDK ingest restart before peer revocation')
                    d=await connect();revoked_children=children(adapter)
                    require(len(revoked_children)==2,'revocation phase owns encoder and publisher')
                    status=await source.advance_trust(revoke=True)
                    require(status['control_result']=='applied' and status['control_applied']==2,'signed live ingest-peer revocation applied')
                    async def publication_gone():return not await publishing()
                    await until(publication_gone,'revoked SDK ingest peer loses publication')
                    await asyncio.sleep(1.5);count=d.count;await asyncio.sleep(1)
                    require(d.count==count,'revoked SDK peer cannot continue decoded video')
                    if detections:await detection_presence('unavailable')
                    baseline=await source.command('status');frozen=baseline['frames'];connections=baseline['connections']
                    async def denied_again():
                        current=await source.command('status')
                        return current['connections']>connections and current['disconnected']>=current['connections']
                    await until(denied_again,'fresh revoked SDK peer admission refused',seconds=10)
                    final=await source.command('status')
                    require(final['frames']==frozen and final['peak']==1,'revoked SDK reconnect emits no additional media')
                    require(all(not Path(f'/proc/{pid}').exists() for pid in revoked_children),'peer revocation reaps encoder and publisher')
                    adapter.terminate();await asyncio.to_thread(adapter.wait,timeout=12)
                    require(adapter.returncode==0,'revocation-phase adapter exits cleanly')
                    await until(disconnected,'revoked retry socket closed on shutdown',seconds=5)
                    lifecycle.update(signed_peer_revocation_stops_media=True,revoked_peer_media_frames=0,
                                     revocation_viewer_frames=d.count)
                    print('PASS: signed live SDK peer revocation stops decoded video; fresh reconnect emits no media',flush=True)
                if sdk_camera:
                    stopped = await source.command('status')
                    media_receipt = dict(schema=1, scope='actual-host-sdk-camera-gateway-media', qualified=True,
                        initial_viewer_frames=[a.count,b.count], recovered_viewer_frames=c.count,
                        source_connections=stopped['connections'], source_peak=stopped['peak'],
                        source_frames=stopped['frames'], stopped_source_active=stopped['active'],
                        source_stall_reconnect_executed=True, credentials_absent_from_argv_environment=True,
                        encoder_publisher_children_reaped=True, sdk_endpoint_used=True,
                        encoder_isolation_executed=bool(media_sandbox),
                        live_controls=lifecycle,
                        device_tls_runtime_qualified=False, hardware_entropy_qualified=False,
                        protected_device_key_storage_qualified=False, trusted_time_acquisition_qualified=False,
                        protected_generation_qualified=False, physical_qualified=False,
                        fips_validated=False, deployment_permitted=False)
                    if detections:
                        from detection_observer import evidence,validate
                        metadata=evidence(confirmed_detection,unavailable_detection);validate(metadata)
                        media_receipt['detection_api']=metadata
                        print('PASS: actual SDK TLS detection reaches authenticated API; trust/stall change source namespaces; stall/revocation expire detection',flush=True)
                    if recording:
                        require(receipt is not None, 'authenticated encoded recording receipt required')
                        receipt.update(sdk_media=media_receipt, sdk_endpoint_used=True, camera_tls_front_end_test_only=False)
                    else: receipt=media_receipt
                if recorder:
                    await recorder.close()
                    require(receipt is not None, 'authenticated encoded recording receipt required')
                if service:
                    service.start(); await until(publishing, 'explicit ingest service restart')
                    d = await connect(); service.assert_sandbox()
                    old_pids = service.group_pids()
                    service.kill_main()
                    async def group_empty():
                        return not service.group_pids() and all(not Path(f'/proc/{p}').exists() for p in old_pids)
                    await until(group_empty, 'SIGKILL must remove encoder and empty service cgroup', seconds=15)
                    await until(disconnected, 'abrupt service death releases camera connection', seconds=5)
                    await asyncio.sleep(1.5); count = d.count; await asyncio.sleep(1)
                    require(d.count == count and not await publishing(), 'crashed ingest cannot leave live publication')
                    require(service.property('ActiveState') == 'failed', 'failed ingest not silently restarted')
                    print('PASS: SIGKILL of ingest main removes encoder, empties cgroup and stops decoded video', flush=True)
                    service.start(); await until(publishing, 'manual recovery after ingest crash')
                    await connect()
                    old_pids = service.group_pids()
                    require(len(old_pids) == 3, 'recovered service owns adapter, encoder and publisher')
                    service.stop_dependency()
                    await until(group_empty, 'dependency stop retires all ingest processes', seconds=15)
                    await until(disconnected, 'dependency stop releases source', seconds=5)
                    print('PASS: explicit crash recovery restores video; dependency loss stops entire ingest cgroup', flush=True)
        except Exception:
            if recorder:
                print('SDK_RECORDING_OBSERVATIONS '+json.dumps(recorder.failure_observations(),sort_keys=True),flush=True)
            from ingest_diagnostics import report_failure
            await report_failure(root, source, adapter, sdk_camera=sdk_camera,
                control=control, media=media_process, readers=readers)
            raise
        finally:
            failures = []
            for reader in readers:
                try: await reader.close()
                except Exception: failures.append('reader')
            if service:
                try: service.close()
                except Exception: failures.append('service')
            for child in reversed(processes):
                try:
                    if child.poll() is None:
                        child.terminate()
                        try: await asyncio.to_thread(child.wait, timeout=8)
                        except subprocess.TimeoutExpired: child.kill(); await asyncio.to_thread(child.wait, timeout=5)
                except Exception: failures.append('process')
            if bridge:
                try: await bridge.close()
                except Exception: failures.append('TLS fixture')
            if source:
                try: await source.close()
                except Exception: failures.append('camera owner')
            for handle in handles:
                try: handle.close()
                except Exception: failures.append('log')
            require(not failures, 'owned ingest fixture cleanup incomplete')
            for log in root.glob('*.log'):
                data=log.read_bytes()
                require(all(value.encode() not in data for value in tokens.values()),'no credential in owned process logs')
        if sdk_camera: print('SDK TLS INGEST PASS: actual SDK key/TLS worker, Rust framing, encoder, two decoded viewers and reconnect; host fixtures only',flush=True)
        elif secure: print('TLS INGEST PASS: real mutual TLS to a test-only front end; no device TLS/entropy claim',flush=True)
        print('INGEST REFERENCE PASS: actual Rust stream implementation with synthetic images; not physical camera/iPhone/VPN', flush=True)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['gateway', 'ingest', 'mediamtx', 'camera-source']:
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--media-sandbox',type=Path)
    parser.add_argument('--ffmpeg',type=Path,help='actual production encoder/recorder executable')
    parser.add_argument('--media-runtime',type=Path,help='qualified minimized native runtime with its public receipts')
    parser.add_argument('--systemd', action='store_true', help='disposable GitHub-hosted runner only')
    parser.add_argument('--detections', action='store_true', help='qualify in-band metadata and both child lifetimes')
    parser.add_argument('--camera-tls',action='store_true',help='require authenticated camera TLS in the host fixture')
    parser.add_argument('--sdk-camera',action='store_true',help='actual SDK endpoint binary; unprivileged host fixture only')
    parser.add_argument('--sdk-qualification',type=Path,help='public SDK qualification JSON binding the endpoint binary')
    parser.add_argument('--sdk-report',type=Path,help='bounded public host media evidence output')
    parser.add_argument('--storage-volume',type=Path)
    parser.add_argument('--luks-uuid')
    parser.add_argument('--recording-worker',type=Path)
    parser.add_argument('--recording-receipt',type=Path)
    args = parser.parse_args()
    if args.sdk_camera and (not args.camera_tls or args.systemd or not args.sdk_qualification or not args.sdk_report):
        parser.error('--sdk-camera requires --camera-tls, --sdk-qualification, --sdk-report and an unprivileged fixture')
    values=[args.storage_volume,args.luks_uuid,args.recording_worker,args.recording_receipt]
    if any(values) and (not all(values) or not args.camera_tls or args.systemd):
        parser.error('complete authenticated encrypted-recording fixture resources required')
    selected_ffmpeg=(args.ffmpeg or (args.media_runtime/'bin/ffmpeg' if args.media_runtime else Path(shutil.which('ffmpeg')))).resolve()
    recording = dict(ffmpeg=selected_ffmpeg,volume=dict(layout_version=2,mount=str(args.storage_volume.resolve()),
                      luks_uuid=args.luks_uuid),worker=args.recording_worker.resolve(),
                      reader=args.recording_worker.resolve().parent/'nxrs-rtsp-reader',
                      media_sandbox=args.media_sandbox.resolve() if args.media_sandbox else None) if all(values) else None
    try:
        if args.sdk_camera:
            import hashlib
            repository=Path(__file__).resolve().parents[2]
            from sdk_media_evidence import read_sdk, read_media, read_python, source_names, validate_media
            print('SDK_MEDIA_PHASE preflight',flush=True)
            sdk_raw,sdk_evidence=read_sdk(repository,args.sdk_qualification,args.camera_source)
            media_engine=read_media(args.mediamtx)
            python_environment=read_python()
            names=source_names(repository,sdk_evidence)
            inputs={name:hashlib.sha256((repository/name).read_bytes()).hexdigest() for name in sorted(names)}
            executable_paths=dict(gateway=args.gateway,ingest=args.ingest,publisher=args.ingest.parent/'nxrs-rtsp-publisher',
                camera=args.camera_source,mediamtx=args.mediamtx,ffmpeg=selected_ffmpeg)
            if args.media_sandbox: executable_paths['media_sandbox']=args.media_sandbox.resolve()
            executables={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in executable_paths.items()}
            runtime_evidence=None
            if args.media_runtime:
                from gateway_media_evidence import verify as verify_native_runtime
                runtime_evidence=verify_native_runtime(repository,args.media_runtime,ffmpeg=selected_ffmpeg,
                    publisher=executable_paths['publisher'],reader=args.ingest.parent/'nxrs-rtsp-reader')
            checkout=None
            if not recording:
                checkout=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repository,text=True,timeout=30).strip()
                require(not subprocess.check_output(['git','status','--porcelain'],cwd=repository,text=True,timeout=30).strip(),
                        'full media qualification requires a clean source checkout')
            else:
                require(args.sdk_report.resolve()==Path(recording['volume']['mount'])/'maintenance/sdk-media.json',
                        'SDK media receipt belongs to owned encrypted volume')
        if args.sdk_camera: print('SDK_MEDIA_PHASE execution',flush=True)
        result = asyncio.run(exercise(args.gateway.resolve(), args.ingest.resolve(), args.mediamtx.resolve(),
                             args.camera_source.resolve(), args.systemd, args.camera_tls,detections=args.detections,recording=recording,sdk_camera=args.sdk_camera,media_sandbox=args.media_sandbox.resolve() if args.media_sandbox else None,ffmpeg=selected_ffmpeg))
        if args.sdk_camera:
            print('SDK_MEDIA_PHASE receipt',flush=True)
            media=result['sdk_media'] if recording else result
            validate_media(media)
            require(inputs=={name:hashlib.sha256((repository/name).read_bytes()).hexdigest() for name in inputs}, 'media sources changed during execution')
            require(executables=={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in executable_paths.items()},
                    'media executable changed during execution')
            if checkout is not None:
                require(subprocess.check_output(['git','rev-parse','HEAD'],cwd=repository,text=True,timeout=30).strip()==checkout
                        and not subprocess.check_output(['git','status','--porcelain'],cwd=repository,text=True,timeout=30).strip(),
                        'media checkout changed during execution')
                media['checkout']=dict(commit=checkout,clean=True)
            else:
                # The outer hosted fixture binds the original clean checkout;
                # this unprivileged copy verifies staged source bytes only.
                media['staged_sources_verified']=True
            require(media_engine==read_media(args.mediamtx), 'media engine evidence changed during execution')
            require(python_environment==read_python(), 'Python environment changed during execution')
            if args.media_runtime:
                require(runtime_evidence==verify_native_runtime(repository,args.media_runtime,ffmpeg=selected_ffmpeg,
                    publisher=executable_paths['publisher'],reader=args.ingest.parent/'nxrs-rtsp-reader'),'native runtime changed during media execution')
                media['media_runtime']=runtime_evidence
            media.update(inputs=inputs,executables=executables,media_engine=media_engine,python_environment=python_environment,
                         sdk_qualification_sha256=hashlib.sha256(sdk_raw).hexdigest())
            validate_media(media)
            require(len(json.dumps(result).encode())<=65536, 'bounded public media evidence')
            private_write(args.sdk_report,json.dumps(media,sort_keys=True))
        if recording:
            expected=Path(recording['volume']['mount'])/'maintenance/secure-ingest.json'
            require(args.recording_receipt.resolve()==expected, 'receipt belongs to owned encrypted volume')
            private_write(expected,json.dumps(result))
    except Exception as error:
        from sdk_media_evidence import failure_locations
        print('SDK_MEDIA_FAILURE_LOCATION '+json.dumps(failure_locations(error),sort_keys=True),flush=True)
        print('INGEST REFERENCE FAILED:', str(error) if isinstance(error, AssertionError) else type(error).__name__, flush=True)
        raise SystemExit(1) from None
