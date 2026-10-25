#!/usr/bin/env python3
"""Owned synthetic native RTSP backend revocation. No personal device or service."""
import argparse
import base64
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from activity_reference import private, port, process_ids
from credential_process import assert_boundary, descendants


def require(condition, category):
    if not condition:raise AssertionError(category)


def until(check, category, seconds=12):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if check():return
        time.sleep(.1)
    raise AssertionError(category)


def rtsp_status(port, method, user, token):
    """Actual bounded RTSP admission response; never place a credential in a URL."""
    body=(b'v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=owned\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n'
          b'm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\n') if method=='ANNOUNCE' else b''
    credential=base64.b64encode((user+':'+token).encode()).decode()
    message=(f'{method} rtsp://127.0.0.1:{port}/one RTSP/1.0\r\nCSeq: 1\r\n'
        f'Authorization: Basic {credential}\r\nContent-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body
    # The pinned media server deliberately delays authentication refusals
    # by up to four seconds. Bound this wire witness beyond that policy.
    with socket.create_connection(('127.0.0.1',port),timeout=6) as connection:
        connection.sendall(message);raw=bytearray()
        while b'\r\n\r\n' not in raw:
            data=connection.recv(2048);require(bool(data),'RTSP response header available');raw.extend(data)
            require(len(raw)<=16384,'bounded RTSP response header')
    line=raw.split(b'\r\n',1)[0].split()
    require(len(line)>=2 and line[0]==b'RTSP/1.0' and len(line[1])==3 and line[1].isdigit(),'RTSP response status')
    return int(line[1])


class Fixture:
    def __init__(self, root, gateway, publisher, reader, mediamtx, ffmpeg):
        self.root=root;self.gateway=gateway;self.publisher=publisher;self.reader=reader
        self.mediamtx=mediamtx;self.ffmpeg=ffmpeg
        self.processes=[];self.files=[];self.threads=[]
        self.used=set()
        def allocate():
            value=port()
            while value in self.used:value=port()
            self.used.add(value);return value
        self.api,self.auth,self.rtsp,self.management,self.web,self.udp=[allocate() for _ in range(6)]
        self.tokens={key:secrets.token_hex(32) for key in ['owner','publisher_a','publisher_b','recorder_a','recorder_b','management']}
        self.secrets=list(self.tokens.values())
        self.user={key:key for key in ['publisher_a','publisher_b','recorder_a','recorder_b']}
        self.state=root/'state';self.state.mkdir(mode=0o700)
        private(self.state/'revocations.json',dict(version=1,revoked=[]))
        private(root/'security.json',dict(revocations_file=str(self.state/'revocations.json'),
            media_api=f'127.0.0.1:{self.management}',media_api_token=self.tokens['management']))
        result=subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem'),'-subj','/CN=localhost',
            '-addext','subjectAltName=IP:127.0.0.1'],capture_output=True,timeout=20)
        require(result.returncode==0,'fixture TLS certificate')
        os.chmod(root/'key.pem',0o600)
        private(root/'media.json',dict(logLevel='warn',authMethod='http',authHTTPExclude=[],
            authHTTPAddress=f'http://127.0.0.1:{self.auth}/internal/mediamtx/auth',
            api=True,apiAddress=f'127.0.0.1:{self.management}',metrics=False,pprof=False,playback=False,
            rtsp=True,rtspAddress=f'127.0.0.1:{self.rtsp}',rtspTransports=['tcp'],rtspAuthMethods=['basic'],
            rtmp=False,hls=False,srt=False,moq=False,webrtc=True,webrtcEncryption=True,
            webrtcAddress=f'127.0.0.1:{self.web}',webrtcLocalUDPAddress=f'127.0.0.1:{self.udp}',
            webrtcServerKey=str(root/'key.pem'),webrtcServerCert=str(root/'cert.pem'),
            webrtcIPsFromInterfaces=False,webrtcAdditionalHosts=['127.0.0.1'],webrtcICEServers2=[],
            paths={name:dict(source='publisher',overridePublisher=False) for name in ['one','two']}))
        now=int(time.time());self.window=dict(not_before=now-1,expires_at=now+3600)
        self.configure()

    def configure(self):
        digest=lambda token:hashlib.sha256(token.encode()).hexdigest()
        cfg=dict(listen=f'127.0.0.1:{self.api}',auth_listen=f'127.0.0.1:{self.auth}',
            credential_window=self.window,grant_ttl_seconds=60,max_grants=32,media_origins=['https://media.test'],
            cameras=[dict(camera_id='cam_'+name,gateway_id='gw',site_id='site',name='SYNTHETIC '+name,
                source='synthetic',stream_path=path,whep_endpoint='https://media.test/'+path+'/whep')
                for name,path in [('a','one'),('b','two')]],
            viewers=[dict(viewer_id='owner',site_id='site',camera_ids=['cam_a','cam_b'],token_sha256=digest(self.tokens['owner']))],
            publishers=[dict(username=self.user['publisher_'+name],camera_id='cam_'+name,
                token_sha256=digest(self.tokens['publisher_'+name])) for name in ['a','b']],
            recorders=[dict(username=self.user['recorder_'+name],camera_id='cam_'+name,
                token_sha256=digest(self.tokens['recorder_'+name])) for name in ['a','b']],
            activity=dict(directory=str(self.root/'activity'),camera_ids=['cam_a','cam_b'],
                retention_seconds=86400,max_bytes=64*1024*1024,max_events=100))
        path=self.root/'config.json'
        if path.exists():path.write_text(json.dumps(cfg))
        else:private(path,cfg)

    def launch(self, args, name, **kwargs):
        log=open(self.root/name,'wb');os.chmod(self.root/name,0o600);self.files.append(log)
        process=subprocess.Popen([str(v) for v in args],stderr=log,stdout=log,
            env={'PATH':'/usr/bin:/bin','LANG':'C'},**kwargs)
        self.processes.append(process);return process

    def request(self, path, *, method='GET', body=None, public=False):
        conn=http.client.HTTPConnection('127.0.0.1',self.api if public else self.auth,timeout=2)
        headers={'Authorization':'Bearer '+self.tokens['owner']} if public else {}
        if body is not None:headers['Content-Type']='application/json';body=json.dumps(body).encode()
        try:
            conn.request(method,path,body,headers);reply=conn.getresponse();raw=reply.read(16385)
            require(len(raw)<=16384,'bounded fixture response')
            return reply.status,json.loads(raw) if raw else None
        finally:conn.close()

    def start(self):
        self.control=self.launch([self.gateway,self.root/'config.json','--security',self.root/'security.json'],'control-'+str(len(self.processes))+'.log')
        self.media=self.launch([self.mediamtx,self.root/'media.json'],'media-'+str(len(self.processes))+'.log')
        def ready():
            require(self.control.poll() is None and self.media.poll() is None,'strict owners remain running')
            try:
                code,value=self.request('/internal/viewers/owner/revocation')
                return code==200 and value['media_control_ready'] and value['persistence_healthy']
            except OSError:return False
        until(ready,'complete WebRTC and RTSP management readiness')

    def publish(self, name, *, wait_ready=True):
        path='one' if name=='a' else 'two';key='publisher_'+name
        log=open(self.root/('source-'+name+'-'+str(len(self.processes))+'.log'),'wb');os.chmod(log.name,0o600);self.files.append(log)
        source=subprocess.Popen([str(self.ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-re','-f','lavfi',
            '-i','testsrc2=s=160x120:r=15','-an','-c:v','libx264','-threads','1','-preset','ultrafast',
            '-tune','zerolatency','-g','15','-bf','0','-f','mpegts','pipe:1'],stdout=subprocess.PIPE,stderr=log,
            env={'PATH':'/usr/bin:/bin','LANG':'C'})
        self.processes.append(source)
        log_name='publisher-'+name+'-'+str(len(self.processes))+'.log'
        native=self.launch([self.publisher,f'127.0.0.1:{self.rtsp}',self.user[key],path],
            log_name,stdin=subprocess.PIPE)
        def relay():
            try:
                native.stdin.write(self.tokens[key].encode());native.stdin.flush()
                while data:=source.stdout.read(8192):
                    native.stdin.write(data);native.stdin.flush()
            except (BrokenPipeError,OSError):pass
            finally:
                with contextlib.suppress(BrokenPipeError,OSError):native.stdin.close()
                source.stdout.close()
        thread=threading.Thread(target=relay);thread.start();self.threads.append(thread)
        native.qualification_log=self.root/log_name
        if wait_ready:
            def ready():
                require(native.poll() is None,'native publisher remains running')
                with (self.root/log_name).open('rb') as log:raw=log.read(16384)
                return any(line.startswith(b'packet=') for line in raw.splitlines())
            until(ready,'native publisher produced its first authenticated packet')
        return native

    def read(self, name):
        path='one' if name=='a' else 'two';key='recorder_'+name
        # stdout belongs to the separate, credential-free decoder.
        native_log=open(self.root/('reader-'+name+'-'+str(len(self.processes))+'.log'),'wb')
        os.chmod(native_log.name,0o600);self.files.append(native_log)
        native=subprocess.Popen([str(self.reader),f'127.0.0.1:{self.rtsp}',self.user[key],path],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=native_log,
            env={'PATH':'/usr/bin:/bin','LANG':'C'})
        self.processes.append(native)
        native.stdin.write(self.tokens[key].encode());native.stdin.close()
        decoder_log=open(self.root/('decoder-'+name+'-'+str(len(self.processes))+'.log'),'wb')
        os.chmod(decoder_log.name,0o600);self.files.append(decoder_log)
        decoder=subprocess.Popen([str(self.ffmpeg),'-nostdin','-hide_banner','-loglevel','error',
            '-f','mpegts','-i','pipe:0','-an','-vf','fps=2,scale=64:48','-pix_fmt','gray','-f','rawvideo','pipe:1'],
            stdin=native.stdout,stdout=subprocess.PIPE,stderr=decoder_log,
            env={'PATH':'/usr/bin:/bin','LANG':'C'})
        native.stdout.close();self.processes.append(decoder)
        progress={'frames':0}
        def consume():
            frame=bytearray()
            while data:=decoder.stdout.read(3072-len(frame)):
                frame.extend(data)
                if len(frame)==3072:progress['frames']+=1;frame.clear()
            decoder.stdout.close()
        thread=threading.Thread(target=consume);thread.start();self.threads.append(thread)
        return native,decoder,thread,progress

    def revoke(self, role, principal):
        code,_=self.request('/internal/'+role+'/'+principal+'/revoke',method='POST',body={})
        require(code==202,'backend identity denial durably accepted')

    def stop(self):
        owned=set()
        for process in self.processes:
            if process.poll() is None:owned.add(process.pid);owned.update(descendants(process.pid))
        forced=False
        for process in reversed(self.processes):
            if process.poll() is None:
                process.send_signal(signal.SIGINT if process is getattr(self,'media',None) else signal.SIGTERM)
                try:process.wait(timeout=6)
                except subprocess.TimeoutExpired:forced=True;process.kill();process.wait(timeout=3)
        for thread in self.threads:thread.join(timeout=5)
        for file in self.files:file.close()
        require(not forced and all(not t.is_alive() for t in self.threads) and owned.isdisjoint(process_ids()),
            'all owned native processes and relays joined')
        self.processes.clear();self.threads.clear();self.files.clear()

    def inspect(self):
        inspected=False
        for process in self.processes:
            if process.poll() is None:
                assert_boundary(process.pid,self.secrets);inspected=True
        require(inspected,'actual native process metadata inspected')

    def closed(self):
        for log in self.root.glob('*.log'):
            data=log.read_bytes()
            require(all(token.encode() not in data for token in self.secrets),'backend credentials absent from owned logs')


def exercise(gateway,publisher,reader,mediamtx,ffmpeg):
    with tempfile.TemporaryDirectory(prefix='nxrs-backend-denial-') as directory:
        root=Path(directory).resolve();f=Fixture(root,gateway,publisher,reader,mediamtx,ffmpeg)
        observed=[]
        try:
            f.start();a=f.publish('a');b=f.publish('b');ra=f.read('a');rb=f.read('b')
            until(lambda:ra[3]['frames']>=12 and rb[3]['frames']>=12,'two independently decoded camera streams',20)
            f.inspect()
            require(rtsp_status(f.rtsp,'ANNOUNCE','publisher_a',f.tokens['publisher_a'])==200
                and rtsp_status(f.rtsp,'DESCRIBE','recorder_a',f.tokens['recorder_a'])==200,
                'actual positive RTSP admission controls')
            f.revoke('recorders','recorder_a')
            until(lambda:ra[0].poll() is not None,'active revoked native recorder retired')
            ra[2].join(timeout=8);require(not ra[2].is_alive(),'revoked decoder output joined')
            stopped=ra[3]['frames'];other=rb[3]['frames']
            until(lambda:rb[3]['frames']>=other+6,'unrelated recorder advances after recorder denial')
            require(ra[3]['frames']==stopped and a.poll() is None and b.poll() is None,'recorder denial preserves both publishers')
            require(rtsp_status(f.rtsp,'DESCRIBE','recorder_a',f.tokens['recorder_a'])==401,'actual retired recorder RTSP401')
            probe=subprocess.run([str(reader),f'127.0.0.1:{f.rtsp}','recorder_a','one'],
                input=f.tokens['recorder_a'].encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8,env={'PATH':'/usr/bin:/bin','LANG':'C'})
            require(probe.returncode!=0 and not probe.stdout,'retired recorder new admission refused without media')
            require(all(token.encode() not in probe.stderr for token in f.secrets),'rejected native reader keeps credential output private')
            f.revoke('publishers','publisher_a')
            until(lambda:a.poll() is not None,'active revoked native publisher retired')
            require(rtsp_status(f.rtsp,'ANNOUNCE','publisher_a',f.tokens['publisher_a'])==401,'actual retired publisher RTSP401')
            other=rb[3]['frames'];until(lambda:rb[3]['frames']>=other+6,'unrelated camera advances after publisher denial')
            require(b.poll() is None,'unrelated native publisher remains live')
            observed.append(rb[3]['frames']);f.inspect();f.stop();f.closed()
            snapshot=(f.state/'revocations.json').read_bytes()
            state=json.loads(snapshot);require(state['version']==2 and set(state['revoked'])=={'publisher_a','recorder_a'},'all denied identities persist in versioned state')
            for key in ['publisher_a','recorder_a']:
                f.tokens[key]=secrets.token_hex(32);f.secrets.append(f.tokens[key])
            f.configure();f.start();b=f.publish('b');rb=f.read('b')
            until(lambda:rb[3]['frames']>=12,'unrelated camera survives controller restart',20)
            # A new token for the SAME denied enrollment ID cannot restore it.
            require(rtsp_status(f.rtsp,'ANNOUNCE','publisher_a',f.tokens['publisher_a'])==401,'rotated denied publisher RTSP401')
            bad=f.publish('a',wait_ready=False);until(lambda:bad.poll() is not None,'rotated denied publisher refused')
            require(b'packet=' not in bad.qualification_log.read_bytes(),'denied publisher emits no packet progress')
            require(rtsp_status(f.rtsp,'DESCRIBE','recorder_a',f.tokens['recorder_a'])==401,'actual retired recorder RTSP401')
            probe=subprocess.run([str(reader),f'127.0.0.1:{f.rtsp}','recorder_a','one'],
                input=f.tokens['recorder_a'].encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8,env={'PATH':'/usr/bin:/bin','LANG':'C'})
            require(probe.returncode!=0 and not probe.stdout,'rotated denied recorder refused without media')
            require(all(token.encode() not in probe.stderr for token in f.secrets),'rotated native reader keeps credential output private')
            require((f.state/'revocations.json').read_bytes()==snapshot,'restart and credential changes preserve denial bytes')
            other=rb[3]['frames'];until(lambda:rb[3]['frames']>=other+6,'unrelated camera advances across denied credential rotation')
            observed.append(rb[3]['frames']);f.stop();f.closed()
            # Controlled recovery enrolls explicitly new backend identities,
            # retaining the old denied identities and activation window.
            f.user['publisher_a']='replacement_publisher_a';f.user['recorder_a']='replacement_recorder_a'
            f.configure();f.start();a=f.publish('a');b=f.publish('b');ra=f.read('a');rb=f.read('b')
            until(lambda:ra[3]['frames']>=12 and rb[3]['frames']>=12,'new authorized identities restore both actual cameras',20)
            require((f.state/'revocations.json').read_bytes()==snapshot,'controlled recovery cannot roll back denial state')
            f.inspect();observed.extend([ra[3]['frames'],rb[3]['frames']])
            require(all(f.request('/internal/'+role+'/'+principal+'/revocation')[1]['revoked'] is False
                for role,principal in [('publishers',f.user['publisher_a']),('recorders',f.user['recorder_a'])]),'new authorized identities are distinct')
        finally:
            f.stop();f.closed()
        return dict(schema=1,scope='host-native-rtsp-backend-identity-revocation',qualified=True,
            recorder_active_termination=True,publisher_active_termination=True,new_admission_denied=True,
            unrelated_camera_continued=True,restart_and_rotated_credentials_denied=True,
            explicit_new_identity_recovery=True,denial_snapshot_unchanged=True,credential_metadata_checked=True,
            joined_cleanup=True,decoded_frame_observations=observed,
            physical_qualified=False,fips_validated=False,complete_credential_lifecycle_qualified=False,
            full_native_advisory_closure=False,deployment_permitted=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for key in ['gateway','publisher','reader','mediamtx','ffmpeg']:parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root/'tools/native-packages'))
    import candidate
    binaries={key:getattr(args,key).resolve() for key in ['gateway','publisher','reader','mediamtx','ffmpeg']}
    def hashes():
        result={}
        for key,path in binaries.items():
            value=hashlib.sha256()
            with path.open('rb') as source:
                while chunk:=source.read(1024*1024):value.update(chunk)
            result[key]=value.hexdigest()
        return result
    inputs=candidate.source_inputs();executables=hashes()
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    changes=subprocess.check_output(['git','status','--porcelain'],cwd=root)
    report=exercise(**binaries)
    require(inputs==candidate.source_inputs() and executables==hashes()
        and head==subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
        and changes==subprocess.check_output(['git','status','--porcelain'],cwd=root),
        'same source and executable cohort throughout backend qualification')
    report.update(inputs=inputs,executables=executables,repository_head=head,repository_has_changes=bool(changes.strip()))
    require(all(type(v) is int and 12<=v<=2**63-1 for v in report['decoded_frame_observations']),
        'bounded independently decoded observations')
    raw=json.dumps(report,sort_keys=True,separators=(',',':'))+'\n';require(len(raw.encode())<=65536,'bounded public backend qualification')
    if args.report:private(args.report,raw)
    print('PASS: native publisher/recorder active denial, independent camera progress, persistent identity denial and controlled new-identity recovery',flush=True)
