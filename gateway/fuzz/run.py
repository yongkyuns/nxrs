#!/usr/bin/env python3
"""Owned synthetic parser fuzzing; export only bounded source/binary/counter evidence."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import selectors
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
TARGETS={'mjpeg':524289,'enrollment':65536,'recording_csv':1025,'recording_event':4097}
NIGHTLY='nightly-2026-03-01'


def require(value):
    if not value: raise ValueError('parser fuzz qualification refused')


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def execute(command,work,phase,environment,seconds):
    require(hasattr(os,'waitid') or hasattr(select,'kqueue'))
    process=subprocess.Popen([str(x) for x in command],cwd=ROOT,env=environment,start_new_session=True,
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    tail=bytearray();total=0;deadline=time.monotonic()+seconds
    exit_queue=None;initial_events=[];terminal=False
    def exited():
        if hasattr(os,'waitid'):
            return os.waitid(os.P_PID,process.pid,os.WEXITED|os.WNOWAIT|os.WNOHANG) is not None
        if exit_queue is None:return False
        events=initial_events[:] if initial_events else exit_queue.control(None,1,0)
        initial_events.clear()
        for event in events:
            require(event.ident==process.pid)
            if event.flags & select.KQ_EV_ERROR:require(event.data==errno.ESRCH)
            else:require(bool(event.fflags & select.KQ_NOTE_EXIT))
        return bool(events)
    try:
        if not hasattr(os,'waitid'):
            # Darwin exposes kqueue process-exit notification, not waitid in
            # Python. Neither mechanism reaps the reserved group leader PID.
            exit_queue=select.kqueue()
            event=select.kevent(process.pid,filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD|select.KQ_EV_ONESHOT,fflags=select.KQ_NOTE_EXIT)
            try:initial_events=exit_queue.control([event],1,0)
            except ProcessLookupError:terminal=True
        with selectors.DefaultSelector() as ready, (work/(phase+'.log')).open('xb') as log:
            ready.register(process.stdout,selectors.EVENT_READ)
            while ready.get_map():
                require(time.monotonic()<deadline)
                for key,_ in ready.select(min(1,max(0,deadline-time.monotonic()))):
                    chunk=os.read(key.fd,8192)
                    if not chunk:ready.unregister(key.fileobj);continue
                    total+=len(chunk);require(total<=1024*1024)
                    log.write(chunk);tail.extend(chunk)
                    if len(tail)>65536:del tail[:-65536]
        # Keep the exited leader unreaped until group cleanup: its PID cannot
        # be recycled into an unrelated process group between wait and killpg.
        while not terminal:
            terminal=exited()
            if not terminal:require(time.monotonic()<deadline);time.sleep(.01)
    finally:
        if process.stdout:process.stdout.close()
        if not terminal:
            try:terminal=exited()
            except (OSError,ValueError):pass
        try:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            except PermissionError:
                # Darwin can report EPERM for the already exited zombie-only
                # group. Accept only after kernel exit notification, never for
                # a live child whose termination is unproven.
                if not terminal:raise
        finally:
            if exit_queue:exit_queue.close()
            status=process.wait(timeout=3)
    return status,bytes(tail)


def statistics(raw):
    fields={}
    for key,output in [('number_of_executed_units','executed_units'),('peak_rss_mb','peak_rss_mb')]:
        values=re.findall(rb'^stat::'+key.encode()+rb':\s*([0-9]{1,12})\s*$',raw,re.M)
        require(bool(values));fields[output]=int(values[-1])
    completed=re.findall(rb'^#[0-9]+\s+DONE\s+cov:\s*([0-9]{1,12})\s+ft:\s*([0-9]{1,12})',raw,re.M)
    require(bool(completed));fields['coverage_edges'],fields['features']=map(int,completed[-1])
    require(1000<=fields['executed_units']<=100000 and fields['coverage_edges']>0 and fields['features']>0
            and 0<fields['peak_rss_mb']<=1024)
    return fields


def inputs():
    names={'gateway/Cargo.toml','gateway/Cargo.lock','gateway/fuzz/Cargo.toml','gateway/fuzz/Cargo.lock',
        'gateway/fuzz/run.py','.github/workflows/security.yml','security/tools.json','tools/check-security.py'}
    for crate in ['ingest','activity','control','private-file','media-process','media-sandbox']:
        names.add('gateway/'+crate+'/Cargo.toml')
        names.update(str(p.relative_to(ROOT)) for p in (ROOT/'gateway'/crate/'src').rglob('*') if p.is_file())
    for child in ['fuzz_targets','seeds']:
        names.update(str(p.relative_to(ROOT)) for p in (ROOT/'gateway/fuzz'/child).rglob('*') if p.is_file())
    return {name:digest(ROOT/name) for name in sorted(names)}


def run(target,work):
    require(sys.platform in ('linux','darwin') and platform.machine() in ('x86_64','arm64','aarch64'))
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True,timeout=10).strip()
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,timeout=10).strip())
    before=inputs();require(not work.exists());work.mkdir(mode=0o700)
    for name in ['corpus','artifacts']:(work/name).mkdir(mode=0o700)
    environment=dict(os.environ)
    for key in ['RUSTFLAGS','RUSTDOCFLAGS','CARGO_ENCODED_RUSTFLAGS','RUSTC_WRAPPER',
                'RUSTC_WORKSPACE_WRAPPER','CARGO_TARGET_DIR','CARGO_BUILD_TARGET','RUSTC_BOOTSTRAP']:
        environment.pop(key,None)
    environment.update(CARGO_NET_OFFLINE='true',ASAN_OPTIONS='detect_leaks=0:abort_on_error=1:symbolize=0')
    cargo=shutil.which('cargo');fuzz=shutil.which('cargo-fuzz');require(cargo and fuzz)
    code,raw=execute([fuzz,'--version'],work,'version',environment,10)
    require(code==0 and raw.strip()==b'cargo-fuzz 0.13.2')
    compiler=subprocess.check_output(['rustc','+'+NIGHTLY,'-vV'],env=environment,cwd=ROOT,text=True,timeout=10)
    host=re.search(r'^host: ([a-z0-9_-]+)$',compiler,re.M).group(1)
    for name in ['asan_probe',target]:
        print('PARSER_FUZZ_PHASE build-'+name,flush=True)
        code,_=execute([cargo,'+'+NIGHTLY,'fuzz','build','--fuzz-dir','gateway/fuzz',
            '--sanitizer','address',name],work,'build-'+name,environment,600)
        require(code==0)
    binaries={name:ROOT/'gateway/fuzz/target'/host/'release'/name for name in ['asan_probe',target]}
    require(all(p.is_file() for p in binaries.values()))
    executed_paths={**binaries,'cargo_fuzz':Path(fuzz)}
    executable_hashes={name:digest(path) for name,path in executed_paths.items()}
    print('PARSER_FUZZ_PHASE sanitizer-positive-control',flush=True)
    code,raw=execute([binaries['asan_probe'],'-runs=1','-seed=1','-timeout=5',
        '-artifact_prefix='+str(work/'artifacts')+'/'],work,'sanitizer-probe',environment,15)
    require(code!=0 and b'ERROR: AddressSanitizer: heap-use-after-free' in raw)
    print('PARSER_FUZZ_PHASE '+target,flush=True)
    code,raw=execute([binaries[target],work/'corpus',ROOT/'gateway/fuzz/seeds'/target,
        '-runs=100000','-seed=1','-max_total_time=60','-timeout=5','-rss_limit_mb=1024',
        '-max_len='+str(TARGETS[target]),'-print_final_stats=1',
        '-artifact_prefix='+str(work/'artifacts')+'/'],work,'fuzz-'+target,environment,80)
    require(code==0);counts=statistics(raw)
    require(executable_hashes=={name:digest(path) for name,path in executed_paths.items()})
    require(before==inputs() and not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,timeout=10).strip())
    report=dict(schema=1,scope='actual-asan-coverage-guided-gateway-parser-fuzzing',qualified=True,
        target=target,checkout=dict(clean=True,commit=head),inputs=before,counts=counts,
        maximum_input_bytes=TARGETS[target],maximum_seconds=60,maximum_rss_mb=1024,
        address_sanitizer_positive_control=True,cargo_fuzz_version='0.13.2',nightly=NIGHTLY,
        rustc_version=compiler.strip(),host_machine=platform.machine(),
        executables=executable_hashes,
        native_codec_parsers_qualified=False,device_runtime_qualified=False,physical_qualified=False,
        fips_validated=False,deployment_permitted=False,private_inputs_used=False)
    raw=(json.dumps(report,sort_keys=True,indent=2)+'\n').encode();require(len(raw)<=65536)
    (work/'qualification.json').write_bytes(raw)
    print('PASS: bounded actual parser fuzzing, real ASan positive control and exact committed input/binary evidence',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--target',choices=TARGETS,required=True)
    p.add_argument('--work',type=Path,required=True);a=p.parse_args()
    try:run(a.target,a.work.resolve())
    except Exception:p.exit(1,'Parser fuzz qualification refused; private logs/artifacts remain local and no success report was produced.\n')
