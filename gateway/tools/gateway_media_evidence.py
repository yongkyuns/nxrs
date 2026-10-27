"""Read-only binding of minimized native media evidence to exact runtime resources."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

ROOT=Path(__file__).resolve().parents[2]
INPUTS=('tools/gateway-media/prepare.py','tools/gateway-media/qualify.py','tools/gateway-media/dependencies.json',
        'tools/gateway-media/README.md','tests/host/test_gateway_media.py','.github/workflows/gateway-media-runtime.yml',
        'security/native-media-dependencies.json','tools/native-media/qualify.py','gateway/fuzz/run.py',
        'tools/native-packages/ubuntu.py','tools/ci-apt.py','gateway/tools/gateway_media_evidence.py',
        'gateway/tests/test_gateway_media_evidence.py','.github/workflows/monitoring-ingest.yml',
        '.github/workflows/encrypted-activity.yml','tools/gateway-media/x264_cleanup.py',
        'tools/gateway-media/patches/x264-open-cleanup.patch','tests/host/test_x264_cleanup.py',
        'tests/host/native/x264_open_cleanup.c')


def require(value,category):
    if not value:raise ValueError(category)


def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def parse(raw):
    def unique(pairs):
        value={}
        for key,item in pairs:
            require(key not in value,'duplicate-native-evidence-key');value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=unique)


def read(path):
    with open(path,'rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode),'regular-native-evidence')
        raw=source.read(65537)
    require(len(raw)<=65536,'bounded-native-evidence');return raw,parse(raw)


def canonical(name):
    path=PurePosixPath(name)
    require(path.parts and not path.is_absolute() and '..' not in path.parts and path.as_posix()==name,'canonical-native-evidence-name')
    return name


def hashes(value):
    require(isinstance(value,dict) and 1<=len(value)<=512,'bounded-native-hashes')
    for name,sha in value.items():
        canonical(name);require(isinstance(sha,str) and re.fullmatch('[0-9a-f]{64}',sha),'native-evidence-sha256')
    return value


def source_names(repository):
    names=set(INPUTS)|{'gateway/Cargo.toml','gateway/Cargo.lock','.github/workflows/monitoring-ingest.yml',
        'gateway/tests/reader_reference.py','gateway/tests/build_publisher_witness.py',
        'gateway/tests/native/publisher_no_decoder.c','gateway/tests/native/publisher_decoder_control.c',
        'gateway/tests/qualify_media_sandbox.py','gateway/tests/media_sandbox/probe.c','tests/device-identity/frame.jpg'}
    for crate in ['ingest','media-process','media-sandbox','private-file']:
        names.add('gateway/'+crate+'/Cargo.toml')
        names.update(str(p.relative_to(repository)) for p in (repository/'gateway'/crate/'src').rglob('*.rs'))
    names.add('service/detection-wire/Cargo.toml')
    names.update(str(p.relative_to(repository)) for p in (repository/'service/detection-wire/src').rglob('*.rs'))
    return names


def check_payloads(runtime,build):
    runtime=Path(runtime).resolve();files=hashes(build['runtime_files']);links=build['runtime_links']
    require(isinstance(links,dict) and len(links)<=512,'bounded-native-links')
    actual_files=set();actual_links={}
    for path in runtime.rglob('*'):
        name=str(path.relative_to(runtime))
        if path.is_symlink():actual_links[name]=os.readlink(path)
        elif path.is_file() and name not in ['build.json','qualification.json']:actual_files.add(name)
    require(actual_files==set(files) and actual_links==links,'complete-unchanged-native-payload-inventory')
    for name,target in links.items():
        canonical(name)
        require(isinstance(target,str) and 0<len(target)<=512 and (runtime/name).resolve().is_relative_to(runtime)
            and (runtime/name).is_file(),'owned-native-library-link')
    require(all(digest(runtime/n)==h for n,h in files.items()),'unchanged-native-runtime-payloads')
    return files


def verify(repository,runtime,*,ffmpeg,publisher,reader):
    repository=Path(repository);runtime=Path(runtime).resolve()
    build_raw,build=read(runtime/'build.json');qualification_raw,q=read(runtime/'qualification.json')
    require(build.get('schema')==1 and build.get('scope')=='hosted-minimized-gateway-media-build-not-release-admission'
        and build.get('qualified') is False and build.get('build_complete') is True,'complete-native-build-required')
    require(q.get('schema')==1 and q.get('scope')=='actual-minimized-host-gateway-media-sdk-isolation-and-clip'
        and q.get('qualified') is True and q.get('unchanged_isolation_grants') is True
        and q.get('native_decoder_open_observer_executed') is True
        and q.get('decoded_clip_frames')==96,'executed-native-runtime-required')
    closed=['physical_qualified','fips_validated','deployment_permitted','upstream_advisory_closure',
            'full_native_advisory_closure','optimized_assembly_fuzz_qualified','complete_native_parser_coverage']
    require(all(value.get(flag) is False for value in [build,q] for flag in closed),'native-runtime-scope-required')
    checkout=q['checkout'];require(checkout==build['checkout'] and checkout.get('clean') is True
        and isinstance(checkout.get('commit'),str) and re.fullmatch('[0-9a-f]{40}',checkout['commit']),'same-clean-native-runtime-checkout')
    require(q['build_sha256']==hashlib.sha256(build_raw).hexdigest(),'native-build-qualification-join')
    require(set(hashes(build['inputs']))==set(INPUTS) and set(hashes(q['inputs']))==source_names(repository),
            'complete-native-source-inventory')
    require(all(q['inputs'][n]==h for n,h in build['inputs'].items()),'same-native-source-inventory')
    require(all(digest(repository/n)==h for n,h in q['inputs'].items()),'current-native-source-required')
    recipe=parse((repository/'tools/gateway-media/dependencies.json').read_bytes())
    ff=parse((repository/'security/native-media-dependencies.json').read_bytes())['ffmpeg']
    require(build['sources']==dict(ffmpeg=ff,x264=recipe['x264']),'reviewed-native-source-pins')
    require(build.get('x264_cleanup')==dict(applied=True,**recipe['x264']['cleanup']),'executed-x264-cleanup-required')
    require(build['resolved']['selected']['decoder']==['h264','mjpeg']
        and build['resolved']['selected']['encoder']==['libx264','mjpeg','rawvideo']
        and all(build['resolved']['selected'][n]==[] for n in ['hwaccel','indev','outdev']), 'bounded-native-codecs')
    files=check_payloads(runtime,build)
    for name,root in build['roots'].items():
        require(name in ['ffmpeg','ffprobe'] and root['sha256']==files['bin/'+name],'native-runtime-root-identity')
        for name,sha in root['libraries'].items():
            if name.startswith('/'):
                require(Path(name)==Path(name).resolve() and name.startswith('/usr/lib/') and Path(name).name in ['ld-linux-x86-64.so.2','libc.so.6','libm.so.6','libgcc_s.so.1','libpthread.so.0','libdl.so.2','librt.so.1'],'native-system-library-scope')
                require(digest(Path(name))==sha,'unchanged-native-system-library')
            else:
                canonical(name);require(files[name]==sha,'native-runtime-library-identity')
    require(set(build['roots'])=={'ffmpeg','ffprobe'},'complete-native-root-set')
    require(digest(Path(ffmpeg))==files['bin/ffmpeg'],'same-production-media-executable')
    sdk={}
    for name,path in [('publisher',publisher),('reader',reader)]:
        value=q['sdk'][name]
        require(value['sha256']==digest(Path(path)),'same-production-sdk-executable')
        require(len(value['libraries'])==4 and all(files['lib/'+canonical(n)]==h for n,h in value['libraries'].items()),
                'same-production-sdk-libraries')
        sdk[name]=value['sha256']
    result=dict(schema=1,scope='qualified-host-minimized-media-runtime',qualified=True,checkout=checkout,
        build_sha256=hashlib.sha256(build_raw).hexdigest(),qualification_sha256=hashlib.sha256(qualification_raw).hexdigest(),
        isolation_sha256=q['isolation_sha256'],ffmpeg_sha256=files['bin/ffmpeg'],ffprobe_sha256=files['bin/ffprobe'],sdk=sdk,
        runtime_payloads_sha256=hashlib.sha256(encoded(dict(files=files,links=build['runtime_links']))).hexdigest(),inputs_sha256=hashlib.sha256(encoded(q['inputs'])).hexdigest(),
        upstream_advisory_closure=False,full_native_advisory_closure=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)

    validate(result)
    return result


def validate(value):
    require(value.get('schema')==1 and value.get('scope')=='qualified-host-minimized-media-runtime'
        and value.get('qualified') is True,'qualified-native-runtime-proof')
    require(all(value.get(flag) is False for flag in ['upstream_advisory_closure','full_native_advisory_closure',
        'physical_qualified','fips_validated','deployment_permitted']),'native-runtime-proof-scope')
    checkout=value['checkout']
    require(checkout.get('clean') is True and isinstance(checkout.get('commit'),str)
        and re.fullmatch('[0-9a-f]{40}',checkout['commit']),'clean-native-runtime-proof')
    fields=['build_sha256','qualification_sha256','isolation_sha256','ffmpeg_sha256','ffprobe_sha256',
            'runtime_payloads_sha256','inputs_sha256']
    require(all(isinstance(value.get(n),str) and re.fullmatch('[0-9a-f]{64}',value[n]) for n in fields)
        and set(value['sdk'])=={'publisher','reader'}
        and all(isinstance(h,str) and re.fullmatch('[0-9a-f]{64}',h) for h in value['sdk'].values()),'complete-native-runtime-proof')
