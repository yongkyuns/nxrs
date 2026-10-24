#!/usr/bin/env python3
"""Reproduce and audit one MediaMTX artifact; never install a service or use keys."""
import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import tarfile
import tempfile
import zipfile

# These consumers intentionally use the same Go scanner JSON protocol, bounded
# public download/cache and owned-process helpers. Restic policy is not reused.
from backup_engine import (Failure, Findings, advisory_report, archive, bounded,
                           command, digest, download, json_new, records, require)

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT/'deploy/dependencies.json'
DATABASE = 'https://vuln.go.dev'
MODULE = 'github.com/bluenviron/mediamtx'
INPUTS = ('gateway/tools/media_engine.py', 'gateway/tools/backup_engine.py',
          'gateway/tools/verify_media_evidence.py', 'gateway/deploy/dependencies.json',
          'gateway/deploy/mediamtx/go.mod', 'gateway/deploy/mediamtx/go.sum')
TESTS = ('TestAuthInternal', 'TestAuthHTTP', 'TestAuthJWT', 'TestAuthJWTJWKSFetch',
         'TestAuthJWTRefresh', 'TestCredential', 'TestConfDeprecatedAuth', 'TestRedact')


def source_inputs():
    return {name:digest(bounded(ROOT.parent/name,1024*1024)) for name in INPUTS}


def host_target():
    return {'Darwin':'darwin','Linux':'linux'}.get(platform.system(),'')+'_'+{
        'arm64':'arm64','aarch64':'arm64','x86_64':'amd64','AMD64':'amd64'}.get(platform.machine(),'')


def unpack_source(data, target, prefix, *, maximum_bytes=128*1024*1024, maximum_members=10000):
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as source:
        members=source.getmembers();seen=set()
        require(0<len(members)<=maximum_members and sum(max(0,m.size) for m in members)<=maximum_bytes,
                'source expansion limit')
        for member in members:
            name=PurePosixPath(member.name)
            require(name.parts and name.parts[0]==prefix and not name.is_absolute()
                    and '..' not in name.parts and name.as_posix()==member.name.rstrip('/')
                    and member.name not in seen and (member.isfile() or member.isdir()),
                    'source member identity')
            seen.add(member.name)
        target.mkdir(mode=0o700)
        source.extractall(target,members=members,filter='data')
    return target/prefix


def asset_bytes(data, pin):
    require(digest(data)==pin['sha256'],'embedded asset archive identity')
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        require(len(source.infolist())<=10000 and source.namelist().count(pin['entry'])==1,
                'one embedded asset required')
        entry=source.getinfo(pin['entry'])
        require(not entry.is_dir() and entry.file_size<=2*1024*1024,'embedded asset size')
        value=source.read(entry)
    require(digest(value)==pin['content_sha256'],'embedded asset content identity')
    return value


def affected_version(value, version):
    def numeric(text):
        if text=='0':return (0,0,0)
        require(isinstance(text,str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',text),
                'reviewed release version required')
        return tuple(map(int,text.split('.')))
    selected=numeric(version);affected=False
    require(value.get('id') and isinstance(value.get('affected'),list) and value['affected'],
            'main advisory schema')
    for item in value['affected']:
        require(item.get('package')==dict(ecosystem='Go',name=MODULE) and not item.get('versions'),
                'main advisory package/range scope')
        ranges=item.get('ranges');require(isinstance(ranges,list) and ranges,'main advisory ranges')
        for interval in ranges:
            require(interval.get('type')=='SEMVER' and isinstance(interval.get('events'),list)
                    and interval['events'],'main advisory version scheme')
            start=None
            for event in interval['events']:
                require(isinstance(event,dict) and len(event)==1,'main advisory event')
                if 'introduced' in event:
                    require(start is None,'main advisory interval order');start=numeric(event['introduced'])
                elif 'fixed' in event:
                    end=numeric(event['fixed'])
                    require(start is not None and end>start,'main advisory fixed interval')
                    affected |= start<=selected<end;start=None
                else:raise Failure('unreviewed main advisory event')
            if start is not None:affected |= selected>=start
    return affected


def main_module_report(index, recipe, fetch=download, today=None):
    review=recipe['main_module_review'];today=today or datetime.now(timezone.utc).date();expires=date.fromisoformat(review['expires'])
    require(today<expires<=today+timedelta(days=90) and review.get('owner') and
            review.get('issue')=='https://github.com/yongkyuns/nxrs/issues/38','main advisory review expiry/owner')
    rows=json.loads(index)
    require(isinstance(rows,list) and rows and all(isinstance(r,dict) and isinstance(r.get('path'),str)
            and isinstance(r.get('vulns'),list) for r in rows),'module index schema')
    matching=[r for r in rows if r['path']==MODULE];require(len(matching)<=1,'duplicate main module')
    entries=matching[0]['vulns'] if matching else []
    pins=review['advisories'];require(isinstance(pins,list) and len(pins)<=32,'bounded main advisory review')
    expected={p['id']:p['sha256'] for p in pins}
    require(len(expected)==len(pins) and all(re.fullmatch(r'GO-[0-9]{4}-[0-9]+',k)
            and re.fullmatch('[0-9a-f]{64}',v) for k,v in expected.items()),'main advisory pin identities')
    require({entry.get('id') for entry in entries}==set(expected) and len(entries)==len(expected),
            'new or removed main advisory requires review')
    for identity,sha in expected.items():
        raw=fetch(DATABASE+'/ID/'+identity+'.json',262144)
        require(digest(raw)==sha,'main advisory content changed')
        value=json.loads(raw)
        require(value.get('id')==identity and not value.get('withdrawn')
                and not affected_version(value,recipe['source']['version']), 'affected upstream MediaMTX release')
    return dict(module=MODULE,version=recipe['source']['version'],module_index_sha256=digest(index),
                reviewed_advisories=expected,review_expires=review['expires'],affected=False)


def module_digest(values):
    require(isinstance(values,list) and all(isinstance(row,list) and len(row)==3
            and all(isinstance(v,str) and v for v in row) for row in values), 'module digest schema')
    return digest(json.dumps(sorted(values),separators=(',',':')).encode())


def inventory(info, recipe, target, sha):
    require(info.get('GoVersion')==recipe['go_version'] and info.get('Path')==MODULE
            and info.get('Main')==dict(Path=MODULE,Version='(devel)'), 'compiled main module/Go identity')
    settings={v['Key']:v['Value'] for v in info.get('Settings',[])}
    operating_system,architecture=target.split('_')
    require(settings.get('CGO_ENABLED')=='0' and settings.get('GOOS')==operating_system
            and settings.get('GOARCH')==architecture and settings.get('-trimpath')=='true'
            and not any(k.startswith('vcs.') for k in settings),'compiled MediaMTX settings')
    modules=info.get('Deps');require(isinstance(modules,list) and modules,'compiled module inventory')
    require(all(isinstance(m,dict) for m in modules), 'compiled module schema')
    components=[dict(type='application',name='nxrs-mediamtx',version=recipe['version'],
        **{'bom-ref':'engine'},hashes=[dict(alg='SHA-256',content=sha)]),
        dict(type='library',name='Go standard library',version=info['GoVersion'],**{'bom-ref':'stdlib'}),
        dict(type='library',name='hls.js',version=recipe['asset']['version'],**{'bom-ref':'hls-js'},
             hashes=[dict(alg='SHA-256',content=recipe['asset']['content_sha256'])])]
    seen=set()
    for module in modules:
        require(not module.get('Replace') and isinstance(module.get('Path'),str) and module['Path']
                and isinstance(module.get('Version'),str) and module['Version']
                and isinstance(module.get('Sum'),str) and module['Sum'].startswith('h1:') and module['Path'] not in seen,
                'compiled module identity')
        seen.add(module['Path']);components.append(dict(type='library',name=module['Path'],version=module['Version'],
            **{'bom-ref':'module-'+str(len(seen))},properties=[dict(name='nxrs:go:module-sum',value=module['Sum'])]))
    require(module_digest([[m.get('Path'),m.get('Version'),m.get('Sum')] for m in modules])
            ==recipe['artifacts'][target]['modules_sha256'] and len(modules)==recipe['artifacts'][target]['module_count'],
            'complete compiled module graph')
    return dict(bomFormat='CycloneDX',specVersion='1.6',version=1,components=components,
                dependencies=[dict(ref='engine',dependsOn=[c['bom-ref'] for c in components[1:]])])


def negative_binary(data, expected):
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as source:
        members=source.getmembers();require(len(members)<=16,'negative archive member count')
        matches=[m for m in members if m.name=='mediamtx']
        require(len(matches)==1 and matches[0].isfile() and matches[0].size<=64*1024*1024,
                'one bounded negative executable required')
        value=source.extractfile(matches[0]).read(64*1024*1024+1)
    require(len(value)<=64*1024*1024 and digest(value)==expected,'negative executable identity')
    return value


def build(target, output, *, cache=None, qualify=False):
    require(not any(os.path.lexists(str(output)+suffix) for suffix in ['', '.cdx.json','.advisories.json','.provenance.json']),
            'new MediaMTX outputs required')
    recipe_raw=bounded(RECIPE,65536);recipe=json.loads(recipe_raw)['mediamtx'];before=source_inputs();host=host_target()
    require(recipe['format_version']==1 and recipe['build']==dict(cgo=False,trimpath=True,buildvcs=False,symbols=True)
            and host in recipe['toolchains'] and target in recipe['artifacts'],'MediaMTX build recipe/target')
    # No missing prerequisite may fall back to the affected upstream executable.
    with tempfile.TemporaryDirectory(prefix='nxrs-media-build-') as temporary:
        work=Path(temporary).resolve();store=Path(cache).resolve() if cache else work/'downloads'
        store.mkdir(mode=0o700,parents=True,exist_ok=True)
        sdk=recipe['toolchains'][host]
        unpack_source(archive(sdk,store,150*1024*1024),work/'sdk','go',maximum_bytes=1024**3,maximum_members=50000)
        source=unpack_source(archive(recipe['source'],store,16*1024*1024),work/'source',recipe['source']['root'])
        go=work/'sdk/go/bin/go'
        for name,pin in recipe['modules'].items():
            require(name in ['go.mod','go.sum'] and pin['path']=='mediamtx/'+name,'module recipe path')
            data=bounded(ROOT/'deploy'/pin['path'],1024*1024);require(digest(data)==pin['sha256'],'frozen module identity')
            (source/name).write_bytes(data)
        asset=recipe['asset'];asset_directory=source/'internal/servers/hls'
        require((asset_directory/'hlsjsdownloader/VERSION').read_text().strip()==asset['version']
                and (asset_directory/'hlsjsdownloader/HASH').read_text().strip()==asset['sha256'],
                'upstream embedded asset pin')
        (asset_directory/'hls.min.js').write_bytes(asset_bytes(archive(asset,store,10*1024*1024),asset))
        (source/'internal/core/VERSION').write_text('v'+recipe['version'])
        env=dict(PATH=str(go.parent)+':/usr/bin:/bin',LANG='C',GOENV='off',GOTOOLCHAIN='local',
                 GOPATH=str(work/'gopath'),GOCACHE=str(work/'cache'),GOSUMDB='sum.golang.org',GOWORK='off',
                 GOPROXY='https://proxy.golang.org',GOTELEMETRY='off',GOAUTH='off',CGO_ENABLED='0',
                 GOMAXPROCS='2',TMPDIR=str(work))
        operating_system,architecture=target.split('_')
        selected=dict(env,GOOS=operating_system,GOARCH=architecture,GOAMD64='v1',GOARM64='v8.0')
        command([go,'mod','download'],cwd=source,env=selected)
        command([go,'mod','verify'],cwd=source,env=selected,timeout=120)
        binary=work/'mediamtx'
        print('MEDIA_BUILD_PHASE compile',flush=True)
        command([go,'build','-mod=readonly','-trimpath','-buildvcs=false','-o',binary,'.'],cwd=source,env=selected)
        data=bounded(binary,64*1024*1024);sha=digest(data)
        require(sha==recipe['artifacts'][target]['sha256'],'reproduced MediaMTX checksum')
        info=json.loads(command([go,'version','-m','-json',binary],cwd=source,env=env,limit=1024*1024))
        bom=inventory(info,recipe,target,sha)
        scanner=recipe['scanner'];module=scanner['module']+'@'+scanner['version']
        checked=json.loads(command([go,'mod','download','-json',module],cwd=work,env=env,limit=1024*1024))
        require(checked.get('Sum')==scanner['sum'] and checked.get('GoModSum')==scanner['go_mod_sum'],
                'reviewed Go scanner module')
        command([go,'install','-trimpath','golang.org/x/vuln/cmd/govulncheck@'+scanner['version']],cwd=work,env=env)
        checker=work/'gopath/bin/govulncheck';scans=[]
        print('MEDIA_BUILD_PHASE source-and-binary-advisories',flush=True)
        for mode,args in [('binary',['-mode','binary',binary]),('source',['.'])]:
            raw=command([checker,'-json',*args],cwd=source,env=selected,timeout=300)
            scans.append(advisory_report(raw,mode,scanner))
        compressed=download(DATABASE+'/index/modules.json.gz',8*1024*1024)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:index=stream.read(32*1024*1024+1)
        require(len(index)<=32*1024*1024,'module index expansion limit')
        main=main_module_report(index,recipe)
        controls=dict(affected_binary_refused=False,affected_advisory_ids=[],affected_binary_sha256=None,
                      upstream_auth_tests=[],target_executed=False)
        if qualify:
            print('MEDIA_BUILD_PHASE affected-binary-refusal',flush=True)
            pin=recipe['negative_control'][target]
            affected=work/'affected-control'
            affected.write_bytes(negative_binary(archive(dict(url=pin['url'],sha256=pin['archive_sha256']),store,40*1024*1024),pin['binary_sha256']))
            # Read only. The affected executable is never made executable or run.
            try:
                raw=command([checker,'-json','-mode','binary',affected],cwd=source,env=env,timeout=300)
                advisory_report(raw,'binary',scanner)
            except Findings as finding:
                require('GO-2026-6809' in finding.report['affected_ids'] and
                        'GO-2026-6603' in finding.report['affected_ids'],'actual negative advisory evidence')
                controls['affected_binary_refused']=True
                controls['affected_advisory_ids']=finding.report['affected_ids']
                controls['affected_binary_sha256']=pin['binary_sha256']
            else:raise Failure('affected MediaMTX executable admitted')
            if host==target:
                print('MEDIA_BUILD_PHASE upstream-authentication-tests',flush=True)
                raw=command([go,'test','-mod=readonly','-count=1','-json','./internal/auth','./internal/conf'],
                            cwd=source,env=env,timeout=300)
                events=[json.loads(line) for line in raw.splitlines()]
                passed={v['Test'] for v in events if v.get('Action')=='pass' and v.get('Test') in TESTS}
                require(passed==set(TESTS),'executed upstream authentication tests')
                controls['upstream_auth_tests']=sorted(passed)
                require(command([binary,'--version'],cwd=work,env=env,timeout=5,limit=4096).strip()==('v'+recipe['version']).encode(),
                        'actual MediaMTX runtime version')
                controls['target_executed']=True
        for name,pin in recipe['modules'].items():
            require(digest(bounded(source/name,1024*1024))==pin['sha256'],'build changed frozen modules')
        require(before==source_inputs(),'MediaMTX builder inputs changed')
        require(digest(bounded(binary,64*1024*1024))==sha and
                digest(bounded(asset_directory/'hls.min.js',2*1024*1024))==asset['content_sha256'],
                'qualified executable/asset unchanged')
        bom_raw=(json.dumps(bom,indent=2,sort_keys=True)+'\n').encode()
        summary=dict(scans=scans,main_module=main,scanner_module=scanner,
                     scanner_binary_sha256=digest(bounded(checker,64*1024*1024)),qualification_controls=controls,
                     source_archive_sha256=recipe['source']['sha256'],embedded_asset_sha256=asset['content_sha256'],
                     hls_javascript_advisories_qualified=False,os_advisories_qualified=False,
                     fips_validated=False,physical_qualified=False,deployment_permitted=False)
        summary_raw=(json.dumps(summary,indent=2,sort_keys=True)+'\n').encode()
        provenance={'_type':'https://in-toto.io/Statement/v1',
            'subject':[dict(name=output.name,digest=dict(sha256=sha))],
            'predicateType':'https://slsa.dev/provenance/v1','predicate':{
                'buildDefinition':dict(buildType='https://github.com/yongkyuns/nxrs/mediamtx/v1',
                    externalParameters=dict(target=target,recipe_sha256=digest(recipe_raw)),
                    internalParameters=dict(build=recipe['build'],runtime_version=recipe['version']),
                    resolvedDependencies=[dict(uri='nxrs:'+name,digest=dict(sha256=value)) for name,value in before.items()]+
                        [dict(uri=pin['url'],digest=dict(sha256=pin['sha256'])) for pin in [recipe['source'],sdk,asset]]),
                'runDetails':dict(builder=dict(id='nxrs:checksum-pinned-mediamtx-builder'),
                    metadata=dict(finishedOn=datetime.now(timezone.utc).isoformat()),
                    byproducts=[dict(name=output.name+suffix,mediaType='application/json',digest=dict(sha256=digest(value)))
                                for suffix,value in [('.cdx.json',bom_raw),('.advisories.json',summary_raw)]])}}
        for suffix,value in [('.cdx.json',bom_raw),('.advisories.json',summary_raw)]:
            with open(str(output)+suffix,'xb') as file:file.write(value)
        json_new(Path(str(output)+'.provenance.json'),provenance)
        with open(output,'xb',opener=lambda p,f:os.open(p,f,0o600)) as file:file.write(data)
        output.chmod(0o700)
        print('PASS: reproduced and audited MediaMTX',target,sha,flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact',choices=['linux_amd64','darwin_arm64','darwin_amd64'],default=host_target())
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--cache',type=Path)
    parser.add_argument('--qualify',action='store_true');args=parser.parse_args()
    try:build(args.artifact,args.output,cache=args.cache,qualify=args.qualify)
    except Failure as error:parser.exit(1,'MediaMTX build/audit refused: '+str(error)+'. No artifact is admitted.\n')
    except OSError as error:parser.exit(1,'MediaMTX build OS operation refused (errno '+str(error.errno)+').\n')
    except (ValueError,KeyError,TypeError,tarfile.TarError,zipfile.BadZipFile):
        parser.exit(1,'MediaMTX build, identity or advisory qualification refused; no artifact is admitted.\n')

if __name__=='__main__':main()
