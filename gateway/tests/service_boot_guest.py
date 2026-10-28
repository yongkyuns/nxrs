#!/usr/bin/env python3
"""Exact private VM guest: autostart, recording retention and locked-key refusal.

This fixture never runs on a personal host. Its test key is stored in the private
image; production key custody, VPN, device TLS and physical recovery stay open.
"""
import errno,hashlib,json,os,pwd,re,shlex,shutil,socket,ssl,stat,subprocess,sys,time,urllib.error,urllib.request,uuid
from pathlib import Path
import service_boot_units as b
import service_boot_xml as xml
import encrypted_service_reference as service
import service_lifetime as lifetime
import proxy_pair
from credential_process import credential_free
from activity_reference import request_with_busy_retry

BASE=b.BASE;ROOT=b.ROOT;CONF=ROOT/'conf';VOLUME=ROOT/'volume'
ACCOUNT=b.PREFIX+'-control';STATE=Path('/var/lib')/ACCOUNT
DEVICE=Path('/dev/mapper')/b.MAPPING
SOURCE=b.PREFIX+'-source.service'
ROLES=[b.PREFIX+'-'+name+'.service' for name in ('control','media','proxy','activity')]+[SOURCE]
CASES=b.CASES
stage='context';admitted=False;last_command_exit=None;reported_locations=[]

def require(value,category):b.require(value,category)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def digest(path):return service.digest(path)

def protected(path,maximum):
    with open(path,'rb',opener=lambda name,flags:os.open(name,flags|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        before=os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid==0 and before.st_nlink==1 and
                before.st_mode&0o022==0 and before.st_size<=maximum,'protected-bounded-guest-file')
        raw=source.read(maximum+1);after=os.fstat(source.fileno())
        require(len(raw)<=maximum and (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),'unchanged-guest-file')
    return raw

def parse(raw):
    def unique(rows):
        result={}
        for key,value in rows:require(key not in result,'unique-guest-field');result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)

def atomic(path,value):
    raw=value if isinstance(value,bytes) else b.encoded(value)+b'\n'
    temporary=path.with_name('.'+path.name+'.pending')
    service.private(temporary,raw);os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def context():
    global admitted
    require(sys.platform=='linux' and os.getuid()==0,'isolated-guest-root-required')
    case,nonce=b.command_case(Path('/proc/cmdline').read_text().strip())
    require(Path('/proc/1/comm').read_text().strip()=='systemd' and Path('/sys/fs/cgroup/cgroup.controllers').is_file(),
            'guest-pid1-and-cgroups')
    plan_raw=protected(BASE/'plan.json',131072);plan=parse(plan_raw)
    b.validate_plan(plan)
    require(plan['nonce']==nonce and b.encoded(plan)+b'\n'==plan_raw,'exact-guest-plan')
    require(os.uname().release==plan['kernel_release'] and digest('/proc/1/exe')==plan['pid1_sha256'] and
            sorted(p.name for p in Path('/sys/class/net').iterdir())==['lo'],'exact-isolated-kernel-and-pid1')
    roots=[line.split() for line in Path('/proc/mounts').read_text().splitlines() if line.split()[1]=='/']
    require(len(roots)==1 and roots[0][0]=='/dev/vda' and roots[0][2]=='ext4','owned-virtual-root-device')
    for parent in (BASE,Path('/usr/src/nxrs'),Path('/etc/systemd/system')):
        meta=parent.lstat();require(stat.S_ISDIR(meta.st_mode) and meta.st_uid==0 and meta.st_mode&0o022==0,'protected-guest-parent')
    manifest_raw=protected(BASE/'source-manifest.json',1024*1024)
    require(sha(manifest_raw)==plan['source_manifest_sha256'],'bound-guest-source-manifest')
    manifest=parse(manifest_raw)
    sys.path.insert(0,'/usr/src/nxrs/tools/native-packages')
    import guest_source
    guest_source.verify_tree(Path('/usr/src/nxrs'),manifest,(0,0))
    require(manifest['head']==plan['head'] and plan['inputs']==service.source_inputs(),'same-guest-service-source')
    require(1<=len(plan['files'])<=512,'bounded-staged-guest-images')
    for name,expected in plan['files'].items():
        require(re.fullmatch(r'/(?:var/lib/service-boot|usr/lib/nxrs-gateway-media-qualification|usr/lib/nxrs-gateway-system-xml-qualification)/[A-Za-z0-9_./+-]+',name)
                and '..' not in Path(name).parts and re.fullmatch('[a-f0-9]{64}',expected),'owned-guest-image-name')
        require(digest(name)==expected,'exact-staged-guest-image')
    for name,target in plan['runtime_links'].items():
        info=Path(name).lstat()
        require(stat.S_ISLNK(info.st_mode) and info.st_uid==0 and os.readlink(name)==target,'exact-service-media-soname')
    require(plan['unit_sha256']==b.unit_hashes() and all(digest(Path('/etc/systemd/system')/name)==expected
            for name,expected in plan['unit_sha256'].items()),'exact-autostart-unit-group')
    for file,key in [('proxy-parent.json','parent_proxy_qualification_sha256'),('proxy-review.json','parent_proxy_review_sha256')]:
        require(sha(protected(BASE/file,1024*1024))==plan[key],'bound-parent-proxy-evidence')
    require(xml.LINK.is_symlink() and os.readlink(xml.LINK)==xml.LINK_TARGET and
        xml.LINK.resolve(strict=True)==xml.LIBRARY,'exact-guest-xml-soname')
    admitted=True
    return case,plan

def command(args,label,accepted=(0,),seconds=45):
    global stage,last_command_exit
    require(admitted,'guest-context-before-native-operations');stage=label;last_command_exit=None
    result=subprocess.run([str(value) for value in args],capture_output=True,timeout=seconds,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C','PYTHONDONTWRITEBYTECODE':'1'})
    require(len(result.stdout)+len(result.stderr)<=1024*1024,'bounded-guest-command-output')
    last_command_exit=result.returncode
    require(result.returncode in accepted,'guest-command-refused')
    return result.stdout

def until(check,label,seconds=40):
    global stage
    stage=label;deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if check():return
        time.sleep(.2)
    stage=label;require(False,'guest-deadline')

def state_of(unit,*,seconds=45):
    return lifetime.parse(command(['systemctl','show',unit,'--all',*lifetime.PROPERTY_ARGS],'service-state',seconds=seconds))

def activation(unit,*,seconds=45):
    # Targets have no MainPID, Result or cgroup properties. Never treat a
    # service-only parser as evidence of a target's automatic activation.
    raw=command(['systemctl','show',unit,'-p','LoadState','-p','ActiveState','-p','SubState'],'target-state',seconds=seconds)
    rows=raw.decode('ascii').splitlines();result={}
    for row in rows:
        key,separator,value=row.partition('=')
        require(separator and key not in result,'unique-target-state');result[key]=value
    require(set(result)=={'LoadState','ActiveState','SubState'} and result['LoadState']=='loaded','complete-target-state')
    return result

def joined():return all(lifetime.quiescent(state_of(unit),unit) for unit in ROLES)

def job_identities(names):
    # systemd may journal a device's canonical sysfs unit instead of the
    # /dev/mapper alias requested by the mount. Resolve only our fixed units.
    raw=command(['systemctl','show',*names,'-p','Id','-p','Names','--all'],
        'activation-unit-identities',seconds=5)
    require(len(raw)<=32768,'bounded-private-unit-identities')
    blocks=[block for block in raw.decode('ascii').strip().split('\n\n') if block.strip()]
    require(1<=len(blocks)<=len(names),'bounded-private-unit-identity-rows')
    result=dict(names)
    for block in blocks:
        fields={}
        for line in block.splitlines():
            key,separator,value=line.partition('=')
            require(separator and key in ('Id','Names') and key not in fields,'private-unit-identity-schema');fields[key]=value
        require(set(fields)=={'Id','Names'},'complete-private-unit-identity')
        # systemd's scalar Id is literal, but Names is a string array printed
        # through shell_maybe_quote(). Parse its quoting as data; never execute
        # it. In particular, preserve the backslashes in escaped unit names.
        aliases=shlex.split(fields['Names'],comments=False,posix=True)
        require(1<=len(aliases)<=128 and fields['Id'] in aliases and
            all(len(name)<=512 and re.fullmatch(r'[A-Za-z0-9_.@:\\-]+',name) for name in aliases),'bounded-private-unit-aliases')
        roles={names[name] for name in aliases if name in names}
        require(len(roles)==1,'owned-private-unit-identity');role=roles.pop()
        for name in aliases:
            require(name not in result or result[name]==role,'unambiguous-private-unit-alias');result[name]=role
    require(len(result)<=128,'bounded-owned-unit-alias-inventory')
    return result

def job_snapshot(*, canonical=True):
    # Query only systemd's structured job identity/outcome fields. Journal
    # messages, invocation IDs, timestamps, PIDs and unit names never leave the
    # guest. Unknown values project to a constant; unrelated units are dropped.
    names={b.PROVIDER:'provider',b.TARGET:'target',b.MOUNT:'mount',
        'dev-mapper-'+b.MAPPING+'.device':'device',b.PREFIX+'-monitor-stop.service':'stopper'}
    names.update({b.PREFIX+'-'+role+'.service':role for role in ('control','media','proxy','activity')})
    if canonical:names=job_identities(names)
    raw=command(['journalctl','-b','_PID=1',*('UNIT='+name for name in sorted(names)),
        '--output=json','--output-fields=UNIT,JOB_TYPE,JOB_RESULT','--no-pager','--lines=128'],
        'activation-job-results',seconds=5)
    require(len(raw)<=65536,'bounded-private-job-fields')
    lines=raw.splitlines();require(len(lines)<=128,'bounded-private-job-rows')
    rows=[]
    for line in lines:
        value=parse(line);require(isinstance(value,dict),'structured-private-job-row')
        name=value.get('UNIT');role=names.get(name) if isinstance(name,str) else None
        if role is None or 'JOB_TYPE' not in value or 'JOB_RESULT' not in value:continue
        operation=value['JOB_TYPE'];result=value['JOB_RESULT']
        item=dict(role=role,operation=operation if isinstance(operation,str) and operation in b.JOB_OPERATIONS else 'other',
            result=result if isinstance(result,str) and result in b.JOB_RESULTS else 'other')
        if item in rows:rows.remove(item)
        rows.append(item);rows=rows[-16:]
    return b.public_job_results(rows)


def activation_snapshot():
    # Only fixed role names, booleans, public result categories and bounded
    # process statuses leave the private guest. No unit paths or logs are read.
    rows=[]
    for role in b.ACTIVATION_ROLES:
        if role=='target':
            rows.append(dict(role=role,active=activation(b.TARGET,seconds=5)['ActiveState']=='active',result=None,exit_status=None))
        elif role=='mount':
            rows.append(dict(role=role,active=os.path.ismount(VOLUME),result=None,exit_status=None))
        else:
            unit=b.PROVIDER if role=='provider' else b.PREFIX+'-'+role+'.service'
            state=state_of(unit,seconds=5)
            status=command(['systemctl','show',unit,'-p','ExecMainStatus','--value'],'activation-status',seconds=5).strip()
            require(re.fullmatch(rb'[0-9]{1,3}',status) and int(status)<=255,'bounded-activation-status')
            result=state['Result'] or 'none'
            rows.append(dict(role=role,active=state['ActiveState']=='active',exit_status=int(status),
                result=result if result in b.ACTIVATION_RESULTS else 'other'))
    return b.public_activation(rows)

def prerequisite_snapshot():
    # Failure-only observations of this guest's two fixed owned units. Never
    # export unit names, messages, device aliases, paths or raw property values.
    require(admitted,'guest-context-before-prerequisite-inspection')
    rows=[];timeouts={}
    def selected(value,known):return value if value in known else 'other'
    def timeout(value):
        return {'1min 30s':'ninety-seconds','90s':'ninety-seconds',
                'infinity':'infinity','0':'zero'}.get(value,'other')
    for role,unit in (('device','dev-mapper-'+b.MAPPING+'.device'),('mount',b.MOUNT)):
        properties=['LoadState','ActiveState','SubState','Job']
        properties+=['JobTimeoutUSec','JobRunningTimeoutUSec'] if role=='device' else ['Result']
        raw=command(['systemctl','show',unit,'--all',*(item for name in properties for item in ('-p',name))],
                    'prerequisite-unit-state',seconds=5)
        require(len(raw)<=4096,'bounded-private-prerequisite-properties')
        fields={}
        for line in raw.decode('ascii').splitlines():
            name,separator,value=line.partition('=')
            require(separator and name in properties and name not in fields,'unique-private-prerequisite-property')
            fields[name]=value
        require(set(fields)==set(properties) and re.fullmatch('[0-9]{1,10}',fields['Job'])
            and int(fields['Job'])<=4294967295,'complete-private-prerequisite-properties')
        rows.append(dict(role=role,load=selected(fields['LoadState'],b.LOAD_STATES),
            state=selected(fields['ActiveState'],b.ACTIVE_STATES),
            substate=selected(fields['SubState'],b.PREREQUISITE_SUBSTATES),job_pending=int(fields['Job'])!=0,
            result=None if role=='device' else selected(fields['Result'] or 'none',b.ACTIVATION_RESULTS)))
        if role=='device':timeouts=dict(device_job_timeout=timeout(fields['JobTimeoutUSec']),
                                       device_running_timeout=timeout(fields['JobRunningTimeoutUSec']))
    return b.public_prerequisites(dict(mapping_present=DEVICE.exists(),units=rows,**timeouts))


def configuration(user):
    """New enrollment exactly once, followed by durable copies at every boot."""
    global stage
    stage='new-enrollment'
    require(not (BASE/'conf').exists() and not STATE.exists(),'new-guest-enrollment-only')
    durable=BASE/'conf';durable.mkdir(mode=0o700);(durable/'tls').mkdir(mode=0o700)
    STATE.mkdir(mode=0o700);os.chown(STATE,user.pw_uid,user.pw_gid)
    command(['runuser','-u',ACCOUNT,'--','/usr/bin/python3','-B','/usr/src/nxrs/gateway/deploy/strict_vpn.py','init-state',STATE],
            'initial-enrollment')
    stage='new-enrollment-files'
    tokens={name:os.urandom(32).hex() for name in ('viewer','owner','publisher','recorder','management')}
    # Exact fixed loopback ports are local to this no-NIC guest, not host ports.
    ports=dict(https=18443,media=18889,ice=18189,rtsp=18554,api=18080,auth=18081,management=19997)
    storage=dict(directory=str(VOLUME/'activity'),camera_ids=['cam_reference'],retention_seconds=259200,
        max_bytes=64*1024*1024,max_events=100,protection=dict(layout_version=2,mount=str(VOLUME),luks_uuid=str(uuid.uuid4())))
    config=dict(listen='127.0.0.1:'+str(ports['api']),auth_listen='127.0.0.1:'+str(ports['auth']),
        grant_ttl_seconds=60,max_grants=64,credential_window=dict(not_before=int(time.time())-1,expires_at=int(time.time())+7200),
        media_origins=['https://127.0.0.1:'+str(ports['https'])],activity=storage,
        cameras=[dict(camera_id='cam_reference',gateway_id='gw_reference',site_id='site_reference',name='SYNTHETIC virtual boot',
            source='synthetic',stream_path='reference',whep_endpoint='https://127.0.0.1:'+str(ports['https'])+'/reference/whep')],
        viewers=[dict(viewer_id=name,site_id='site_reference',camera_ids=['cam_reference'],token_sha256=sha(tokens[token].encode()),
                      manage_recordings=token=='owner') for name,token in [('viewer_reference','viewer'),('owner_reference','owner')]],
        publishers=[dict(username='publisher_reference',camera_id='cam_reference',token_sha256=sha(tokens['publisher'].encode()))],
        recorders=[dict(username='recorder_reference',camera_id='cam_reference',token_sha256=sha(tokens['recorder'].encode()))])
    values={'config.json':config,'tokens.json':tokens,'ports.json':ports,
        'security.json':dict(revocations_file=str(STATE/'revocations.json'),media_api='127.0.0.1:'+str(ports['management']),media_api_token=tokens['management']),
        'mediamtx.json':service.strict_vpn.media_config('127.0.0.1',ports,ports['management']),
        'recorder.token':tokens['recorder'],
        'activity.json':dict(storage=storage,camera_id='cam_reference',rtsp='127.0.0.1:'+str(ports['rtsp']),stream_path='reference',
            username='recorder_reference',token_file='/run/'+b.PREFIX+'-activity/recorder.token',ffmpeg=str(ROOT/'bin/ffmpeg'),
            reader=str(ROOT/'bin/reader'),media_sandbox=str(ROOT/'bin/sandbox'),motion_percent=2,pre_roll_seconds=4,
            post_roll_seconds=4,max_clip_seconds=20),
        'source.json':dict(token=tokens['publisher'],publisher=str(ROOT/'bin/publisher'),ffmpeg='/usr/bin/ffmpeg',
                           rtsp='127.0.0.1:'+str(ports['rtsp']),username='publisher_reference')}
    for name,value in values.items():service.private(durable/name,value)
    service.private(durable/'nginx.conf',service.vpn.nginx_config('127.0.0.1','127.0.0.1',ports,
        cert='/run/credentials/'+b.PREFIX+'-proxy.service/server.crt',
        key='/run/credentials/'+b.PREFIX+'-proxy.service/server.key',runtime='/run/'+b.PREFIX+'-proxy'))
    command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',durable/'tls/privkey.pem',
        '-out',durable/'tls/fullchain.pem','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1,DNS:localhost'],'fixture-certificate')
    (durable/'tls/privkey.pem').chmod(0o600)

def stage_runtime(plan):
    global stage
    stage='stage-runtime-inventory'
    directory=BASE/'bin';destination=ROOT/'bin'
    expected={Path(name).name for name in plan['files'] if Path(name).parent==directory}
    require(expected and {path.name for path in directory.iterdir()}==expected,'exact-staged-runtime-inventory')
    require(all(re.fullmatch('[a-z][a-z-]{0,31}',name) for name in expected),'named-staged-runtime')
    require(not os.path.ismount(destination) and not list(destination.iterdir()),'new-runtime-bind-target')
    for name in sorted(expected):
        original=directory/name
        require(original.is_file() and not original.is_symlink() and digest(original)==plan['files'][str(original)],
                'exact-staged-runtime-image')
    # The bounded VM has 2 GiB RAM. Debug executables can exhaust /run's
    # tmpfs when duplicated there. Expose the already verified disk inventory
    # without copying it, retaining the generated application's original paths.
    command(['mount','--bind',directory,destination],'bind-runtime-images')
    command(['mount','-o','remount,bind,ro,nodev,nosuid',destination],'protect-runtime-images')
    stage='verify-runtime-bind'
    flags=os.ST_RDONLY|os.ST_NODEV|os.ST_NOSUID
    require(os.path.ismount(destination) and os.statvfs(destination).f_flag&flags==flags,'read-only-runtime-bind')
    require({path.name for path in destination.iterdir()}==expected,'exact-bound-runtime-inventory')
    for name in sorted(expected):
        original=directory/name;bound=destination/name;before=original.stat();after=bound.lstat()
        require(stat.S_ISREG(after.st_mode) and (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino)
                and digest(bound)==plan['files'][str(original)],'same-disk-runtime-image')
    stage='stage-runtime-proxy-review'
    (ROOT/'proxy-artifact').mkdir(mode=0o755)
    review=ROOT/'proxy-review';review.mkdir(mode=0o700)
    shutil.copyfile(directory/'nginx',review/'nginx');(review/'nginx').chmod(0o700)
    require(digest(review/'nginx')==plan['files'][str(directory/'nginx')],'exact-copied-review-image')
    stage='stage-runtime-source'
    source=Path('/usr/src/nxrs/gateway/tests/systemd_recording_source.py')
    shutil.copyfile(source,ROOT/'source.py');(ROOT/'source.py').chmod(0o644)
    require(digest(source)==digest(ROOT/'source.py'),'exact-fixture-source')
    return review

def proxy_store_flags():
    target=ROOT/'proxy-artifact'
    require(os.path.ismount(target),'owned-proxy-store-mounted')
    return os.statvfs(target).f_flag

def stage_proxy_store(case):
    require(case in CASES,'named-proxy-store-case')
    source=BASE/('proxy-store-'+case);target=ROOT/'proxy-artifact'
    require(not source.exists() and not os.path.ismount(target) and not list(target.iterdir()),'new-proxy-store')
    source.mkdir(mode=0o755)
    command(['mount','--bind',source,target],'bind-proxy-store')
    command(['mount','-o','remount,bind,rw,exec,nodev,nosuid',target],'admit-proxy-store-execution')
    flags=proxy_store_flags()
    require(target.samefile(source) and flags&(os.ST_RDONLY|os.ST_NOEXEC)==0 and
            flags&(os.ST_NODEV|os.ST_NOSUID)==os.ST_NODEV|os.ST_NOSUID,'executable-guest-disk-proxy-store')

def protect_proxy_store():
    command(['mount','-o','remount,bind,ro,exec,nodev,nosuid',ROOT/'proxy-artifact'],'protect-proxy-store')
    flags=proxy_store_flags();required=os.ST_RDONLY|os.ST_NODEV|os.ST_NOSUID
    require(flags&required==required and not flags&os.ST_NOEXEC,'readonly-executable-proxy-store')

def retire_proxy_store():
    target=ROOT/'proxy-artifact'
    require(os.path.ismount(target),'owned-proxy-store-still-mounted')
    command(['umount',target],'release-proxy-store')
    require(not os.path.ismount(target) and not list(target.iterdir()),'joined-proxy-store')

def retire_runtime():
    destination=ROOT/'bin'
    require(os.path.ismount(destination),'owned-runtime-still-mounted')
    command(['umount',destination],'release-runtime-images')
    require(not os.path.ismount(destination) and not list(destination.iterdir()),'joined-runtime-bind')

def failure_phase(error):
    # Fixed errno categories carry no exception text, private paths or command
    # output. They distinguish storage/permission failures without exporting a
    # guest disk or logs. Unexpected errors retain the operation's fixed phase.
    public=stage if re.fullmatch('[a-z][a-z-]{0,63}',stage) else 'unclassified'
    categories={errno.ENOSPC:'no-space',errno.EDQUOT:'quota',errno.EACCES:'permission',errno.EPERM:'permission',
                errno.ENOENT:'missing',errno.EROFS:'read-only',errno.EIO:'io'}
    suffix=categories.get(error.errno) if isinstance(error,OSError) else None
    return public+'-'+suffix if suffix and len(public)+len(suffix)<64 else public

def public_locations(error):
    import failure_location
    sources={str(service.ROOT/name):name for name in b.PUBLIC_FAILURE_SOURCES}
    values=[]
    # The HTTPS corpus already records source coordinates for caught unittest
    # failures. Project only that bounded contract, never test output or text.
    try:
        report=parse(protected(ROOT/'boundary.json',4096))
        values.extend(failure_location.project_locations(report.get('failure_locations',[]),b.PUBLIC_FAILURE_SOURCES))
    except Exception:pass
    values.extend(failure_location.trace_locations(error,sources))
    result=[]
    for value in values:
        if value not in result:result.append(value)
        if len(result)==8:break
    return b.public_failure_locations(result)

def preparation_failure(value):
    require(isinstance(value,dict) and set(value)=={'phase','command_exit','locations'} and
            isinstance(value['phase'],str) and re.fullmatch('[a-z][a-z-]{0,63}',value['phase']) and
            (value['command_exit'] is None or (type(value['command_exit']) is int and -128<=value['command_exit']<=255)),
            'fixed-guest-prepare-failure')
    b.public_failure_locations(value['locations'])
    return value

def initialize_storage(user):
    require(admitted,'guest-context-before-initialization')
    # PID 1 observes every mount transition, including our temporary external
    # mount. Using VOLUME here can complete its queued .mount start and then
    # invalidate the BindsTo dependents when initialization unmounts it, before
    # this provider has returned. Prepare through a disjoint owned mountpoint.
    initial=BASE/'initialize-volume'
    require(not os.path.lexists(initial),'new-private-initialization-mountpoint')
    initial.mkdir(mode=0o700)
    try:
        command(['mount','-t','ext4','-o','rw,nosuid,nodev,noexec',DEVICE,initial],'initialize-mount')
        require(os.path.ismount(initial),'actual-private-initialization-mount')
        service.provision_storage(initial,user.pw_uid,user.pw_gid)
    finally:
        # A timed-out command may have allocated the mount before losing its
        # returned status. Inspect only this invocation's fixed owned point.
        if os.path.ismount(initial):command(['umount',initial],'join-initialization-mount')
        require(not os.path.ismount(initial),'joined-private-initialization-mount')
        initial.rmdir()

def current_xml(plan):
    require(admitted,'guest-context-before-xml-qualification')
    import candidate
    value=candidate.xml_runtime(plan['head'])
    require(value['proof']==xml.validate_plan(plan),'same-current-guest-xml-parent')
    return value['proof']

def qualify_xml(plan):
    proof=current_xml(plan)
    command([xml.PROBE,xml.PROBE],'refuse-wrong-guest-xml-image',accepted=(1,))
    raw=command([xml.PROBE,xml.LIBRARY],'qualify-guest-xml-abi')
    require(raw.strip()==b'EXPAT_2_9_0_ABI_AND_LOADER_OK' and current_xml(plan)==proof,'actual-guest-xml-abi')
    value=dict(schema=1,scope=xml.SCOPE,proof=proof,probe_sha256=digest(xml.PROBE),
        current_python_exact_image=True,c_abi_qualified=True,wrong_image_refused=True,
        unresolved_published_advisories=['CVE-2025-66382','CVE-2026-113351','CVE-2026-115506'],full_native_advisory_closure=False,deployment_permitted=False)
    return xml.validate(value,plan)

def prepare(case,plan):
    global stage
    stage='fresh-boot-resources'
    require(not ROOT.exists() and not DEVICE.exists(),'fresh-boot-resources')
    stage='qualify-guest-xml'
    atomic(BASE/('xml-'+case+'.json'),qualify_xml(plan))
    ROOT.mkdir(mode=0o711);(ROOT/'bin').mkdir(mode=0o755);VOLUME.mkdir(mode=0o000)
    command(['ip','link','set','lo','up'],'owned-loopback-interface')
    review=stage_runtime(plan)
    stage_proxy_store(case)
    if case=='provisioned':
        stage='new-static-enrollment'
        require(not STATE.exists(),'new-static-account-state')
        service.private(BASE/'account.conf','u '+ACCOUNT+' - "owned virtual gateway" /nonexistent /usr/sbin/nologin\n')
        try:pwd.getpwnam(ACCOUNT)
        except KeyError:pass
        else:require(False,'new-static-account')
        command(['systemd-sysusers',BASE/'account.conf'],'create-static-account')
        configuration(pwd.getpwnam(ACCOUNT))
        service.private(BASE/'volume.key',os.urandom(32))
        with open(BASE/'volume.img','xb',opener=lambda path,flags:os.open(path,flags,0o600)) as output:
            output.truncate(256*1024*1024);output.flush();os.fsync(output.fileno())
    stage='persistent-static-enrollment'
    user=pwd.getpwnam(ACCOUNT)
    require(user.pw_uid>0 and STATE.is_dir() and STATE.stat().st_uid==user.pw_uid,'persistent-static-enrollment')
    shutil.copytree(BASE/'conf',CONF)
    # Qualify this binary against the guest's actual ABI on every independent
    # kernel boot, before any production proxy process can be admitted.
    stage='qualify-guest-proxy'
    import service_boot_proxy
    receipt,approval=service_boot_proxy.execute(ROOT/'bin/nginx',ROOT/'boundary.json',
        protected(BASE/'proxy-parent.json',1024*1024),protected(BASE/'proxy-review.json',1024*1024),
        plan['head'],plan['proxy_inputs'],plan['proxy_libraries'])
    service.private(review/'review.json',approval)
    service.private(review/'qualification.json',service_boot_proxy.encoded(receipt))
    command(proxy_pair.install_arguments(ROOT/'bin/installer',review,ROOT/'proxy-artifact'),'install-owned-proxy-pair')
    protect_proxy_store()
    atomic(BASE/('proxy-'+case+'.json'),receipt)
    command(['modprobe','loop'],'loop-module');command(['modprobe','dm_crypt'],'crypt-module')
    config=parse(protected(BASE/'conf/config.json',65536))
    if case=='provisioned':
        command(['cryptsetup','luksFormat','--batch-mode','--type','luks2','--uuid',config['activity']['protection']['luks_uuid'],
            '--pbkdf','pbkdf2','--pbkdf-force-iterations','1000','--key-file',BASE/'volume.key',BASE/'volume.img'],'new-encrypted-volume')
    # The locked case deliberately executes cryptsetup with a missing test key.
    # It must fail before a mapper or writable plain directory is available.
    command(['cryptsetup','open','--key-file',BASE/'volume.key',BASE/'volume.img',b.MAPPING],'unlock-test-volume')
    if case=='provisioned':
        command(['mkfs.ext4','-q',DEVICE],'format-test-filesystem')
        initialize_storage(user)
    atomic(BASE/('prepared-'+case+'.json'),dict(case=case,nonce=plan['nonce'],account_uid=user.pw_uid,account_gid=user.pw_gid))

def requestor():
    tokens=parse(protected(BASE/'conf/tokens.json',65536));ports=parse(protected(BASE/'conf/ports.json',4096))
    tls=ssl.create_default_context(cafile=str(BASE/'conf/tls/fullchain.pem'))
    def request(path,role='owner',method='GET',body=None,internal=False):
        headers={'Content-Type':'application/json'}
        if role:headers['Authorization']='Bearer '+tokens[role]
        data=json.dumps(body).encode() if body is not None else None
        origin=('http://127.0.0.1:'+str(ports['auth']) if internal else 'https://127.0.0.1:'+str(ports['https']))
        req=urllib.request.Request(origin+path,data=data,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req,context=tls,timeout=5) as response:code=response.status;raw=response.read(32*1024*1024+1)
        except urllib.error.HTTPError as response:code=response.code;raw=response.read(32*1024*1024+1)
        require(len(raw)<=32*1024*1024,'bounded-guest-response');return code,raw
    return tokens,request

def ready(request):
    try:
        code,raw=request('/internal/viewers/viewer_reference/revocation',None,internal=True)
        health,body=request('/healthz',None)
        return code==200 and json.loads(raw)['media_control_ready'] is True and health==200 and json.loads(body)=={'status':'ok'}
    except urllib.error.URLError as error:
        if isinstance(error.reason,ssl.SSLError):raise
        return False
    except (OSError,TimeoutError):return False

def cleanup():
    command(['systemctl','stop',b.TARGET,*ROLES,b.MOUNT,b.PREFIX+'-monitor-stop.service'],'stop-owned-group',seconds=90)
    until(joined,'join-owned-cgroups',45)
    require(not os.path.ismount(VOLUME),'joined-recording-mount')
    require(not os.path.lexists(BASE/'initialize-volume'),'joined-private-initialization-storage')
    if DEVICE.exists():command(['cryptsetup','close',b.MAPPING],'close-owned-mapping')
    require(not DEVICE.exists(),'joined-recording-mapping')
    # cryptsetup can allocate an owned loop automatically; retire only exact
    # associations to this private image, never unrelated guest devices.
    rows=command(['losetup','-j',BASE/'volume.img'],'owned-loop-associations').splitlines()
    require(len(rows)<=1,'one-owned-volume-association')
    for row in rows:
        match=re.fullmatch(rb'(/dev/loop[0-9]+): [^\r\n]+ \('+re.escape(os.fsencode(BASE/'volume.img'))+rb'\)',row)
        require(match is not None,'exact-owned-volume-association')
        command(['losetup','--detach',match[1].decode()],'detach-owned-loop')
    require(not command(['losetup','-j',BASE/'volume.img'],'joined-owned-loop').strip(),'joined-loop-association')
    require(VOLUME.stat().st_mode&0o777==0 and not (VOLUME/'activity').exists(),'no-plaintext-underlying-directory')
    retire_proxy_store()
    retire_runtime()

def check(case,plan):
    global stage,reported_locations
    reported_locations=[]
    stage='boot-ledger'
    index=CASES.index(case);ledger=BASE/'ledger.json';prior=None
    if index==0:require(not ledger.exists(),'first-boot-ledger')
    else:
        prior=parse(protected(ledger,4096))
        require(set(prior)=={'nonce','sequence','boot_id_sha256','machine_id_sha256','clip_sha256','denial_sha256'}
                and prior['nonce']==plan['nonce'] and prior['sequence']==index,'prior-boot-generation')
    stage='boot-identities'
    boot=Path('/proc/sys/kernel/random/boot_id').read_bytes().strip();machine=Path('/etc/machine-id').read_bytes().strip()
    require(re.fullmatch(rb'[a-f0-9-]{36}',boot) and re.fullmatch(rb'[a-f0-9]{32}',machine),'actual-boot-identities')
    require(prior is None or (sha(boot)!=prior['boot_id_sha256'] and sha(machine)==prior['machine_id_sha256']),'fresh-kernel-same-machine')
    provider=state_of(b.PROVIDER);target=activation(b.TARGET)
    if case!='locked' and provider['ActiveState']=='failed':
        failure=preparation_failure(parse(protected(BASE/('prepare-failure-'+case+'.json'),4096)))
        reported_locations=failure['locations']
        stage=failure['phase'];require(False,'guest-preparation-refused')
    # Both normal and locked boots need actual XML execution before unlock.
    xml_receipt=xml.validate(parse(protected(BASE/('xml-'+case+'.json'),4096)),plan)
    require(current_xml(plan)==xml_receipt['proof'],'same-checker-xml-image')
    # Inspect automatic startup before issuing any application start operation.
    if case=='locked':
        failure=preparation_failure(parse(protected(BASE/('prepare-failure-'+case+'.json'),4096)))
        reported_locations=failure['locations']
        require(failure['phase']=='unlock-test-volume' and
                type(failure['command_exit']) is int and failure['command_exit'] in (1,2),'actual-missing-key-unlock-refusal')
        require(provider['ActiveState']=='failed' and provider['Result']=='exit-code' and target['ActiveState']!='active'
                and not DEVICE.exists() and not (BASE/('prepared-'+case+'.json')).exists(),'automatic-locked-boot-refusal')
        until(joined,'locked-boot-joined-group',45)
        require(not os.path.ismount(VOLUME) and VOLUME.stat().st_mode&0o777==0 and not (VOLUME/'activity').exists(),
                'locked-boot-no-plain-storage')
        event=parse(protected(BASE/'retained.json',4096))
        require(digest(STATE/'revocations.json')==prior['denial_sha256']==event['denial_sha256'],'locked-boot-denial-retained')
        require(not (BASE/'volume.key').exists() and len(protected(BASE/'held-key',32))==32,'withheld-test-key')
        # This named fixture restores its own synthetic key for the next fresh
        # boot. It does not unlock/restart the refused application in this boot.
        os.rename(BASE/'held-key',BASE/'volume.key')
        clip_hash=event['clip_sha256'];denial=event['denial_sha256'];clip_bytes=event['clip_bytes'];frames=event['decoded_frames']
    else:
        stage='request-credentials'
        request_tokens,request=requestor();base='/v1/cameras/cam_reference/activity'
        require(provider['ActiveState']=='active' and target['ActiveState']=='active' and os.path.ismount(VOLUME),
                'actual-automatic-group-start')
        until(lambda:ready(request),'https-management-ready',60)
        for role in (None,'publisher','recorder'):require(request(base,role)[0]==401,'recording-role-boundary')
        if index==0:
            # Readiness is proven before the synthetic publisher is started.
            command(['systemctl','start',SOURCE],'admit-synthetic-source')
            def recorded():
                code,raw=request_with_busy_retry(lambda:request(base),'GET')
                return code==200 and json.loads(raw)['events']
            until(recorded,'automatic-encrypted-recording',100)
            code,raw=request_with_busy_retry(lambda:request(base),'GET');require(code==200,'recorded-event-page')
            event=json.loads(raw)['events'][0];clip_path=base+'/'+event['event_id']+'/clip'
            stage='recorded-clip-read'
            code,clip=request_with_busy_retry(lambda:request(clip_path),'GET')
            require(code==200,'recorded-clip-response')
            require(len(clip)==event['clip_bytes'],'actual-recorded-clip')
            clip_hash=sha(clip);clip_bytes=len(clip);service.private(ROOT/'download.mp4',clip)
            command(['ffmpeg','-nostdin','-v','error','-i',ROOT/'download.mp4','-f','null','-'],'decode-encrypted-clip',seconds=60)
            raw=command(['ffprobe','-v','error','-select_streams','v:0','-count_frames','-show_entries','stream=nb_read_frames',
                '-of','csv=p=0',ROOT/'download.mp4'],'count-decoded-frames',seconds=60)
            require(re.fullmatch(rb'[0-9]{1,5}\s*',raw) and 12<=int(raw)<=10000,'actual-decoded-frame-count');frames=int(raw)
            seen=False
            for unit in ROLES:
                group=state_of(unit)['ControlGroup'];require(group=='/system.slice/'+unit,'owned-application-cgroup')
                for file in (Path('/sys/fs/cgroup')/group.lstrip('/')).rglob('cgroup.procs'):
                    for pid in map(int,file.read_text().split()):
                        value=credential_free(pid,request_tokens.values());require(value is not False,'credential-metadata-boundary');seen=seen or value is True
            require(seen,'live-credential-metadata-observation')
            require(request('/internal/viewers/viewer_reference/revoke',None,'POST',{},True)[0]==202,'durable-viewer-denial')
            require(request('/v1/cameras','viewer')[0]==403,'revoked-viewer-refusal')
            denial=digest(STATE/'revocations.json')
            event=dict(event_id=event['event_id'],clip_sha256=clip_hash,clip_bytes=clip_bytes,decoded_frames=frames,denial_sha256=denial)
            atomic(BASE/'retained.json',event)
        else:
            event=parse(protected(BASE/'retained.json',4096));clip_hash=event['clip_sha256'];clip_bytes=event['clip_bytes'];frames=event['decoded_frames'];denial=event['denial_sha256']
            require(clip_hash==prior['clip_sha256'] and denial==prior['denial_sha256']==digest(STATE/'revocations.json'),'durable-prior-boot-state')
            require(request('/v1/cameras','viewer')[0]==403 and request('/v1/cameras','owner')[0]==200,'persistent-denial-and-healthy-owner')
            stage='retained-clip-read'
            code,clip=request_with_busy_retry(lambda:request(base+'/'+event['event_id']+'/clip'),'GET')
            require(code==200,'retained-clip-response')
            require(len(clip)==clip_bytes and sha(clip)==clip_hash,'exact-prior-boot-recorded-bytes')
        cleanup()
        if case=='rebooted':
            require(not (BASE/'held-key').exists(),'new-withheld-test-key')
            os.rename(BASE/'volume.key',BASE/'held-key')
    # Even locked boots must show the exact executed guest proxy ABI categories.
    proxy=parse(protected(BASE/('proxy-'+case+'.json'),16384))
    require(proxy['guest_abi_qualified'] is True,'guest-proxy-boundaries-before-admission')
    if case=='locked':cleanup()
    require(current_xml(plan)==xml_receipt['proof'],'unchanged-guest-xml-after-cleanup')
    current=dict(nonce=plan['nonce'],sequence=index+1,boot_id_sha256=sha(boot),machine_id_sha256=sha(machine),
        clip_sha256=clip_hash,denial_sha256=denial)
    atomic(ledger,current)
    report=dict(schema=2,scope='owned-generated-gateway-service-boot',case=case,sequence=index+1,nonce=plan['nonce'],
        plan_sha256=sha(b.encoded(plan)+b'\n'),boot_id_sha256=sha(boot),machine_id_sha256=sha(machine),
        previous_boot_id_sha256=None if prior is None else prior['boot_id_sha256'],kernel_release=os.uname().release,
        pid1_sha256=plan['pid1_sha256'],network_devices=['lo'],automatic_start_qualified=case!='locked',
        locked_boot_refused=case=='locked',
        unlock_refusal=dict(operation='cryptsetup-open',key_file_withheld=True,exit_code=failure['command_exit']) if case=='locked' else None,
        clip_sha256=clip_hash,clip_bytes=clip_bytes,decoded_frames=frames,denial_sha256=denial,
        guest_proxy_abi=proxy,guest_xml_abi=xml_receipt,all_cgroups_joined=True,mount_mapping_loop_joined=True,no_plaintext_fallback=True,
        runtime_bind_readonly_same_images=True,runtime_bind_joined=True,
        proxy_store_exec_readonly=True,proxy_store_joined=True,
        ledger_committed=True,production_key_custody_qualified=False,device_tls_qualified=False,vpn_qualified=False,
        power_loss_qualified=False,physical_qualified=False,verified_boot_chain_qualified=False,
        protected_rollback_qualified=False,fips_validated=False,deployment_permitted=False)
    emit(report)

def emit(report):
    with open('/dev/console','w') as output:output.write('SERVICE_BOOT_REPORT '+b.encoded(report).decode()+'\n');output.flush()

def failure_diagnostics(error):
    locations=b.public_failure_locations(reported_locations or public_locations(error))
    def retain(caught):
        # Keep only the same bounded source-coordinate contract. Failed
        # diagnostic queries must never erase the original startup failure.
        for item in public_locations(caught):
            if item not in locations and len(locations)<8:locations.append(item)
        b.public_failure_locations(locations)
    try:activation_rows=activation_snapshot()
    except Exception as caught:retain(caught);activation_rows=[]
    try:job_rows=job_snapshot()
    except Exception as caught:
        retain(caught)
        # An unavailable canonical identity query must not discard the fixed
        # owned-unit history that was available before alias resolution.
        try:job_rows=job_snapshot(canonical=False)
        except Exception as fallback:retain(fallback);job_rows=[]
    return locations,activation_rows,job_rows

def main():
    # A personal-host invocation cannot reach any native command, mutation or
    # poweroff path. The checker independently proves the exact same context.
    case,plan=context();mode=sys.argv[1] if len(sys.argv)==2 else ''
    require(mode in ('prepare','check'),'explicit-guest-role')
    if mode=='prepare':
        try:prepare(case,plan)
        except Exception as error:
            public=failure_phase(error)
            code=last_command_exit if type(last_command_exit) is int and -128<=last_command_exit<=255 else None
            atomic(BASE/('prepare-failure-'+case+'.json'),dict(phase=public,command_exit=code,locations=public_locations(error)))
            sys.exit(2)
        return
    try:check(case,plan)
    except Exception as error:
        # Fixed phases and source coordinates carry no messages, locals,
        # arguments or private path values. Raw output stays in the image.
        public=stage if re.fullmatch('[a-z][a-z-]{0,63}',stage) else 'unclassified'
        locations,activation_rows,job_rows=failure_diagnostics(error)
        try:prerequisites=prerequisite_snapshot()
        except Exception:prerequisites=None  # Preserve the original failure.
        with open('/dev/console','w') as output:
            if locations:output.write('SERVICE_BOOT_LOCATIONS '+b.encoded(locations).decode()+'\n')
            if activation_rows:output.write('SERVICE_BOOT_ACTIVATION '+b.encoded(activation_rows).decode()+'\n')
            if job_rows:output.write('SERVICE_BOOT_JOBS '+b.encoded(job_rows).decode()+'\n')
            if prerequisites:output.write('SERVICE_BOOT_PREREQUISITES '+b.encoded(prerequisites).decode()+'\n')
            output.write('SERVICE_BOOT_FAILURE '+public+'\n');output.flush()
    finally:os.sync();os.execv('/usr/bin/systemctl',['systemctl','poweroff'])

if __name__=='__main__':main()
