#!/usr/bin/env python3
"""Hosted-root qualification of actual reviewed-pair publication and recovery."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import sys
import threading
import time
import proxy_pair as pair
import service_lifetime as lifetime

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
sys.path.insert(0,str(ROOT/'gateway/tools'))
import candidate
import gateway_proxy_evidence as evidence
native=candidate.ubuntu
SCOPE='hosted-linux-reviewed-proxy-pair-process-recovery'
CASES=('partial-stage','before-selection','after-selection','late-sync-error')
LIMITS=('physical_power_loss_qualified','protected_rollback_qualified','release_approval_qualified',
        'full_native_advisory_closure','physical_qualified','fips_validated','deployment_permitted')
VARIANT=b'\nNXRS_OWNED_PAIR_FIXTURE_NONLOADED_TRAILER_V1\n'

def require(value,category):
    if not value:raise ValueError(category)

def digest(path):return hashlib.sha256(pair.read(Path(path),64*1024*1024)).hexdigest()

def executable_digest(path,owner):
    # Cargo build outputs and distribution programs can have hardlink aliases.
    # They are executable images, separate from private approval/selection
    # files, whose single-link admission is unchanged.
    require(type(owner) is int and owner>=0,'explicit-fixture-image-owner')
    with open(path,'rb',opener=lambda name,flags:os.open(name,flags|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        before=os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid==owner and before.st_mode&0o022==0 and
            before.st_mode&0o111!=0 and before.st_size<=64*1024*1024,'protected-bounded-fixture-image')
        value=hashlib.sha256();total=0
        while block:=source.read(1024*1024):
            total+=len(block);require(total<=64*1024*1024,'bounded-fixture-image-read');value.update(block)
        after=os.fstat(source.fileno())
        fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_gid','st_nlink')
        require(total==before.st_size and all(getattr(before,key)==getattr(after,key) for key in fields),
            'unchanged-fixture-image-read')
    return value.hexdigest()

def debugger_digest(path):return executable_digest(path,0)

def program_images(args):
    owner=ROOT.stat().st_uid
    expected=dict(launcher=ROOT/'gateway/target/debug/nxrs-gateway-proxy',
                  installer=ROOT/'gateway/target/debug/nxrs-gateway-proxy-install')
    require(all(getattr(args,name)==path for name,path in expected.items()),'exact-built-pair-programs')
    return {name:executable_digest(path,owner) for name,path in expected.items()}

def public_failure_locations(error):
    import failure_location
    names=('gateway/tests/proxy_pair_reference.py','gateway/tests/proxy_pair.py','gateway/tests/service_lifetime.py')
    sources={key:name for name in names for key in (name,str(ROOT/name))}
    return failure_location.trace_locations(error,sources)

def hosted():
    native.hosted()
    require(os.geteuid()==0 and platform.machine()=='x86_64','hosted-linux-amd64-root-required')
    require(Path('/proc/1/comm').read_text().strip()=='systemd' and Path('/sys/fs/cgroup/cgroup.controllers').is_file(),'pair-systemd-cgroup-required')

def graph(review):
    raw=pair.read(review/'review.json',8192);q=pair.read(review/'qualification.json',1024*1024)
    value=json.loads(raw)
    require(hashlib.sha256(q).hexdigest()==value['qualification_sha256'] and digest(review/'nginx')==value['binary_sha256'],'same-pair-review-graph')
    return dict(approval_sha256=hashlib.sha256(raw).hexdigest(),qualification_sha256=hashlib.sha256(q).hexdigest(),binary_sha256=value['binary_sha256'])

class Work:
    def __init__(self,root):
        self.root=root;self.units=set();self.observers=set();self.deadline=time.monotonic()+600
        spec=importlib.util.spec_from_file_location('owned_pair_process',ROOT/'gateway/fuzz/run.py')
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin',LANG='C',LC_ALL='C',HOME=str(root),PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
    def command(self,args,accepted=(0,),seconds=30,environment=None,phase='command'):
        require(re.fullmatch('[a-z][a-z-]{0,63}',phase) is not None,'fixed-pair-command-phase')
        require(time.monotonic()<self.deadline,'pair-global-deadline')
        # Thread-owned subdirectories avoid racing the command/log sequence.
        folder=self.root/('command-'+secrets.token_hex(8));folder.mkdir(mode=0o700)
        status,raw=self.runner.execute(args,folder,'private',self.env if environment is None else environment,
            min(seconds,max(1,self.deadline-time.monotonic())),cwd=self.root)
        require(status in accepted,'owned-pair-'+phase+'-status')
        return status,raw
    def retire(self,unit):
        self.command(['/usr/bin/systemctl','stop',unit],accepted=(0,5),seconds=20,phase='debugger-stop')
        _,raw=self.command(['/usr/bin/systemctl','show',unit,*lifetime.PROPERTY_ARGS],accepted=(0,1),phase='debugger-state')
        state=lifetime.parse(raw)
        absent=state['LoadState']=='not-found' and state['ActiveState']=='inactive' and state['MainPID']==state['ControlPID']=='0' and state['Job'] in ('','0')
        require(absent or lifetime.quiescent(state,unit),'owned-debugger-unit-quiescent')
        group=Path('/sys/fs/cgroup/system.slice')/unit
        if group.exists():
            files=list(group.rglob('cgroup.procs'));require(1<=len(files)<=256 and all(not p.read_text().strip() for p in files),'owned-debugger-cgroup-empty')
        self.command(['/usr/bin/systemctl','reset-failed',unit],accepted=(0,1),phase='debugger-reset')
        self.units.remove(unit)
    def debug(self,command,environment,case):
        unit=self.root.name+'-'+case+'.service'
        _,raw=self.command(['/usr/bin/systemctl','show',unit,'-p','LoadState','--value'],accepted=(0,1),phase='debugger-admission')
        require(raw.strip()==b'not-found','new-owned-debugger-unit')
        self.units.add(unit)
        options=['/usr/bin/systemd-run','--quiet','--wait','--pipe','--service-type=exec','--unit='+unit,
            '--property=KillMode=control-group','--property=RuntimeMaxSec=40','--property=TimeoutStopSec=10',
            '--property=SendSIGKILL=yes','--property=Description=nxrs owned pair interruption fixture']
        options+=['--setenv='+name+'='+value for name,value in environment.items()]
        try:return self.command([*options,*command],seconds=55,phase='debugger-'+case)
        finally:self.retire(unit)
    def close(self):
        # Cleanup gets its own bounded window even if the work deadline expired.
        self.deadline=max(self.deadline,time.monotonic()+60)
        for thread,stop in list(self.observers):
            stop.set();thread.join(timeout=35)
            require(not thread.is_alive(),'owned-reader-retired-before-removal')
            self.observers.remove((thread,stop))
        for unit in list(self.units):self.retire(unit)

def selector(store):return pair.selected_version(store).name

def new_store(root,name):
    store=root/name;store.mkdir(mode=0o755);return store

def install(work,installer,review,store,expected='none',accepted=(0,),phase='install'):
    return work.command(pair.install_arguments(installer,review,store,expected),accepted,phase=phase)

def launcher_arguments(launcher,store,config,runtime,selection='active.json'):
    return [launcher,'--selection',store/selection,'--config',config,'--runtime',runtime,'--check']

def adapter(root,launcher,store,runtime):
    # The shared four-category corpus expects nginx's -t/-c CLI. This owned
    # fixture translates only those two forms to the actual Rust launcher.
    source=('#!/usr/bin/python3\nimport os,sys\na=sys.argv[1:]\n'
        "assert (len(a)==3 and a[:2]==['-t','-c']) or (len(a)==2 and a[0]=='-c')\n"
        'v='+repr([str(launcher),'--selection',str(store/'active.json'),'--runtime',str(runtime),'--config'])+'+[a[-1]]\n'
        "if a[0]=='-t':v+=['--check']\nos.execv(v[0],v)\n")
    output=root/'boundary-adapter';output.write_text(source);output.chmod(0o755)
    return output

def boundaries(work,binary,output):
    env={**work.env,'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted'}
    work.command(['/usr/bin/python3',ROOT/'tools/gateway-proxy/boundary.py','--nginx',binary,'--report',output],seconds=90,environment=env,phase='https-boundary')
    value=json.loads(pair.read(output,4096))
    require(value==dict(tests=4,failures=0,skips=0,log_discard_qualified=True,log_configuration_refusals=2),'complete-pair-https-boundaries')
    return value

def variant(work,root,review,parent):
    destination=root/'review-b';destination.mkdir(mode=0o700)
    binary=pair.read(review/'nginx',64*1024*1024)
    require(binary.startswith(b'\x7fELF') and len(binary)+len(VARIANT)<=64*1024*1024,'bounded-elf-fixture-variant')
    (destination/'nginx').write_bytes(binary+VARIANT);(destination/'nginx').chmod(0o755)
    executed=boundaries(work,destination/'nginx',root/'variant-boundaries.json')
    proof=dict(schema=1,scope='owned-nonloaded-trailer-proxy-pair-fixture',parent=parent,
        binary_sha256=digest(destination/'nginx'),trailer_sha256=hashlib.sha256(VARIANT).hexdigest(),
        transformation='append-fixed-nonloaded-fixture-trailer',https_boundaries=executed,
        publisher_signature_covers_fixture_trailer=False,**{key:False for key in LIMITS})
    raw=(json.dumps(proof,sort_keys=True)+'\n').encode()
    (destination/'qualification.json').write_bytes(raw);(destination/'qualification.json').chmod(0o600)
    approval=json.loads(pair.read(review/'review.json',8192));approval.update(binary_sha256=proof['binary_sha256'],qualification_sha256=hashlib.sha256(raw).hexdigest())
    (destination/'review.json').write_text(json.dumps(approval,sort_keys=True)+'\n');(destination/'review.json').chmod(0o600)
    return destination

def debugger_failure_locations(raw):
    import failure_location
    value=json.loads(raw)
    require(isinstance(value,dict) and set(value)=={'schema','scope','locations'} and
        type(value['schema']) is int and value['schema']==1 and value['scope']=='owned-pair-debugger-failure',
        'bounded-pair-debugger-failure-schema')
    locations=value['locations']
    require(isinstance(locations,list) and 1<=len(locations)<=8,'bounded-pair-debugger-locations')
    for item in locations:
        require(isinstance(item,dict) and set(item)=={'source','line','error'} and
            failure_location.project_locations([item],['gateway/tests/proxy_pair_gdb.py'])==[item],
            'public-pair-debugger-location')
    require(len({(item['source'],item['line'],item['error']) for item in locations})==len(locations),
        'distinct-pair-debugger-locations')
    return locations

def interrupt(work,root,installer,review,store,expected,case):
    target=graph(review)['approval_sha256'];marker=root/(case+'-observed.json')
    plan=root/(case+'-plan.json')
    arguments=pair.install_arguments(installer,review,store,expected)
    plan.write_text(json.dumps(dict(store=str(store),target=target,case=case,marker=str(marker),contender=arguments)));plan.chmod(0o600)
    command=['/usr/bin/gdb','-nx','-nh','-batch','-iex','set auto-load safe-path /nonexistent',
        '-x',ROOT/'gateway/tests/proxy_pair_gdb.py','--args',*pair.install_arguments(installer,review,store,expected)]
    try:
        _,raw=work.debug(command,{**work.env,'NXRS_PAIR_DEBUG_PLAN':str(plan),
            'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted'},case)
    except Exception:
        # Work.debug first joins the whole transient unit in its finally block.
        # A bounded private sidecar can identify code without exporting logs.
        failure=marker.with_suffix('.failure.json')
        if failure.exists():
            locations=debugger_failure_locations(pair.read(failure,4096))
            print('PAIR_DEBUG_LOCATIONS '+json.dumps(locations,sort_keys=True,separators=(',',':')),flush=True)
        raise
    value=json.loads(pair.read(marker,4096))
    require(value['case']==case and value['fsync_entry_observed'] is True and value['joined'] is True,'actual-owned-installer-stop')
    if case=='late-sync-error':
        require(value['exit_code']==1 and value['fsync_completed_before_error_injection'] is True and b'selection-visible-durability-uncertain' in raw,'actual-visible-uncertain-category')
    if case=='partial-stage':require(value['stage_members']==['approval.json'],'actual-incomplete-pair-interruption')
    if case=='before-selection':require(value['concurrent_writer_refused'] is True,'actual-simultaneous-writer-refusal')
    require(value['selected_target']==(case in ('after-selection','late-sync-error')),'actual-publication-boundary')
    return value

@contextmanager
def observe(work,launcher,store,config,runtime):
    stop=threading.Event();errors=[];observations=[];started=threading.Event()
    def reader():
        try:
            while not stop.is_set() and len(observations)<128:
                status,_=work.command(launcher_arguments(launcher,store,config,runtime),accepted=(0,1),phase='concurrent-reader')
                observations.append(status);started.set()
        except BaseException as error:errors.append(type(error).__name__);started.set()
    thread=threading.Thread(target=reader);work.observers.add((thread,stop));thread.start()
    try:
        require(started.wait(15) and not errors,'actual-pair-reader-started')
        yield observations
    finally:
        stop.set();thread.join(timeout=35)
        require(not thread.is_alive(),'owned-pair-reader-joined')
        work.observers.remove((thread,stop))
        require(not errors,'owned-pair-reader-errors')

def validate(value,head,inputs):
    require(type(value['schema']) is int and value['schema']==1 and value['scope']==SCOPE and value['qualified'] is True
            and value['checkout']==dict(commit=head,clean=True) and value['inputs']==inputs,'current-complete-pair-receipt')
    require(set(value['pairs'])=={'original','fixture_variant'} and value['pairs']['original']['binary_sha256']!=value['pairs']['fixture_variant']['binary_sha256'],'two-distinct-actual-pair-images')
    for entry in value['pairs'].values():
        require(set(entry)=={'approval_sha256','qualification_sha256','binary_sha256'} and all(re.fullmatch('[a-f0-9]{64}',v) for v in entry.values()),'complete-pair-hashes')
    require(set(value['interruptions'])==set(CASES) and all(v is True for v in value['interruptions'].values()),'all-installer-interruptions')
    require(all(type(value[k]) is int for k in ('recovered_pairs','switches','reader_successes','reader_refusals','https_tests','https_skips')),'typed-pair-counts')
    require(value['recovered_pairs']==4 and value['switches']==16 and value['reader_successes']>=1 and value['reader_refusals']>=0
            and value['reader_successes']+value['reader_refusals']<=128 and value['reader_overlapped_switches'] is True
            and value['concurrent_writer_refused'] is True,'actual-pair-switches-and-readers')
    require(value['refusals']==dict(stale=1,changed_version=1,changed_selector=1,missing_selector=1,symlink_selector=1,hardlink_selector=1,writable_selector=1)
            and all(type(v) is int for v in value['refusals'].values()),'actual-pair-refusal-cases')
    require(value['https_tests']==12 and value['https_skips']==0 and value['retained_original_pair'] is True and value['retained_original_launcher_checked'] is True and value['cleanup_joined'] is True,'actual-pair-https-and-cleanup')
    require(set(value['executables'])=={'launcher','installer','gdb'} and all(re.fullmatch('[a-f0-9]{64}',v) for v in value['executables'].values()),'actual-pair-programs')
    require(all(value[k] is False for k in LIMITS),'pair-qualification-limits')
    return value

def validate_documents(value,documents):
    require(set(documents)=={'original-review','original-qualification','fixture-variant-review','fixture-variant-qualification'},'complete-pair-public-documents')
    policies={}
    for label,key in [('original','original'),('fixture-variant','fixture_variant')]:
        raw=documents[label+'-review'];proof=documents[label+'-qualification'];review=json.loads(raw)
        require(len(raw)<=8192 and len(proof)<=1024*1024,'bounded-pair-public-documents')
        require(value['pairs'][key]==dict(approval_sha256=hashlib.sha256(raw).hexdigest(),
            qualification_sha256=hashlib.sha256(proof).hexdigest(),binary_sha256=review['binary_sha256'])
            and review['qualification_sha256']==hashlib.sha256(proof).hexdigest(),'exact-pair-public-documents')
        policies[label]=review
    expected={**policies['original'],'binary_sha256':policies['fixture-variant']['binary_sha256'],
              'qualification_sha256':policies['fixture-variant']['qualification_sha256']}
    require(expected==policies['fixture-variant'],'same-pair-policy-except-owned-variant')
    variant=json.loads(documents['fixture-variant-qualification'])
    require(variant['scope']=='owned-nonloaded-trailer-proxy-pair-fixture' and variant['parent']==value['pairs']['original']
        and variant['binary_sha256']==value['pairs']['fixture_variant']['binary_sha256']
        and variant['trailer_sha256']==hashlib.sha256(VARIANT).hexdigest()
        and variant['transformation']=='append-fixed-nonloaded-fixture-trailer'
        and variant['publisher_signature_covers_fixture_trailer'] is False and all(variant[k] is False for k in LIMITS),'explicit-fixture-variant-scope')
    require(variant['https_boundaries']==dict(tests=4,failures=0,skips=0,log_discard_qualified=True,log_configuration_refusals=2),'variant-actual-https-boundaries')

def run(args):
    hosted() # No argument-dependent files, commands or output before admission.
    root=Path('/run')/('nxrs-pair-'+secrets.token_hex(4));root.mkdir(mode=0o755)
    work=Work(root);head=None;before=None;value=None;documents={}
    try:
        def git(*parts):return work.command(['/usr/bin/git','-c','safe.directory='+str(ROOT),'-C',ROOT,*parts],phase='git-source')[1].decode().strip()
        head=git('rev-parse','HEAD');require(not git('status','--porcelain'),'clean-pair-source');before=candidate.source_inputs()
        require(not os.path.lexists(args.report),'new-pair-report')
        debugger_image=debugger_digest(Path('/usr/bin/gdb'))
        programs=program_images(args)
        proof=evidence.verify(ROOT,head,before);parent=graph(args.review)
        require(parent['qualification_sha256']==proof['qualification_sha256'] and parent['binary_sha256']==proof['binary_sha256'],'same-actual-qualified-parent-pair')
        review_b=variant(work,root,args.review,parent);second=graph(review_b)
        for label,review in [('original',args.review),('fixture-variant',review_b)]:
            for name,maximum in [('review',8192),('qualification',1024*1024)]:
                documents[label+'-'+name]=pair.read(review/(name+'.json'),maximum)
        require(parent['approval_sha256']!=second['approval_sha256'],'distinct-pair-approval')
        runtime=root/'runtime';runtime.mkdir(mode=0o700);config=root/'check.conf'
        config.write_text('daemon off;\nmaster_process off;\nerror_log /dev/null emerg;\npid '+str(runtime/'nginx.pid')+';\nevents {}\nhttp { access_log off; }\n')
        observed={};recovered=0
        for index,case in enumerate(CASES):
            store=new_store(root,'crash-'+str(index));install(work,args.installer,args.review,store,phase=case+'-initial-install')
            old=pair.selected_version(store);old_bytes=(old/'nginx').read_bytes()
            interrupt(work,root,args.installer,review_b,store,parent['approval_sha256'],case)
            expected=second['approval_sha256'] if case in ('after-selection','late-sync-error') else parent['approval_sha256']
            require(selector(store)==expected,'correct-visible-pair-after-interruption')
            work.command(launcher_arguments(args.launcher,store,config,runtime),phase=case+'-visible-launch')
            install(work,args.installer,review_b,store,parent['approval_sha256'],phase=case+'-recover-install')
            require(selector(store)==second['approval_sha256'] and (old/'nginx').read_bytes()==old_bytes,'recovered-complete-pair-and-retained-reader-image')
            install(work,args.installer,review_b,store,parent['approval_sha256'],phase=case+'-retry-install')
            recovered+=1;observed[case]=True
        store=new_store(root,'live');install(work,args.installer,args.review,store,phase='live-initial-install');old=pair.selected_version(store)
        (store/'retained.json').write_bytes((store/'active.json').read_bytes());(store/'retained.json').chmod(0o644)
        adapter_path=adapter(root,args.launcher,store,runtime)
        boundaries(work,adapter_path,root/'selected-a-boundaries.json')
        with observe(work,args.launcher,store,config,runtime) as reads:
            started_reads=len(reads)
            current=parent['approval_sha256']
            for i in range(16):
                review,target=(review_b,second['approval_sha256']) if i%2==0 else (args.review,parent['approval_sha256'])
                install(work,args.installer,review,store,current,phase='concurrent-switch');require(selector(store)==target,'complete-pair-selection');current=target
            overlapped=len(reads)>started_reads
        require(any(status==0 for status in reads),'actual-concurrent-reader-success')
        require(overlapped,'actual-reader-overlap-with-switches')
        install(work,args.installer,review_b,store,current,phase='live-final-install')
        boundaries(work,adapter_path,root/'selected-b-boundaries.json')
        # A reader's previously captured selector still resolves the retained
        # original pair after active.json selects the other actual image.
        require(json.loads((store/'retained.json').read_bytes())['approval_sha256']==parent['approval_sha256'],'retained-original-selection')
        work.command(launcher_arguments(args.launcher,store,config,runtime,'retained.json'),phase='retained-launch')
        refusals={}
        def refusal(name,call):
            status,_=call();require(status!=0,'actual-pair-refusal-'+name);refusals[name]=1
        refusal('stale',lambda:install(work,args.installer,args.review,store,parent['approval_sha256'],(1,),phase='stale-refusal'))
        require(selector(store)==second['approval_sha256'],'stale-writer-preserves-selection')
        selected=pair.selected_version(store);image=selected/'nginx';original=image.read_bytes();image.write_bytes(original+b'changed')
        try:
            refusal('changed_version',lambda:install(work,args.installer,review_b,store,second['approval_sha256'],(1,),phase='changed-version-refusal'))
            work.command(launcher_arguments(args.launcher,store,config,runtime),accepted=(1,),phase='changed-version-launch')
        finally:image.write_bytes(original)
        active=store/'active.json';raw=active.read_bytes()
        for name in ('changed_selector','missing_selector','symlink_selector','hardlink_selector','writable_selector'):
            try:
                if name=='changed_selector':active.write_bytes(b'{}')
                elif name=='missing_selector':active.unlink()
                elif name=='symlink_selector':active.unlink();active.symlink_to(old/'approval.json')
                elif name=='hardlink_selector':os.link(active,store/'alias')
                else:active.chmod(0o666)
                refusal(name,lambda:work.command(launcher_arguments(args.launcher,store,config,runtime),accepted=(1,),phase='selector-refusal'))
            finally:
                if os.path.lexists(store/'alias'):(store/'alias').unlink()
                if os.path.lexists(active):active.unlink()
                active.write_bytes(raw);active.chmod(0o644)
        install(work,args.installer,review_b,store,second['approval_sha256'],phase='recovered-install')
        work.command(launcher_arguments(args.launcher,store,config,runtime),phase='recovered-launch')
        require(digest(old/'nginx')==parent['binary_sha256'],'original-pair-retained')
        require(before==candidate.source_inputs() and head==git('rev-parse','HEAD') and not git('status','--porcelain'),'unchanged-pair-source')
        require(debugger_digest(Path('/usr/bin/gdb'))==debugger_image,'unchanged-executed-debugger-image')
        require(program_images(args)==programs,'unchanged-executed-pair-images')
        value=dict(schema=1,scope=SCOPE,qualified=True,checkout=dict(commit=head,clean=True),inputs=before,
            pairs=dict(original=parent,fixture_variant=second),interruptions=observed,recovered_pairs=recovered,switches=16,
            reader_successes=reads.count(0),reader_refusals=reads.count(1),refusals=refusals,https_tests=12,https_skips=0,
            reader_overlapped_switches=overlapped,concurrent_writer_refused=True,
            retained_original_pair=True,retained_original_launcher_checked=True,cleanup_joined=False,executables=dict(**programs,gdb=debugger_image),
            **{key:False for key in LIMITS})
    finally:
        # Every command group and observer has already joined before removal.
        work.close();shutil.rmtree(root)
    require(value is not None and not root.exists(),'owned-pair-cleanup-complete');value['cleanup_joined']=True
    validate(value,head,before)
    validate_documents(value,documents)
    raw=(json.dumps(value,sort_keys=True)+'\n').encode();require(len(raw)<=256*1024,'bounded-public-pair-report')
    for name,document in documents.items():
        with open(args.report.with_name(args.report.stem+'-'+name+'.json'),'xb') as output:output.write(document)
    with open(args.report,'xb') as output:output.write(raw)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('review','launcher','installer','report'):parser.add_argument('--'+name,type=Path,required=True)
    try:run(parser.parse_args())
    except Exception as error:
        locations=public_failure_locations(error)
        if locations:print('PAIR_FAILURE_LOCATIONS '+json.dumps(locations,sort_keys=True,separators=(',',':')),flush=True)
        category=str(error) if isinstance(error,ValueError) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Owned pair qualification refused ('+category+').\n')

if __name__=='__main__':main()
