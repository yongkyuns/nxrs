#!/usr/bin/env python3
"""Actual encrypted recording service group on an owned disposable hosted runner.

Network prerequisites and the synthetic source are named fixtures. This does not
qualify cold boot, device TLS, production unlock custody, VPN or physical media.
"""
import argparse,hashlib,http.client,json,os,pwd,re,secrets,shutil,socket,ssl,stat,subprocess,sys,time,urllib.error,urllib.request,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/deploy'))
sys.path.insert(0,str(ROOT/'gateway/tools'))
import strict_vpn,vpn,encrypted_activity
import service_lifetime as lifetime
from credential_process import credential_free
from activity_reference import request_with_busy_retry

FLAGS=('cold_boot_qualified','power_loss_qualified','protected_rollback_qualified','physical_qualified',
       'device_tls_qualified','vpn_qualified','production_unlock_custody_qualified','backup_restore_qualified',
       'whole_os_qualified','release_trust_qualified','fips_validated','deployment_permitted')
CONTROLS=('actual_generated_units','actual_encrypted_mount','camera_scoped_https_recording','clip_fully_decoded',
          'credential_metadata_checked','viewer_denial_persisted','mount_loss_stopped_group',
          'all_service_cgroups_joined','no_plaintext_fallback','recovery_same_clip_bytes','recovery_same_denial',
          'healthy_viewer_recovered','owned_cleanup_joined')
UNIT_KEYS={'owned-control.service','owned-media.service','owned-proxy.service','owned-monitor-stop.service','owned-monitor.target',
    'owned-control.service.d/encrypted-storage.conf','owned-activity.service','owned-monitor.target.d/encrypted-storage.conf',
    'owned-volume.mount','owned-network.service','owned-source.service'}
PROGRAMS={'control','media','proxy','activity','sandbox','publisher','reader','ffmpeg','fixture_encoder'}
SCOPE='hosted-generated-encrypted-recording-service-group'
phase='context'

def require(value,category):
    if not value:raise ValueError(category)

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def hosted():
    require(sys.platform=='linux' and os.geteuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true'
            and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted','hosted-linux-root-required')
    require('ID=ubuntu' in Path('/etc/os-release').read_text() and 'VERSION_ID="24.04"' in Path('/etc/os-release').read_text(),
            'hosted-noble-required')
    require(Path('/proc/1/comm').read_text().strip()=='systemd' and Path('/sys/fs/cgroup/cgroup.controllers').is_file(),
            'real-systemd-and-cgroups-required')

def command(args,stage,accepted=(0,),seconds=30):
    global phase
    phase=stage
    result=subprocess.run([str(v) for v in args],capture_output=True,timeout=seconds)
    require(result.returncode in accepted and len(result.stdout)+len(result.stderr)<=1024*1024,'owned-command-'+stage)
    return result.stdout

def private(path,value):
    raw=value if isinstance(value,bytes) else (value if isinstance(value,str) else json.dumps(value)).encode()
    with open(path,'xb',opener=lambda name,flags:os.open(name,flags,0o600)) as output:
        output.write(raw);output.flush();os.fsync(output.fileno())

def source_inputs():
    sys.path.insert(0,str(ROOT/'tools/native-packages'))
    import candidate
    from encrypted_activity_reference import source_inputs as composition_inputs
    names=set(candidate.source_inputs())|set(composition_inputs())|{
        'gateway/tests/encrypted_service_reference.py','gateway/tests/systemd_recording_source.py',
        'gateway/tests/test_encrypted_services.py','gateway/deploy/encrypted_activity.py',
        'gateway/deploy/activity.py','gateway/tests/service_lifetime.py','gateway/tests/credential_process.py',
        'gateway/deploy/encrypted-activity.md','.github/workflows/encrypted-activity.yml'}
    return {name:digest(ROOT/name) for name in sorted(names)}

def units(prefix,root,mapping,mount_unit):
    """Rename fixture resources; retain production sandbox and dependency edges."""
    require(re.fullmatch('nxrs-es-[a-f0-9]{8}',prefix) and root==Path('/run')/prefix and
            re.fullmatch('es[a-f0-9]{16}',mapping) and re.fullmatch(r'[A-Za-z0-9_.\\-]+\.mount',mount_unit),'owned-unit-resources')
    aliases={name:prefix+'-'+name for name in ('control','media','proxy','monitor','activity')}
    network=prefix+'-network.service'
    result={}
    for name,text in {**strict_vpn.services(),**encrypted_activity.units()}.items():
        if name=='nxrs-firewall.service':continue
        text=text.replace('wg-quick@wg-nxrs.service',network).replace('nxrs-firewall.service',network)
        text=text.replace('systemd-cryptsetup@nxrsrecordings.service',network)
        name=name.replace('srv-nxrs.mount',mount_unit)
        text=text.replace('srv-nxrs.mount',mount_unit).replace('dev-mapper-nxrsrecordings.device','dev-mapper-'+mapping+'.device')
        text=text.replace('/dev/mapper/nxrsrecordings','/dev/mapper/'+mapping)
        text=text.replace('/srv/nxrs',str(root/'volume'))
        text=text.replace('/etc/nxrs-monitor',str(root/'conf'))
        for old,new in [('nxrs-gateway-control','control'),('nxrs-mediamtx','media'),('nxrs-gateway-proxy','proxy'),
                        ('nxrs-camera-activity','activity'),('nxrs-media-sandbox','sandbox')]:
            text=text.replace('/usr/local/libexec/'+old,str(root/'bin'/new))
        text=text.replace('/usr/local/lib/nxrs-proxy',str(root/'proxy-artifact'))
        # Map complete filesystem paths before service-name substitutions, which
        # also occur inside names such as nxrs-mediamtx and /etc/nxrs-monitor.
        for old,new in aliases.items():name=name.replace('nxrs-'+old,new);text=text.replace('nxrs-'+old,new)
        if name.endswith('.service'):
            text+='\n[Service]\nStandardOutput=append:'+str(root/'conf'/(Path(name).name+'.log'))+'\nStandardError=append:'+str(root/'conf'/(Path(name).name+'.log'))+'\n'
        result[name]=text
    result[network]='[Service]\nType=oneshot\nExecStart=/usr/bin/true\nRemainAfterExit=yes\n'
    result[prefix+'-source.service']=(
        '[Unit]\nPartOf='+aliases['monitor']+'.target\nAfter='+aliases['media']+'.service\n'
        '[Service]\nType=simple\nUser='+aliases['control']+'\nGroup='+aliases['control']+'\n'
        'LoadCredential=source:'+str(root/'conf/source.json')+'\n'
        'ExecStart=/usr/bin/python3 '+str(root/'bin/source.py')+' %d/source\n'
        'KillMode=control-group\nTimeoutStopSec=35\nRestart=no\nNoNewPrivileges=yes\n'
        'ProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\nPrivateDevices=yes\n'
        'RestrictAddressFamilies=AF_INET AF_UNIX\nCapabilityBoundingSet=\nUMask=0077\nLimitCORE=0\n'
        'StandardOutput=null\nStandardError=null\n')
    return result

def unit_digests(rendered,prefix,mapping,mount_unit):
    canonical=lambda text:text.replace(mount_unit,'owned-volume.mount').replace(prefix,'owned').replace(mapping,'ownedmapping')
    raw={canonical(name):hashlib.sha256(text.encode()).hexdigest() for name,text in rendered.items()}
    normalized={canonical(name):hashlib.sha256(canonical(text).encode()).hexdigest() for name,text in rendered.items()}
    require(set(raw)==set(normalized)==UNIT_KEYS,'complete-generated-unit-inventory')
    return raw,normalized

def expected_unit_policy():
    prefix='nxrs-es-00000000';mapping='es0000000000000000';mount_unit='owned.mount'
    return unit_digests(units(prefix,Path('/run')/prefix,mapping,mount_unit),prefix,mapping,mount_unit)[1]

def validate(value):
    require(set(value)=={'schema','scope','qualified','checkout','inputs','executables','unit_sha256',
        'normalized_unit_sha256','proofs','controls','clip_sha256','clip_bytes','decoded_frames',*FLAGS} and value['schema']==1
        and type(value['schema']) is int and value['scope']==SCOPE and value['qualified'] is True,'service-receipt-schema')
    require(value['checkout']['clean'] is True and set(value['checkout'])=={'commit','clean'}
            and re.fullmatch('[a-f0-9]{40}',value['checkout']['commit']),'clean-service-source')
    require(value['inputs']==source_inputs() and all(re.fullmatch('[a-f0-9]{64}',v) for v in value['inputs'].values()),'exact-service-source')
    for table,keys in [('executables',PROGRAMS),('unit_sha256',UNIT_KEYS)]:
        require(isinstance(value[table],dict) and set(value[table])==keys and
                all(re.fullmatch('[a-f0-9]{64}',v) for v in value[table].values()),'bounded-service-programs-and-units')
    require(value['normalized_unit_sha256']==expected_unit_policy(),'same-generated-unit-policy')
    require(set(value['proofs'])=={'system_regex','media_runtime','media_isolation','proxy_qualification','proxy_review'} and
            all(re.fullmatch('[a-f0-9]{64}',v) for v in value['proofs'].values()),'bound-service-native-proofs')
    require(set(value['controls'])==set(CONTROLS) and all(value['controls'][k] is True for k in CONTROLS) and all(value[k] is False for k in FLAGS),'service-controls-and-scope')
    require(re.fullmatch('[a-f0-9]{64}',value['clip_sha256']) and type(value['clip_bytes']) is int and
            1<=value['clip_bytes']<=32*1024*1024 and type(value['decoded_frames']) is int and
            12<=value['decoded_frames']<=10000,'actual-recorded-clip')
    return value

def run(args):
    global phase
    hosted() # Before files, source reads, subprocesses, accounts, services or disks.
    head=command(['git','-c','safe.directory='+str(ROOT),'-C',ROOT,'rev-parse','HEAD'],'source-head').decode().strip()
    require(not command(['git','-c','safe.directory='+str(ROOT),'-C',ROOT,'status','--porcelain'],'clean-source').strip(),'clean-service-checkout')
    before=source_inputs()
    import candidate,system_regex_evidence,gateway_media_evidence,gateway_proxy_evidence
    regex=system_regex_evidence.current(ROOT)
    native=gateway_media_evidence.verify(ROOT,args.media_runtime,ffmpeg=args.ffmpeg,publisher=args.publisher,reader=args.reader)
    require(native['checkout']==dict(commit=head,clean=True),'same-service-media-source')
    proxy=gateway_proxy_evidence.verify(ROOT,head,candidate.source_inputs())
    proof_raw,proof=gateway_proxy_evidence.read(args.proxy_review/'qualification.json')
    review_raw,review=gateway_proxy_evidence.read(args.proxy_review/'review.json')
    require(hashlib.sha256(proof_raw).hexdigest()==review['qualification_sha256']==proxy['qualification_sha256']
            and digest(args.proxy_review/'nginx')==review['binary_sha256']==proxy['binary_sha256'],'same-service-proxy-bundle')
    binaries={name:getattr(args,name) for name in ('control','media','proxy','activity','sandbox','publisher','reader','ffmpeg')}
    hashes={name:digest(path) for name,path in binaries.items()};hashes['fixture_encoder']=digest('/usr/bin/ffmpeg')
    isolation_raw=args.isolation_qualification.read_bytes();require(len(isolation_raw)<=65536,'bounded-current-isolation')
    isolation=json.loads(isolation_raw)
    require(hashlib.sha256(isolation_raw).hexdigest()==native['isolation_sha256'] and
            isolation['executables']['launcher']==hashes['sandbox'] and isolation['executables']['ffmpeg']==hashes['ffmpeg'],'same-service-isolation-image')
    prefix='nxrs-es-'+secrets.token_hex(4);root=Path('/run')/prefix;conf=root/'conf';volume=root/'volume'
    control=prefix+'-control';target=prefix+'-monitor.target';stopper=prefix+'-monitor-stop.service';source_unit=prefix+'-source.service'
    state=Path('/var/lib')/control;mapping='es'+secrets.token_hex(8);device=Path('/dev/mapper')/mapping
    require(not root.exists() and not state.exists() and not device.exists(),'new-service-resources')
    mount_unit=command(['systemd-escape','--path','--suffix=mount',volume],'owned-mount-name').decode().strip()
    require(re.fullmatch('[A-Za-z0-9_.\\-]+',mount_unit),'fixed-owned-mount-unit')
    rendered=units(prefix,root,mapping,mount_unit);written=[];account=False;loop=None;mapped=False;cleanup=False
    roles=[prefix+'-'+name+'.service' for name in ('control','media','proxy','activity')]+[source_unit]
    tokens={name:secrets.token_hex(32) for name in ('viewer','owner','publisher','recorder','management')}
    ports={};used=set()
    for name in ('https','media','ice','rtsp','api','auth','management'):
        while True:
            with socket.socket(type=socket.SOCK_DGRAM if name=='ice' else socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            if port not in used:ports[name]=port;used.add(port);break
    image=root/'volume.img';result=None
    try:
        root.mkdir(mode=0o711);conf.mkdir(mode=0o700);(root/'bin').mkdir(mode=0o755);volume.mkdir(mode=0o000)
        for name,path in binaries.items():shutil.copyfile(path,root/'bin'/name);(root/'bin'/name).chmod(0o755)
        shutil.copyfile(Path(__file__).with_name('systemd_recording_source.py'),root/'bin/source.py');(root/'bin/source.py').chmod(0o644)
        artifact=root/'proxy-artifact';artifact.mkdir(mode=0o755)
        for original,name in [('nginx','nginx'),('review.json','approval.json')]:
            shutil.copyfile(args.proxy_review/original,artifact/name);(artifact/name).chmod(0o755 if name=='nginx' else 0o644)
        private(conf/'account.conf','u '+control+' - "owned encrypted service fixture" /nonexistent /usr/sbin/nologin\n')
        try:pwd.getpwnam(control)
        except KeyError:pass
        else:require(False,'new-fixture-account')
        account=True;command(['systemd-sysusers',conf/'account.conf'],'create-owned-account');user=pwd.getpwnam(control)
        state.mkdir(mode=0o700);os.chown(state,user.pw_uid,user.pw_gid)
        # The initializer runs only for this new enrollment, never during recovery.
        command(['runuser','-u',control,'--','/usr/bin/python3',ROOT/'gateway/deploy/strict_vpn.py','init-state',state],'new-denial-state')
        key=conf/'volume.key';private(key,secrets.token_bytes(32))
        with open(image,'xb',opener=lambda p,f:os.open(p,f,0o600)) as output:output.truncate(256*1024*1024)
        loop=command(['losetup','--find','--show',image],'attach-owned-loop').decode().strip();require(re.fullmatch('/dev/loop[0-9]+',loop),'owned-loop-device')
        identifier=str(uuid.uuid4())
        command(['cryptsetup','luksFormat','--batch-mode','--type','luks2','--uuid',identifier,'--pbkdf','pbkdf2','--pbkdf-force-iterations','1000',
                 '--key-file',key,loop],'format-owned-volume')
        command(['cryptsetup','open','--key-file',key,loop,mapping],'unlock-owned-volume');mapped=True
        command(['mkfs.ext4','-q',device],'format-owned-storage')
        protection=dict(layout_version=2,mount=str(volume),luks_uuid=identifier)
        storage=dict(directory=str(volume/'activity'),camera_ids=['cam_reference'],retention_seconds=259200,max_bytes=64*1024*1024,max_events=100,protection=protection)
        sha=lambda token:hashlib.sha256(token.encode()).hexdigest()
        config=dict(listen='127.0.0.1:'+str(ports['api']),auth_listen='127.0.0.1:'+str(ports['auth']),grant_ttl_seconds=60,max_grants=64,
            credential_window=dict(not_before=int(time.time())-1,expires_at=int(time.time())+3600),
            media_origins=['https://127.0.0.1:'+str(ports['https'])],activity=storage,
            cameras=[dict(camera_id='cam_reference',gateway_id='gw_reference',site_id='site_reference',name='SYNTHETIC service fixture',
                source='synthetic',stream_path='reference',whep_endpoint='https://127.0.0.1:'+str(ports['https'])+'/reference/whep')],
            viewers=[dict(viewer_id=name,site_id='site_reference',camera_ids=['cam_reference'],token_sha256=sha(tokens[token]),manage_recordings=token=='owner')
                for name,token in [('viewer_reference','viewer'),('owner_reference','owner')]],
            publishers=[dict(username='publisher_reference',camera_id='cam_reference',token_sha256=sha(tokens['publisher']))],
            recorders=[dict(username='recorder_reference',camera_id='cam_reference',token_sha256=sha(tokens['recorder']))])
        private(conf/'config.json',config)
        private(conf/'security.json',dict(revocations_file=str(state/'revocations.json'),media_api='127.0.0.1:'+str(ports['management']),media_api_token=tokens['management']))
        private(conf/'mediamtx.json',strict_vpn.media_config('127.0.0.1',ports,ports['management']))
        private(conf/'recorder.token',tokens['recorder'])
        private(conf/'activity.json',dict(storage=storage,camera_id='cam_reference',rtsp='127.0.0.1:'+str(ports['rtsp']),stream_path='reference',
            username='recorder_reference',token_file='/run/'+prefix+'-activity/recorder.token',ffmpeg=str(root/'bin/ffmpeg'),
            reader=str(root/'bin/reader'),media_sandbox=str(root/'bin/sandbox'),motion_percent=2,pre_roll_seconds=4,post_roll_seconds=4,max_clip_seconds=20))
        private(conf/'source.json',dict(token=tokens['publisher'],publisher=str(root/'bin/publisher'),ffmpeg='/usr/bin/ffmpeg',
            rtsp='127.0.0.1:'+str(ports['rtsp']),username='publisher_reference'))
        (conf/'tls').mkdir(mode=0o700)
        command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',conf/'tls/privkey.pem',
            '-out',conf/'tls/fullchain.pem','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1,DNS:localhost'],'fixture-tls')
        (conf/'tls/privkey.pem').chmod(0o600)
        private(conf/'nginx.conf',vpn.nginx_config('127.0.0.1','127.0.0.1',ports,cert='/run/credentials/'+prefix+'-proxy.service/server.crt',
            key='/run/credentials/'+prefix+'-proxy.service/server.key',runtime='/run/'+prefix+'-proxy'))
        for name,text in rendered.items():
            destination=Path('/run/systemd/system')/name
            require(not destination.exists(),'new-owned-unit')
            destination.parent.mkdir(mode=0o755,exist_ok=True)
            with open(destination,'x') as output:written.append(destination);output.write(text)
        command(['systemctl','daemon-reload'],'load-owned-units')
        def state_of(unit):return lifetime.parse(command(['systemctl','show',unit,'--all',*lifetime.PROPERTY_ARGS],'read-owned-service-state'))
        def joined():return all(lifetime.quiescent(state_of(unit),unit) for unit in roles)
        def until(check,stage,seconds=20):
            global phase
            phase=stage;deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                if check():return
                time.sleep(.15)
            phase=stage;require(False,stage)
        def stop_group():
            command(['systemctl','stop',target,*roles],'stop-owned-group',seconds=60);until(joined,'join-owned-service-cgroups')
        def start_group():
            stop_group();command(['systemctl','stop',stopper],'release-failure-latch')
            command(['systemctl','reset-failed',target,stopper,*roles], 'reset-owned-group')
            command(['systemctl','start',target],'start-encrypted-service-group',seconds=60)
            command(['systemctl','start',source_unit],'start-owned-source')
        command(['systemctl','start',mount_unit],'mount-owned-encrypted-storage')
        require(os.path.ismount(volume),'real-service-encrypted-mount');os.chown(volume,user.pw_uid,user.pw_gid);volume.chmod(0o700)
        tls=ssl.create_default_context(cafile=str(conf/'tls/fullchain.pem'));origin='https://127.0.0.1:'+str(ports['https'])
        base='/v1/cameras/cam_reference/activity'
        def request(path,role='owner',method='GET',body=None,internal=False):
            data=json.dumps(body).encode() if body is not None else None
            headers={'Content-Type':'application/json'}
            if role:headers['Authorization']='Bearer '+tokens[role]
            address=('http://127.0.0.1:'+str(ports['auth']) if internal else origin)+path
            req=urllib.request.Request(address,data=data,headers=headers,method=method)
            try:
                with urllib.request.urlopen(req,context=tls,timeout=3) as response:code=response.status;raw=response.read(32*1024*1024+1)
            except urllib.error.HTTPError as response:code=response.code;raw=response.read(32*1024*1024+1)
            require(len(raw)<=32*1024*1024,'bounded-service-http-body');return code,raw
        def page():
            code,raw=request_with_busy_retry(lambda:request(base),'GET')
            return json.loads(raw) if code==200 else None
        def ready():
            try:
                code,raw=request('/internal/viewers/viewer_reference/revocation',None,internal=True)
                health_code,health_raw=request('/healthz',None)
                return code==200 and json.loads(raw)['media_control_ready'] is True and health_code==200 and json.loads(health_raw)=={'status':'ok'}
            except urllib.error.URLError as error:
                if isinstance(error.reason,ssl.SSLError):raise
                return False
            except (OSError,TimeoutError):return False
        start_group();until(ready,'actual-https-and-management-ready')
        for role in (None,'publisher','recorder'):require(request(base,role)[0]==401,'recording-role-isolation')
        metadata_checked=False
        def recorded():
            nonlocal metadata_checked
            for unit in roles:
                state_now=state_of(unit);group=state_now['ControlGroup']
                require(group=='/system.slice/'+unit,'owned-service-cgroup')
                files=list((Path('/sys/fs/cgroup')/group.lstrip('/')).rglob('cgroup.procs'));require(1<=len(files)<=256,'bounded-owned-cgroups')
                for file in files:
                    for pid in map(int,file.read_text().split()):
                        inspected=credential_free(pid,tokens.values());require(inspected is not False,'service-credential-boundary')
                        metadata_checked=metadata_checked or inspected is True
            value=page();return value and value['events']
        until(recorded,'automatic-encrypted-recording',40);event=page()['events'][0]
        code,clip=request(base+'/'+event['event_id']+'/clip');require(code==200 and len(clip)==event['clip_bytes'],'authorized-recorded-clip')
        clip_hash=hashlib.sha256(clip).hexdigest();private(root/'download.mp4',clip)
        command(['/usr/bin/ffmpeg','-nostdin','-v','error','-i',root/'download.mp4','-f','null','-'],'decode-recorded-clip')
        raw=command(['/usr/bin/ffprobe','-v','error','-select_streams','v:0','-count_frames','-show_entries','stream=nb_read_frames','-of','csv=p=0',root/'download.mp4'],'count-recorded-frames')
        require(re.fullmatch(rb'[0-9]{1,5}\s*',raw) and int(raw)>=12,'real-decoded-frame-count');frames=int(raw)
        require(request('/internal/viewers/viewer_reference/revoke',None,'POST',{},True)[0]==202,'persist-viewer-denial')
        require(request('/v1/cameras','viewer')[0]==403,'revoked-viewer-refused')
        denial=digest(state/'revocations.json')
        command(['systemctl','stop',mount_unit],'lose-owned-encrypted-mount',seconds=60)
        until(joined,'mount-loss-joined-entire-group',40)
        require(not os.path.ismount(volume) and not (volume/'activity').exists() and volume.stat().st_mode&0o777==0,'no-plaintext-service-fallback')
        start_group();until(ready,'recovered-service-readiness')
        require(request('/v1/cameras','viewer')[0]==403 and digest(state/'revocations.json')==denial,'same-denial-after-service-recovery')
        require(request('/v1/cameras','owner')[0]==200,'healthy-viewer-after-recovery')
        code,recovered=request(base+'/'+event['event_id']+'/clip')
        require(code==200 and recovered==clip and hashlib.sha256(recovered).hexdigest()==clip_hash,'same-recorded-bytes-after-mount-recovery')
        require(metadata_checked,'executed-service-metadata-required')
        stop_group();command(['systemctl','stop',mount_unit],'retire-owned-mount')
        raw_unit_hashes,normalized_unit_hashes=unit_digests(rendered,prefix,mapping,mount_unit)
        require(all(path.read_text()==rendered[path.relative_to('/run/systemd/system').as_posix()] for path in written),'unchanged-executed-unit-files')
        result=dict(schema=1,scope=SCOPE,qualified=True,checkout=dict(commit=head,clean=True),inputs=before,executables=hashes,
            unit_sha256=raw_unit_hashes,normalized_unit_sha256=normalized_unit_hashes,
            proofs=dict(system_regex=regex['qualification_sha256'],media_runtime=digest(args.media_runtime/'qualification.json'),media_isolation=native['isolation_sha256'],
                proxy_qualification=proxy['qualification_sha256'],proxy_review=hashlib.sha256(review_raw).hexdigest()),
            controls=dict.fromkeys(CONTROLS,True),clip_sha256=clip_hash,clip_bytes=len(clip),decoded_frames=frames,**dict.fromkeys(FLAGS,False))
    finally:
        original_phase=phase if sys.exc_info()[0] is not None else None
        # Only exact resources allocated by this invocation; never lazy/forced
        # unmount, global pkill, or deletion before every cgroup/device retires.
        if written:
            command(['systemctl','stop',target,*roles,mount_unit,stopper,prefix+'-network.service'],'cleanup-owned-services',seconds=70)
            for unit in roles:
                state_now=lifetime.parse(command(['systemctl','show',unit,'--all',*lifetime.PROPERTY_ARGS],'cleanup-cgroup-state'))
                require(lifetime.quiescent(state_now,unit),'cleanup-owned-cgroup-join')
        require(not os.path.ismount(volume),'cleanup-owned-mount-join')
        if mapped or device.exists():command(['cryptsetup','close',mapping],'cleanup-owned-mapping')
        require(not device.exists(),'cleanup-owned-mapping-join')
        if image.exists():
            associated=command(['losetup','-j',image],'find-owned-loop').splitlines()
            require(len(associated)<=1,'one-owned-loop-association')
            for row in associated:
                match=re.fullmatch(rb'(/dev/loop[0-9]+): [^\r\n]+ \('+re.escape(os.fsencode(image))+rb'\)',row)
                require(match is not None,'exact-owned-loop-backing')
                if loop:require(match[1].decode()==loop,'same-owned-loop')
                command(['losetup','--detach',match[1].decode()],'cleanup-owned-loop')
            require(not command(['losetup','-j',image],'joined-owned-loop').strip(),'cleanup-owned-loop-join')
        for path in reversed(written):
            path.unlink()
            if path.parent!=Path('/run/systemd/system'):path.parent.rmdir()
        if written:command(['systemctl','daemon-reload'],'cleanup-owned-unit-registration')
        if account:
            try:pwd.getpwnam(control)
            except KeyError:pass
            else:command(['userdel',control],'cleanup-owned-account')
        if state.exists():shutil.rmtree(state)
        if root.exists():shutil.rmtree(root)
        cleanup=True
        if original_phase is not None:phase=original_phase
    require(result is not None and cleanup and before==source_inputs(),'complete-current-service-evidence')
    require(head==command(['git','-c','safe.directory='+str(ROOT),'-C',ROOT,'rev-parse','HEAD'],'final-service-source').decode().strip()
            and not command(['git','-c','safe.directory='+str(ROOT),'-C',ROOT,'status','--porcelain'],'final-clean-service-source').strip(),'unchanged-clean-service-source')
    validate(result);raw=(json.dumps(result,sort_keys=True,indent=2)+'\n').encode();require(len(raw)<=131072,'bounded-public-service-receipt')
    with open(args.report,'xb') as output:output.write(raw)
    print('ENCRYPTED SERVICES PASS: actual generated units, recorded bytes, mount-loss group retirement and durable recovery; boot/device gates remain open')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('control','media','proxy','activity','sandbox','publisher','reader','ffmpeg','media-runtime','proxy-review','isolation-qualification','report'):
        parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args()
    try:run(args)
    except Exception:
        parser.exit(1,'Encrypted service qualification refused ('+phase+').\n')

if __name__=='__main__':main()
