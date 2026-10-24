#!/usr/bin/env python3
"""Disposable hosted-Linux loop-image qualification; never accepts a disk/device path.

Only an owned fresh sparse file can be formatted. No personal-host execution,
raw process logs, generated credentials or synthetic footage are uploaded.
"""
import argparse
import hashlib
import http.client
import socket
import time
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid


def require(value, category):
    if not value: raise AssertionError(category)


def command(args, *, success=True, error=None, public_sdk_failure=False, public_activity_failure=False, **kwargs):
    try:
        result=subprocess.run([str(v) for v in args],env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'},
                              capture_output=True,timeout=kwargs.pop('timeout',30),**kwargs)
    except subprocess.TimeoutExpired:
        raise AssertionError('bounded owned-fixture operation timed out') from None
    if public_sdk_failure and result.returncode!=0:
        from sdk_media_evidence import public_failure
        print('SDK_RECORDING_FAILURE '+json.dumps(public_failure(result.stdout+result.stderr),sort_keys=True),flush=True)
    if public_activity_failure and result.returncode!=0:
        from activity_diagnostics import public_failure
        print('ACTIVITY_RECORDING_FAILURE '+json.dumps(public_failure(result.stdout+result.stderr),sort_keys=True),flush=True)
    require((result.returncode==0)==success,'owned-fixture operation status: '+Path(args[0]).name)
    if error is not None: require(error in result.stderr,'expected fail-closed storage category')
    return result.stdout


def hosted():
    require(sys.platform=='linux' and os.geteuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true'
            and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted','disposable hosted Linux root required')
    release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    require(release.get('ID','').strip('"')=='ubuntu' and release.get('VERSION_ID','').strip('"')=='24.04',
            'Ubuntu 24.04 runner required')


def source_inputs(sdk=None):
    root=Path(__file__).resolve().parents[2]
    names=['tests/device-identity/frame.jpg','.github/workflows/monitoring-ingest.yml','.github/workflows/encrypted-activity.yml','gateway/Cargo.toml','gateway/Cargo.lock',
           'gateway/tests/encrypted_activity_reference.py','gateway/tests/activity_reference.py',
           'gateway/tests/test_activity_reference.py','.github/workflows/monitoring-gateway.yml',
           'gateway/tests/activity_diagnostics.py','gateway/tests/test_activity_diagnostics.py',
           'gateway/tests/backup_reference.py','gateway/tests/ingest_reference.py','gateway/tests/recording_ingest.py',
           'gateway/tests/rust_camera.py','gateway/tests/camera_source.rs',
           'gateway/tests/tls_camera.py','gateway/tests/revocation_reference.py','gateway/tests/credential_process.py',
           'gateway/tests/sdk_media_evidence.py','gateway/tests/test_sdk_media_evidence.py',
           'gateway/tests/qualify_media_sandbox.py','gateway/tests/media_sandbox/probe.c',
           'tools/enrollment/issuer.py','tools/enrollment/prepare_ingest.py','gateway/deploy/dependencies.json',
           'gateway/deploy/backup-dependencies.json','gateway/deploy/restic/go.mod','gateway/deploy/restic/go.sum',
           'gateway/tools/backup_engine.py','gateway/tools/verify_backup_evidence.py']
    for crate in ['activity','ingest','control','private-file','media-sandbox','media-process']:
        names.append('gateway/'+crate+'/Cargo.toml')
        names.extend(str(p.relative_to(root)) for p in (root/'gateway'/crate/'src').rglob('*.rs'))
    if sdk is not None:
        from sdk_media_evidence import source_names
        names.extend(source_names(root,sdk))
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(set(names))}


def run(worker,gateway,mediamtx,*,backup=None,publisher_test=None,restic=None,
        ingest=None,publisher=None,camera_source=None,tls_python=None,sdk_qualification=None,reader=None,media_sandbox=None,isolation_qualification=None):
    hosted()
    reader=reader or worker.parent/"nxrs-rtsp-reader"
    repository=Path(__file__).resolve().parents[2]
    sdk_raw,sdk=None,None
    if sdk_qualification:
        require(all([ingest,publisher,camera_source,tls_python,backup]), 'complete SDK composition resources required')
        from sdk_media_evidence import read_sdk, read_media
        sdk_raw,sdk=read_sdk(repository,sdk_qualification,camera_source)
        media_engine=read_media(mediamtx)
        checkout=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repository,text=True,timeout=30).strip()
        require(not subprocess.check_output(['git','status','--porcelain'],cwd=repository,text=True,timeout=30).strip(),
                'SDK composition requires a clean source checkout')
    inputs=source_inputs(sdk)
    binaries=dict(worker=worker,gateway=gateway,mediamtx=mediamtx,backup=backup,publisher_test=publisher_test,
                  restic=restic,ingest=ingest,publisher=publisher,camera_source=camera_source,reader=reader,media_sandbox=media_sandbox,ffmpeg=Path('/usr/bin/ffmpeg'))
    hashes={name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in binaries.items() if path}
    isolation_sha=None
    if media_sandbox:
        require(isolation_qualification is not None, 'executed isolation receipt required')
        raw=Path(isolation_qualification).read_bytes()
        require(len(raw)<=65536,'bounded isolation receipt required')
        isolation=json.loads(raw)
        require(isolation['qualified'] is True and isolation['scope']=='disposable-linux-media-process-isolation'
                and isolation['physical_qualified'] is False and isolation['deployment_permitted'] is False
                and isolation['required_landlock_abi']==3 and isolation['encoded_frames']==16
                and isolation['executables']['launcher']==hashes['media_sandbox']
                and isolation['executables']['ffmpeg']==hashes['ffmpeg'], 'same executed isolation helper required')
        expected={'hardlink','inherited','outside_write','parent_memory','secret','signal','symlink','tcp','udp','unix','work_write','fork','clone','clone_new'}
        require(isolation['controls']['baseline']==dict.fromkeys(expected,1)
                and isolation['controls']['isolated']=={k:int(k=='work_write') for k in expected},
                'executed confinement positive and negative controls required')
        require(all(inputs.get(name)==digest for name,digest in isolation['inputs'].items()),
                'same committed isolation qualification source closure required')
        require(isolation['checkout']==dict(clean=True,commit=subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=repository,text=True,timeout=30).strip()), 'same isolation qualification checkout required')
        isolation_sha=hashlib.sha256(raw).hexdigest()

    temporary=tempfile.mkdtemp(prefix='nxrs-encrypted-fixture-');root=Path(temporary).resolve();root.chmod(0o711)
    image=root/'owned-volume.img';password=root/'unlock';wrong=root/'wrong';replacement=root/'replacement';mount=root/'volume'
    loop=None;mapping='nxrstest'+secrets.token_hex(8);mapped=False;mounted=False;nested=False;daemon=None
    require(not Path('/dev/mapper',mapping).exists(),'fresh owned mapping name required')
    mount.mkdir(mode=0o000)
    for path in [password,wrong,replacement]:
        with open(path,'xb',opener=lambda p,f:os.open(p,f,0o600)) as out:out.write(secrets.token_bytes(32))
    with open(image,'xb',opener=lambda p,f:os.open(p,f,0o600)) as out:out.truncate(256*1024*1024)
    tools=root/'tools';tools.mkdir(mode=0o755)
    # Stage public executables/scripts so the unprivileged fixture does not need
    # access to the runner user's private checkout/home directory.
    for source,name in [(worker,'worker'),(gateway,'gateway'),(mediamtx,'mediamtx'),(reader,'nxrs-rtsp-reader')]+([(media_sandbox,'nxrs-media-sandbox')] if media_sandbox else []):
        shutil.copyfile(source,tools/name);(tools/name).chmod(0o755)
    if sdk is not None:
        for suffix in ['.cdx.json','.advisories.json','.provenance.json']:
            shutil.copyfile(str(mediamtx)+suffix,str(tools/'mediamtx')+suffix)
            Path(str(tools/'mediamtx')+suffix).chmod(0o644)
    for name in ['activity_reference.py','credential_process.py','activity_diagnostics.py']:
        shutil.copyfile(Path(__file__).with_name(name),tools/name);(tools/name).chmod(0o644)
    identifier=str(uuid.uuid4());protected=dict(layout_version=2,mount=str(mount),luks_uuid=identifier)
    settings=dict(directory=str(mount/'activity'),camera_ids=['cam_one'],retention_seconds=3600,
                  max_bytes=67108864,max_events=100,protection=protected)
    config=dict(storage=settings,camera_id='cam_one',rtsp='127.0.0.1:8554',stream_path='one',username='recorder',
                token_file=str(root/'unused.token'),ffmpeg='/usr/bin/ffmpeg',reader=str(tools/'nxrs-rtsp-reader'),motion_percent=2,
                pre_roll_seconds=2,post_roll_seconds=4,max_clip_seconds=10)
    def port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
    api,auth=port(),port();token=secrets.token_hex(32)
    control=dict(listen=f'127.0.0.1:{api}',auth_listen=f'127.0.0.1:{auth}',grant_ttl_seconds=60,max_grants=64,
        media_origins=['https://media.test'],cameras=[dict(camera_id='cam_one',gateway_id='gw',site_id='site',
        name='SYNTHETIC',source='synthetic',stream_path='one',whep_endpoint='https://media.test/one/whep')],
        publishers=[],viewers=[dict(viewer_id='owner',site_id='site',camera_ids=['cam_one'],token_sha256=hashlib.sha256(token.encode()).hexdigest())],activity=settings)
    def private(path,data):
        path.write_text(json.dumps(data));path.chmod(0o600)
    def check(ok):
        private(root/'worker.json',config)
        command([tools/'worker',root/'worker.json','--require-encrypted-storage','--check-storage'],success=ok,
                error=None if ok else b'encrypted activity storage unavailable')
    def reject_control():
        private(root/'control.json',control)
        command([tools/'gateway',root/'control.json','--require-encrypted-storage'],success=False,timeout=5,error=b'activity storage unavailable')
    def unlock(key):
        command(['/usr/sbin/cryptsetup','open','--key-file',key,loop,mapping])
    def attach():
        command(['/usr/bin/mount','-t','ext4','-o','rw,nosuid,nodev,noexec','/dev/mapper/'+mapping,mount])
    try:
        check(False);reject_control();require(not (mount/'activity').exists(),'no pre-unlock plaintext directory')
        loop=command(['/usr/sbin/losetup','--find','--show',image]).decode().strip()
        require(bool(re.fullmatch('/dev/loop[0-9]+',loop)),'owned loop identity')
        command(['/usr/sbin/cryptsetup','luksFormat','--type','luks2','--uuid',identifier,
            '--cipher','aes-xts-plain64','--key-size','512','--pbkdf','argon2id','--pbkdf-memory','32768',
            '--pbkdf-parallel','1','--iter-time','10','--batch-mode','--key-file',password,loop])
        command(['/usr/sbin/cryptsetup','open','--key-file',wrong,loop,mapping],success=False)
        unlock(password);mapped=True
        command(['/usr/sbin/mkfs.ext4','-q','/dev/mapper/'+mapping])
        attach();mounted=True;mount.chmod(0o711)
        protected['luks_uuid']=str(uuid.uuid4());check(False);reject_control()
        require(not (mount/'activity').exists(),'wrong UUID must not create storage')
        protected['luks_uuid']=identifier
        # A nested plaintext filesystem cannot escape whole-volume coverage.
        escape=mount/'plain';escape.mkdir()
        command(['/usr/bin/mount','-t','tmpfs','-o','nosuid,nodev,noexec','tmpfs',escape]);nested=True
        check(False);reject_control()
        command(['/usr/bin/umount',escape]);nested=False;escape.rmdir()
        # Require hardened mount flags before first creation.
        command(['/usr/bin/mount','-o','remount,exec',mount]);check(False)
        command(['/usr/bin/mount','-o','remount,noexec',mount])
        maintenance=mount/'maintenance';maintenance.mkdir(mode=0o700)
        check(True)
        print('PASS: actual kernel LUKS2 identity and mount/UUID/nested-filesystem failure controls',flush=True)
        activity=mount/'activity'
        # Initial store lock is root-owned; all runtime state belongs to the same
        # non-root fixture identity before starting the actual app processes.
        for directory,folders,files in os.walk(activity):
            os.chown(directory,65534,65534)
            for name in files:os.chown(Path(directory)/name,65534,65534)
        for directory,folders,files in os.walk(maintenance):
            os.chown(directory,65534,65534)
            for name in files:os.chown(Path(directory)/name,65534,65534)
        staging=mount/'qualification';staging.mkdir(mode=0o700);os.chown(staging,65534,65534)
        command(['/usr/bin/setpriv','--reuid','65534','--regid','65534','--clear-groups','/usr/bin/python3',
            tools/'activity_reference.py','--gateway',tools/'gateway','--worker',tools/'worker',
            '--mediamtx',tools/'mediamtx','--ffmpeg','/usr/bin/ffmpeg',
            *(['--media-sandbox',tools/'nxrs-media-sandbox'] if media_sandbox else []),
            '--storage-volume',mount,'--luks-uuid',identifier],timeout=180,public_activity_failure=True)
        print('PASS: real unprivileged encrypted-volume recording, decoding, auth, retention and recovery',flush=True)
        secure_receipt=None
        if ingest:
            require(all([publisher,camera_source,tls_python,backup]), 'complete composed security resources required')
            for source,name in [(ingest,'ingest'),(publisher,'nxrs-rtsp-publisher'),(camera_source,'camera-source')]:
                shutil.copyfile(source,tools/name);(tools/name).chmod(0o755)
            code=tools/'composed';tests=code/'gateway/tests';enrollment=code/'tools/enrollment'
            tests.mkdir(parents=True);enrollment.mkdir(parents=True)
            # Copy only the public source closure, never checkout credentials,
            # SDK private fixtures, native build logs or enrollment keys.
            for name,digest in inputs.items():
                destination=code/name;destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(repository/name,destination);destination.chmod(0o644)
                require(hashlib.sha256(destination.read_bytes()).hexdigest()==digest, 'staged source identity')
            extra=[]
            if sdk is not None:
                staged_sdk=tools/'sdk-qualification.json';staged_sdk.write_bytes(sdk_raw);staged_sdk.chmod(0o644)
                extra=['--sdk-camera','--sdk-qualification',staged_sdk,'--sdk-report',maintenance/'sdk-media.json']
            receipt_path=maintenance/'secure-ingest.json'
            command(['/usr/bin/setpriv','--reuid','65534','--regid','65534','--clear-groups',tls_python,
                tests/'ingest_reference.py','--gateway',tools/'gateway','--ingest',tools/'ingest',
                '--mediamtx',tools/'mediamtx','--camera-source',tools/'camera-source','--camera-tls',
                '--storage-volume',mount,'--luks-uuid',identifier,'--recording-worker',tools/'worker',
                '--recording-receipt',receipt_path,
                *(['--media-sandbox',tools/'nxrs-media-sandbox'] if media_sandbox else []),*extra],timeout=180,public_sdk_failure=sdk is not None)
            secure_receipt=json.loads(receipt_path.read_bytes())
            require(secure_receipt['schema']==1 and secure_receipt['camera_id']=='cam_one'
                    and secure_receipt['encrypted_volume_required'] is True
                    and secure_receipt['credential_reader_used'] is True
                    and secure_receipt['decoder_isolation_executed'] is bool(media_sandbox)
                    and secure_receipt['camera_tls_front_end_test_only'] is (sdk is None)
                    and secure_receipt['physical_qualified'] is False, 'complete composed recording receipt')
            if sdk is not None:
                from sdk_media_evidence import validate_media
                media=secure_receipt.get('sdk_media',{})
                validate_media(media)
                require(secure_receipt.get('sdk_endpoint_used') is True
                        and media.get('sdk_qualification_sha256')==hashlib.sha256(sdk_raw).hexdigest()
                        and media.get('media_engine')==media_engine
                        and media.get('staged_sources_verified') is True
                        and media.get('executables')=={key:hashes[original] for key,original in
                            dict(gateway='gateway',ingest='ingest',publisher='publisher',camera='camera_source',
                                 mediamtx='mediamtx',ffmpeg='ffmpeg',**({'media_sandbox':'media_sandbox'} if media_sandbox else {})).items()}
                        and media==json.loads((maintenance/'sdk-media.json').read_bytes()),
                        'SDK recording and media evidence must describe the same execution')
                require(all(inputs.get(name)==digest for name,digest in media['inputs'].items()),
                        'SDK media sources match the original checked-out composition')
            print('PASS: actual Rust camera, mutual-TLS host ingest, native publisher, two decoded viewers and encrypted recording',flush=True)
        # Verify the already-open gateway rechecks the kernel boundary, rather
        # than relying only on an initial configuration/startup check.
        live=staging/'control.json';private(live,control);os.chown(live,65534,65534)
        daemon=subprocess.Popen([str(tools/'gateway'),str(live),'--require-encrypted-storage'],
            user=65534,group=65534,extra_groups=[],env={'PATH':'/usr/bin:/bin','LANG':'C'},
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def status():
            connection=http.client.HTTPConnection('127.0.0.1',api,timeout=3)
            try:
                connection.request('GET','/v1/cameras/cam_one/activity',headers={'Authorization':'Bearer '+token})
                response=connection.getresponse();response.read(1024*1024);return response.status
            finally:connection.close()
        deadline=time.monotonic()+5
        while True:
            try:
                require(status()==200,'live protected-store positive control');break
            except ConnectionRefusedError:
                require(daemon.poll() is None and time.monotonic()<deadline,'live protected controller readiness')
                time.sleep(.05)
        escape.mkdir()
        command(['/usr/bin/mount','-t','tmpfs','-o','nosuid,nodev,noexec','tmpfs',escape]);nested=True
        require(status()>=500,'live store must reject changed kernel protection')
        command(['/usr/bin/umount',escape]);nested=False;escape.rmdir()
        require(status()==200,'same protected kernel identity after controlled restoration')
        daemon.terminate();daemon.wait(8);daemon=None
        print('PASS: live gateway rechecks storage protection and denies a new plaintext nested mount',flush=True)
        if backup:
            from backup_reference import cycle
            cycle(root=root,mount=mount,tools=tools,settings=settings,control=control,api=api,token=token,
                  backup=backup,publisher_test=publisher_test,restic=restic,command=command,secure_receipt=secure_receipt)
        command(['/usr/sbin/cryptsetup','luksAddKey','--key-file',password,'--pbkdf','argon2id',
            '--pbkdf-memory','32768','--pbkdf-parallel','1','--iter-time','10',loop,replacement])
        command(['/usr/sbin/cryptsetup','open','--test-passphrase','--key-file',replacement,loop])
        command(['/usr/sbin/cryptsetup','luksRemoveKey',loop,password])
        command(['/usr/sbin/cryptsetup','open','--test-passphrase','--key-file',password,loop],success=False)
        password=replacement
        print('PASS: tested replacement LUKS key slot, removed old passphrase, old key rejected',flush=True)
        canary=secrets.token_hex(64).encode();(activity/'offline-canary').write_bytes(canary)
        command(['/usr/bin/umount',mount]);mounted=False
        command(['/usr/sbin/cryptsetup','close',mapping]);mapped=False
        check(False);reject_control();require(not activity.exists(),'locked volume must not create plaintext fallback')
        # Unique plaintext test canary is absent from raw owned ciphertext.
        with open(image,'rb') as source:
            tail=b''
            while data:=source.read(1024*1024):
                require(canary not in tail+data,'plaintext canary in owned ciphertext')
                tail=data[-len(canary):]
        command(['/usr/sbin/cryptsetup','open','--key-file',wrong,loop,mapping],success=False)
        unlock(password);mapped=True;attach();mounted=True
        require((activity/'offline-canary').read_bytes()==canary,'correct-key persistent volume recovery')
        check(True)
        print('PASS: missing/wrong UUID/key, nested plaintext, mount flags, no fallback and lock/reopen persistence',flush=True)
    finally:
        # Only exact resources created above; no forced/lazy unmount or global cleanup.
        # Retain the image on an unproven cleanup rather than deleting a backing file.
        if daemon and daemon.poll() is None:
            daemon.terminate()
            try:daemon.wait(8)
            except subprocess.TimeoutExpired:
                daemon.kill();daemon.wait(3)
                raise AssertionError('owned controller cleanup deadline') from None
        if os.path.ismount(mount/'plain'):command(['/usr/bin/umount',mount/'plain'])
        if os.path.ismount(mount):command(['/usr/bin/umount',mount])
        if Path('/dev/mapper',mapping).exists():command(['/usr/sbin/cryptsetup','close',mapping])
        # A command timeout may lose the returned handle after allocation. Inspect
        # authoritative kernel associations for ONLY this fresh owned image.
        associated=command(['/usr/sbin/losetup','--associated',image,'--output','NAME','--noheadings']).decode().split()
        for device in associated:
            require(bool(re.fullmatch('/dev/loop[0-9]+',device)),'owned cleanup loop identity')
            command(['/usr/sbin/losetup','--detach',device])
        mount.chmod(0o700);shutil.rmtree(root)
    require(inputs==source_inputs(sdk), 'composition source inputs unchanged')
    if sdk is not None:
        require(Path(sdk_qualification).read_bytes()==sdk_raw, 'SDK qualification unchanged')
        require(subprocess.check_output(['git','rev-parse','HEAD'],cwd=repository,text=True,timeout=30).strip()==checkout
                and not subprocess.check_output(['git','status','--porcelain'],cwd=repository,text=True,timeout=30).strip(),
                'SDK composition checkout unchanged')
    require(hashes=={name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in binaries.items() if path},
            'composition binaries unchanged')
    return dict(schema=1,scope='host-secure-ingest-encrypted-recording-and-authenticated-backup-composition',
                qualified=True,inputs=inputs,executables=hashes,recording=secure_receipt,
                sdk_camera_host_qualified=sdk is not None,
                sdk_qualification_sha256=hashlib.sha256(sdk_raw).hexdigest() if sdk is not None else None,
                authenticated_camera_ingest_qualified=bool(ingest),encrypted_recording_qualified=True,
                decoder_isolation_executed=media_sandbox is not None,media_isolation_qualification_sha256=isolation_sha,
                authenticated_backup_restore_qualified=bool(backup),interrupted_restore_qualified=bool(backup),
                physical_qualified=False,mcu_tls_qualified=False,full_native_advisory_closure=False,deployment_permitted=False)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['worker','gateway','mediamtx']:p.add_argument('--'+name,type=Path,required=True)
    for name in ['backup','publisher-test','restic','ingest','publisher','camera-source','tls-python','sdk-qualification','reader','media-sandbox','isolation-qualification']:
        p.add_argument('--'+name,type=Path)
    p.add_argument('--report',type=Path)
    a=p.parse_args()
    if any([a.backup,a.publisher_test,a.restic]) and not all([a.backup,a.publisher_test,a.restic]):p.error('complete backup fixture resources required')
    extra=[a.ingest,a.publisher,a.camera_source,a.tls_python]
    if any(extra) and (not all(extra) or not a.backup):p.error('complete composed security fixture resources required')
    if a.sdk_qualification and not all(extra):p.error('complete SDK composition resources required')
    if a.report and not all(extra):p.error('composed evidence requires every integration gate')
    report=run(a.worker.resolve(),a.gateway.resolve(),a.mediamtx.resolve(),backup=a.backup,publisher_test=a.publisher_test,
        restic=a.restic,ingest=a.ingest,publisher=a.publisher,camera_source=a.camera_source,tls_python=a.tls_python,sdk_qualification=a.sdk_qualification,reader=a.reader,media_sandbox=a.media_sandbox,isolation_qualification=a.isolation_qualification)
    if a.report:
        require(all(extra), 'composed evidence requires every integration gate')
        parent=a.report.parent.stat()
        with a.report.open('x') as output:
            json.dump(report,output,sort_keys=True,indent=2)
            os.fchmod(output.fileno(),0o644)
            os.fchown(output.fileno(),parent.st_uid,parent.st_gid)
