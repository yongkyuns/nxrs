"""Actual protected-volume backup CLI, media restore and interruption controls.

Imported only by the disposable hosted-Linux loop-image fixture. No device path,
raw logs, secret input, media payload or process metadata is printed.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import time
from credential_process import assert_boundary,credential_free


def require(value,category):
    if not value:raise AssertionError(category)


def cycle(*,root,mount,tools,settings,control,api,token,backup,publisher_test,restic,command):
    from encrypted_activity_reference import hosted
    hosted()
    helper=tools/'backup';sdk=tools/'publisher-test';engine=tools/'restic'
    for source,destination in [(backup,helper),(publisher_test,sdk),(restic,engine)]:
        shutil.copyfile(source,destination);destination.chmod(0o755)
    clip=tools/'source.mp4';thumb=tools/'source.jpg'
    command(['/usr/bin/ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=160x96:rate=6',
        '-t','2','-an','-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-g','6','-movflags','+faststart',clip])
    command(['/usr/bin/ffmpeg','-nostdin','-v','error','-i',clip,'-frames:v','1',thumb])
    clip.chmod(0o644);thumb.chmod(0o644)
    environment={'PATH':'/usr/bin:/bin','LANG':'C'}
    sdk_env=dict(environment,NXRS_TEST_VOLUME=str(mount),NXRS_TEST_LUKS_UUID=settings['protection']['luks_uuid'],
                 NXRS_TEST_CLIP=str(clip),NXRS_TEST_THUMB=str(thumb))
    result=subprocess.run([str(sdk),'--ignored','--exact','publish_known_media_for_backup'],user=65534,group=65534,
                          extra_groups=[],env=sdk_env,capture_output=True,timeout=15)
    require(result.returncode==0,'real Store media-publication SDK witness')
    event=json.loads((mount/'maintenance/fixture-event.json').read_text())['event_id']
    repository=root/'encrypted-backup';repository.mkdir(mode=0o700);os.chown(repository,65534,65534)
    config_path=root/'backup.json'
    config=dict(storage=settings,repository=str(repository),repository_id=None,restic=str(engine),
                restic_sha256=hashlib.sha256(engine.read_bytes()).hexdigest(),timeout_seconds=60)
    secret=secrets.token_hex(32);wrong=secrets.token_hex(32)
    active=[]
    def private(path,data):
        path.write_text(json.dumps(data));path.chmod(0o600);os.chown(path,65534,65534)
    def kill_group(process):
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.wait(5)
    def start(operation,key=secret):
        private(config_path,config)
        process=subprocess.Popen([str(helper),str(config_path),*operation],user=65534,group=65534,extra_groups=[],
            env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        active.append(process)
        try:process.stdin.write(key.encode());process.stdin.close()
        except BrokenPipeError:pass
        process.stdin=None
        return process
    def invoke(operation,*,key=secret,error=None):
        process=start(operation,key);deadline=time.monotonic()+120;seen=False
        try:
            while process.poll() is None:
                require(time.monotonic()<deadline,'bounded backup fixture operation')
                status=credential_free(process.pid,[key])
                if status is not None:require(status,'key absent from helper process metadata')
                seen=bool(assert_boundary(process.pid,[key])) or seen
                time.sleep(.02)
            output,stderr=process.communicate(timeout=5)
            # Product stderr is a fixed Error display, but never forward raw
            # native output or a process's unrecognized text to CI diagnostics.
            categories={
                'invalid': b'invalid backup configuration or snapshot',
                'storage': b'backup storage unavailable',
                'busy': b'recording or backup resources are busy; stop the whole group',
                'protection': b'protected non-root maintenance boundary required',
                'key': b'backup key rejected', 'repository': b'backup repository identity mismatch',
                'native': b'backup tool refused the operation', 'output': b'backup tool output limit exceeded',
                'timeout': b'backup operation deadline exceeded', 'cancelled': b'backup operation cancelled',
                'recovery': b'interrupted restore requires explicit recovery',
            }
            observed=next((name for name,value in categories.items()
                           if stderr.strip()==b'nxrs-activity-backup: '+value),'unclassified')
            if process.returncode==0:observed='success'
            require(key.encode() not in output+stderr,'key absent from backup output')
            if error:
                require(process.returncode!=0 and error.encode() in stderr,
                        'expected backup failure category; observed '+observed)
                return None
            require(process.returncode==0,'actual encrypted backup CLI status; observed '+observed)
            require(seen,'actual backup-native descendant metadata observed')
            require(key.encode() not in output+stderr,'key absent from backup output')
            return json.loads(output)
        finally:
            kill_group(process)
    gateway=None
    live=root/'backup-control.json';private(live,control)
    authority_hash=hashlib.sha256(live.read_bytes()).hexdigest()
    def request(path,credential=token):
        connection=http.client.HTTPConnection('127.0.0.1',api,timeout=5)
        try:
            connection.request('GET',path,headers={'Authorization':'Bearer '+credential})
            response=connection.getresponse();data=response.read(34*1024*1024);return response.status,data
        finally:connection.close()
    def start_gateway():
        nonlocal gateway
        gateway=subprocess.Popen([str(tools/'gateway'),str(live),'--require-encrypted-storage'],
            user=65534,group=65534,extra_groups=[],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+5
        while True:
            try:
                require(request('/v1/cameras/cam_one/activity')[0]==200,'restored controller positive readiness');return
            except ConnectionRefusedError:
                require(gateway.poll() is None and time.monotonic()<deadline,'protected controller remains running')
                time.sleep(.02)
    def stop_gateway():
        nonlocal gateway
        if gateway and gateway.poll() is None:
            gateway.terminate()
            try:gateway.wait(8)
            except subprocess.TimeoutExpired:
                gateway.kill();gateway.wait(3);raise AssertionError('owned controller retirement') from None
        gateway=None
    def blocked_gateway():
        result=subprocess.run([str(tools/'gateway'),str(live),'--require-encrypted-storage'],
            user=65534,group=65534,extra_groups=[],env=environment,capture_output=True,timeout=5)
        require(result.returncode!=0 and b'activity storage unavailable' in result.stderr,'restore intent blocks admission')
    data_root=mount/'activity';maintenance=mount/'maintenance'
    def empty_copy():
        # Only a synthetic restored copy, with all owned applications stopped.
        for entry in data_root.iterdir():
            if entry.is_dir():shutil.rmtree(entry)
            else:entry.unlink()
    try:
        start_gateway()
        invoke(['init'],error='resources are busy')
        require(not list(repository.iterdir()),'active stores must prevent repository initialization')
        stop_gateway()
        receipt=invoke(['init']);config['repository_id']=receipt['repository_id']
        snapshot=invoke(['backup'])['snapshot_id']
        require(len(snapshot)==64,'full authenticated snapshot identity')
        invoke(['check']);invoke(['check'],key=wrong,error='backup key rejected')
        original_id=config['repository_id'];config['repository_id']='f'*64
        invoke(['check'],error='repository identity mismatch');config['repository_id']=original_id
        invoke(['restore',snapshot],error='explicit recovery')
        for path in repository.rglob('*'):
            if path.is_file():
                ciphertext=path.read_bytes()
                require(secret.encode() not in ciphertext,'backup key absent from repository files')
                require(clip.read_bytes()[:64] not in ciphertext,'media canary absent from ciphertext')
        original=maintenance/'original-fixture';original.mkdir(mode=0o700);os.chown(original,65534,65534)
        for entry in list(data_root.iterdir()):entry.rename(original/entry.name)
        invoke(['restore',snapshot],key=wrong,error='backup key rejected')
        require(not list(data_root.iterdir()),'wrong key cannot create restored files')
        # A real publication write failure occurs AFTER durable ready intent.
        data_root.chmod(0o500)
        invoke(['restore',snapshot],error='explicit recovery')
        pending=json.loads((maintenance/'restore.json').read_text())
        require(pending['ready'] is True,'executed ready-intent publication failure')
        blocked_gateway();data_root.chmod(0o700)
        invoke(['resume',snapshot])
        require(not (maintenance/'restore.json').exists(),'successful explicit resume removes intent')
        start_gateway()
        status,body=request('/v1/cameras/cam_one/activity')
        page=json.loads(body);kept=next(item for item in page['events'] if item['event_id']==event)
        require(status==200 and kept['kept'],'restored kept state and history authorization')
        status,payload=request('/v1/cameras/cam_one/activity/'+event+'/clip')
        require(status==200 and payload==clip.read_bytes(),'restored media reaches actual authenticated API')
        require(request('/v1/cameras/cam_one/activity/'+event+'/clip',wrong)[0]==401,'restored media still requires current authorization')
        decoded=subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-i','pipe:0','-f','null','-'],
                               input=payload,capture_output=True,env=environment,timeout=15)
        require(decoded.returncode==0,'restored real H264 media fully decodes')
        stop_gateway()
        require(hashlib.sha256(live.read_bytes()).hexdigest()==authority_hash,'authority configuration never restored or replaced')
        print('PASS: actual CLI encrypted backup/key/repository/metadata controls and authenticated decoded restore',flush=True)
        print('PASS: active-store refusal and real ready-phase publication failure block startup until explicit resume',flush=True)
        # Kill the WHOLE owned process group during an actual native restore.
        empty_copy();process=start(['restore',snapshot]);deadline=time.monotonic()+15
        while True:
            require(process.poll() is None and time.monotonic()<deadline,'observed native restore interruption window')
            marker=maintenance/'restore.json'
            if marker.exists():
                try:
                    intent=json.loads(marker.read_text());stage=maintenance/('restore-'+intent['stage'])
                    if not intent['ready'] and stage.is_dir():break
                except (OSError,json.JSONDecodeError):pass
            time.sleep(.001)
        kill_group(process);process.communicate(timeout=5)
        blocked_gateway()
        invoke(['unlock-stale']) # Native safe unlock only, never --remove-all.
        invoke(['resume',snapshot])
        require((data_root/'events'/event/'clip.mp4').read_bytes()==clip.read_bytes(),'killed preparing-phase restore resumes exact media')
        print('PASS: killed native restore group leaves startup blocked; safe stale-lock recovery and exact resume',flush=True)
        pack=next(path for path in (repository/'data').rglob('*') if path.is_file())
        damaged=bytearray(pack.read_bytes());damaged[-1]^=1;pack.chmod(0o600);pack.write_bytes(damaged)
        invoke(['check'],error='backup tool refused')
        require(hashlib.sha256(live.read_bytes()).hexdigest()==authority_hash,'corruption cannot change authorization state')
        print('PASS: corrupted encrypted repository rejected; current authorization remains independent',flush=True)
    finally:
        stop_gateway()
        for process in active:kill_group(process)
        data_root.chmod(0o700)
