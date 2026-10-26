#!/usr/bin/env python3
"""Owned hosted PID 1 experiment: initialization must not consume a mount job.

This minimal tmpfs graph qualifies the ordering mechanism, not gateway boot,
encrypted storage, device TLS, production key custody or physical power loss.
"""
import argparse,hashlib,json,os,re,secrets,shutil,stat,subprocess,sys,time
from pathlib import Path
import service_lifetime as lifetime
import failure_location
ROOT=Path(__file__).resolve().parents[2]
helper_admitted=False
INPUTS=('gateway/tests/service_boot_mount_reference.py','gateway/tests/service_lifetime.py',
    'gateway/tests/service_boot_guest.py','gateway/tests/service_boot_units.py',
    'tests/host/test_service_boot_mount_reference.py','.github/workflows/gateway-service-boot-order.yml',
    'gateway/tests/failure_location.py')
FLAGS=('full_gateway_boot_qualified','encrypted_storage_qualified','device_tls_qualified',
    'production_key_custody_qualified','power_loss_qualified','physical_qualified','fips_validated','deployment_permitted')

def require(value,category):
    if not value:raise ValueError(category)
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def inputs():return {name:digest(ROOT/name) for name in INPUTS}
def locations(error):
    base=Path(__file__).resolve().parent
    return failure_location.trace_locations(error,{str(base/name):'gateway/tests/'+public for name,public in
        (('service_boot_mount_reference.py','service_boot_mount_reference.py'),('provider.py','service_boot_mount_reference.py'),
         ('service_lifetime.py','service_lifetime.py'))})
def hosted():
    require(sys.platform=='linux' and os.geteuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true' and
        os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted','disposable-hosted-root-required')
    release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    require(release.get('ID','').strip('"')=='ubuntu' and release.get('VERSION_ID','').strip('"')=='24.04' and
        Path('/proc/1/comm').read_text().strip()=='systemd' and Path('/sys/fs/cgroup/cgroup.controllers').is_file(),'actual-noble-pid1-required')
def command(args,*,accepted=(0,),seconds=12):
    result=subprocess.run([str(x) for x in args],capture_output=True,timeout=seconds,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'})
    require(result.returncode in accepted and len(result.stdout)+len(result.stderr)<=65536,'owned-native-operation-refused')
    return result.returncode,result.stdout
def until(check):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        if check():return
        time.sleep(.05)
    require(False,'owned-state-observation-deadline')
def active(unit):
    _,raw=command(['systemctl','show',unit,'-p','ActiveState','--value'])
    value=raw.decode('ascii').strip();require(value in ('active','inactive','activating','deactivating','failed','reloading'),'fixed-active-state')
    return value=='active'
def mount_name(path):
    _,raw=command(['systemd-escape','--path','--suffix=mount',path]);value=raw.decode('ascii').strip()
    require(re.fullmatch(r'[A-Za-z0-9_.\\-]{1,240}\.mount',value),'fixed-owned-mount-name');return value
def owned_root(root):
    require(root.is_absolute() and root.parent==Path('/run') and re.fullmatch('nxrs-bm-[a-f0-9]{16}',root.name),'owned-mount-experiment-root')
    info=root.lstat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and info.st_mode&0o077==0,'private-owned-mount-experiment')
def provider(root,mode):
    global helper_admitted
    hosted();owned_root(root)
    require(mode in ('managed','private') and Path(__file__).resolve()==root/'provider.py','explicit-owned-provider-role')
    expected=json.dumps(dict(schema=1,mode=mode),sort_keys=True).encode()
    with open(root/'owned.json','rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        info=os.fstat(source.fileno());require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and info.st_mode&0o077==0 and info.st_size<=128,'owned-provider-marker')
        require(source.read(129)==expected,'same-owned-provider-marker')
    helper_admitted=True
    point=root/('volume' if mode=='managed' else 'initialize');managed=mount_name(root/'volume');temporary=mount_name(point)
    try:
        command(['mount','-t','tmpfs','-o','size=1048576,mode=0700,nosuid,nodev,noexec','tmpfs',point])
        # Observe a real manager transition, not a timing guess. The legacy
        # case deterministically exposes the interleaving in unit_process_job.
        until(lambda:active(temporary))
        _,job=command(['systemctl','show',managed,'-p','Job','--value'])
        pending=job.decode('ascii').strip() not in ('','0')
        observed=dict(managed_active_before_provider_return=active(managed),managed_job_pending=pending)
        with open(root/'observed.json','xb',opener=lambda p,f:os.open(p,f,0o600)) as output:output.write(json.dumps(observed).encode())
    finally:
        if os.path.ismount(point):command(['umount',point])
        require(not os.path.ismount(point),'provider-initialization-mount-joined')
    until(lambda:not active(temporary))

def units(root):
    prefix=root.name;provider_unit=prefix+'-provider.service';consumer=prefix+'-consumer.service';mount=mount_name(root/'volume')
    helper=root/'provider.py';mode=json.loads((root/'owned.json').read_bytes())['mode']
    provider_text=('[Unit]\nDefaultDependencies=no\n[Service]\nType=oneshot\nRemainAfterExit=yes\n'
        'Environment=GITHUB_ACTIONS=true RUNNER_ENVIRONMENT=github-hosted\n'
        'ExecStart=/usr/bin/python3 -B '+str(helper)+' provider '+str(root)+' '+mode+'\n'
        'TimeoutStartSec=30\nTimeoutStopSec=10\nKillMode=control-group\nStandardOutput=null\nStandardError=null\n')
    mount_text=('[Unit]\nRequires='+provider_unit+'\nAfter='+provider_unit+'\n'
        '[Mount]\nWhat=tmpfs\nWhere='+str(root/'volume')+'\nType=tmpfs\nOptions=size=1048576,mode=0700,nosuid,nodev,noexec\nTimeoutSec=10\n')
    consumer_text=('[Unit]\nRequires='+provider_unit+'\nAfter='+provider_unit+' '+mount+'\nBindsTo='+mount+'\n'
        '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\nTimeoutStartSec=30\nTimeoutStopSec=10\nKillMode=control-group\n')
    return {provider_unit:provider_text,mount:mount_text,consumer:consumer_text},provider_unit,mount,consumer

def run_case(mode):
    hosted();require(mode in ('managed','private'),'named-owned-mount-case')
    root=Path('/run')/('nxrs-bm-'+secrets.token_hex(8));root.mkdir(mode=0o700);owned_root(root)
    written=[];registered=False;record=None
    try:
        for name in ('volume','initialize'):(root/name).mkdir(mode=0o700)
        (root/'owned.json').write_bytes(json.dumps(dict(schema=1,mode=mode),sort_keys=True).encode());(root/'owned.json').chmod(0o600)
        shutil.copyfile(Path(__file__),root/'provider.py')
        for name in ('service_lifetime.py','failure_location.py'):shutil.copyfile(ROOT/'gateway/tests'/name,root/name)
        require(digest(root/'provider.py')==digest(Path(__file__)),'same-owned-helper-source')
        rendered,provider_unit,mount,consumer=units(root)
        for name,raw in rendered.items():
            path=Path('/run/systemd/system')/name
            with open(path,'x') as output:output.write(raw)
            written.append(path)
        command(['systemctl','daemon-reload']);registered=True
        code,_=command(['systemctl','start',consumer],accepted=(0,1),seconds=40)
        if (root/'failure.json').is_file():
            raw=(root/'failure.json').read_bytes();require(len(raw)<=4096,'bounded-owned-provider-failure')
            rows=json.loads(raw)
            accepted={'gateway/tests/service_boot_mount_reference.py','gateway/tests/service_lifetime.py'}
            require(failure_location.project_locations(rows,accepted)==rows,'fixed-owned-provider-failure')
            print('SERVICE_BOOT_ORDER_HELPER_LOCATIONS '+json.dumps(rows,sort_keys=True,separators=(',',':')),flush=True)
            require(False,'owned-provider-refused')
        raw=(root/'observed.json').read_bytes();require(len(raw)<=1024,'bounded-owned-transition-record');observed=json.loads(raw)
        require(set(observed)=={'managed_active_before_provider_return','managed_job_pending'} and
            all(type(v) is bool for v in observed.values()),'fixed-owned-transition-record')
        require(active(provider_unit),'actual-provider-completed')
        require(code==(1 if mode=='managed' else 0) and active(consumer) is (mode=='private') and active(mount) is (mode=='private'),'actual-causal-startup-result')
        require(observed==dict(managed_active_before_provider_return=mode=='managed',managed_job_pending=mode=='private'),'observed-pending-job-consumption')
        record=dict(case=mode,start_exit=code,**observed,consumer_active=mode=='private',managed_mount_active=mode=='private')
    finally:
        if registered:
            command(['systemctl','stop',consumer,mount,provider_unit],seconds=30)
            for unit in (provider_unit,consumer):
                def joined():
                    _,raw=command(['systemctl','show',unit,'--all',*lifetime.PROPERTY_ARGS])
                    return lifetime.quiescent(lifetime.parse(raw),unit)
                until(joined)
        for name in ('volume','initialize'):
            point=root/name
            if os.path.ismount(point):command(['umount',point])
            require(not os.path.ismount(point),'all-owned-experiment-mounts-joined')
        for path in reversed(written):path.unlink()
        if written:command(['systemctl','daemon-reload'])
        shutil.rmtree(root)
    require(record is not None,'complete-owned-ordering-case');record['owned_cleanup_joined']=True;return record

def validate(value):
    require(set(value)=={'schema','scope','qualified','checkout','inputs','manager_sha256','cases',*FLAGS} and type(value['schema']) is int and value['schema']==1 and
        value['scope']=='hosted-pid1-initialization-mount-order' and value['qualified'] is True,'complete-mount-order-receipt')
    require(set(value['checkout'])=={'commit','clean'} and value['checkout']['clean'] is True and re.fullmatch('[a-f0-9]{40}',value['checkout']['commit']) and value['inputs']==inputs() and
        re.fullmatch('[a-f0-9]{64}',value['manager_sha256']) and all(value[k] is False for k in FLAGS),'bound-mount-order-scope')
    expected=[dict(case=mode,start_exit=1 if mode=='managed' else 0,managed_active_before_provider_return=mode=='managed',
        managed_job_pending=mode=='private',consumer_active=mode=='private',managed_mount_active=mode=='private',owned_cleanup_joined=True)
        for mode in ('managed','private')]
    require(value['cases']==expected and all(type(row['start_exit']) is int and all(type(row[k]) is bool for k in
        ('managed_active_before_provider_return','managed_job_pending','consumer_active','managed_mount_active','owned_cleanup_joined'))
        for row in value['cases']),'actual-old-fail-private-pass-and-joined-cases');return value
def run(report):
    hosted()
    require(report.is_absolute() and report.parent==Path(os.environ.get('RUNNER_TEMP','')) and not os.path.lexists(report),'owned-new-ordering-report')
    _,raw=command(['git','-c','safe.directory='+str(ROOT),'-C',ROOT,'rev-parse','HEAD']);head=raw.decode().strip()
    _,raw=command(['git','-c','safe.directory='+str(ROOT),'-C',ROOT,'status','--porcelain']);require(not raw.strip(),'clean-owned-ordering-source')
    before=inputs();cases=[run_case(mode) for mode in ('managed','private')]
    require(before==inputs(),'unchanged-owned-ordering-source')
    result=dict(schema=1,scope='hosted-pid1-initialization-mount-order',qualified=True,checkout=dict(commit=head,clean=True),
        inputs=before,manager_sha256=digest('/proc/1/exe'),cases=cases,**dict.fromkeys(FLAGS,False));validate(result)
    raw=(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n').encode();require(len(raw)<=16384,'bounded-public-ordering-receipt')
    with open(report,'xb') as output:output.write(raw)
    report.chmod(0o644)
    print('PASS: actual PID 1 consumes the legacy initialization mount job; private initialization preserves startup; owned resources joined')
def main():
    if len(sys.argv)==4 and sys.argv[1]=='provider':
        try:provider(Path(sys.argv[2]),sys.argv[3])
        except Exception as error:
            # A staged helper can export only fixed public source coordinates.
            # Its private native output and arguments never leave the owned root.
            rows=locations(error);target=Path(sys.argv[2])/'failure.json'
            if helper_admitted and rows:
                with open(target,'xb',opener=lambda p,f:os.open(p,f,0o600)) as output:output.write(json.dumps(rows).encode())
            sys.exit(2)
        return
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',type=Path,required=True)
    try:run(parser.parse_args().report)
    except Exception as error:
        rows=locations(error)
        if rows:print('SERVICE_BOOT_ORDER_LOCATIONS '+json.dumps(rows,sort_keys=True,separators=(',',':')),flush=True)
        parser.exit(1,'Owned mount ordering qualification refused.\n')
if __name__=='__main__':main()
