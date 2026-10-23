#!/usr/bin/env python3
"""Build and audit one exact backup artifact; publish only NEW public output files.

Python 3.12+. No installation, signing key, credential, personal service or device
operation. Source/module/SDK/artifact identities are committed review inputs.
"""
import argparse
import bz2
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import tarfile
import tempfile
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT/'deploy/backup-dependencies.json'
TAGS = 'disable_grpc_modules'
DATABASE = 'https://vuln.go.dev'


class Failure(Exception):
    pass


class Findings(Failure):
    def __init__(self, report):
        super().__init__('native imported/called advisory gate failed')
        self.report = report


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(value, category):
    if not value:
        raise Failure(category)


def bounded(path, limit):
    with open(path, 'rb', opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as file:
        import stat
        require(stat.S_ISREG(os.fstat(file.fileno()).st_mode), 'regular public input required')
        data=file.read(limit+1)
    require(len(data)<=limit,'public input size limit')
    return data


def download(url, limit):
    require(url.startswith('https://'), 'HTTPS public input required')
    with urllib.request.urlopen(url, timeout=60) as response:
        require(response.geturl().startswith('https://'), 'HTTPS redirect required')
        data=response.read(limit+1)
    require(len(data)<=limit,'download size limit')
    return data


def archive(pin, cache, limit):
    name=cache/(pin['sha256']+'.archive')
    if name.exists():
        data=bounded(name,limit)
    else:
        data=download(pin['url'],limit)
        require(digest(data)==pin['sha256'],'public archive checksum')
        with open(name,'xb') as file:file.write(data)
    require(digest(data)==pin['sha256'],'cached archive checksum')
    return data


def unpack(data, target):
    target.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as source:
        members=source.getmembers()
        require(len(members)<=50000 and sum(max(0,m.size) for m in members)<=1024**3,
                'archive expansion limit')
        source.extractall(target,members=members,filter='data')


def command(args, *, cwd, env, timeout=600, limit=64*1024*1024, fixture_input=None):
    # Compiler/scanner output contains only public build inputs, but never send
    # arbitrary tool text to CI. Bound it on disk and return fixed categories.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        child=subprocess.Popen([str(a) for a in args],cwd=cwd,env=env,
            stdin=subprocess.PIPE if fixture_input is not None else subprocess.DEVNULL,
            stdout=output,stderr=errors,start_new_session=True)
        try:
            if fixture_input is not None:
                require(len(fixture_input)==64,'bounded synthetic compatibility input')
                child.stdin.write(fixture_input);child.stdin.close()
            status=child.wait(timeout)
        except BaseException:
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL)
                child.wait()
            raise
        output.seek(0);data=output.read(limit+1)
        errors.seek(0);require(len(errors.read(limit+1))<=limit,'native tool error-output limit')
    require(len(data)<=limit,'native tool output limit')
    require(status==0,'native build/scanner execution failed')
    return data


def records(data):
    decoder=json.JSONDecoder();text=data.decode();values=[]
    while text.strip():
        value,end=decoder.raw_decode(text.lstrip());values.append(value);text=text.lstrip()[end:]
    require(all(isinstance(v,dict) for v in values),'scanner record schema')
    return values


def advisory_report(data, mode, scanner):
    values=records(data)
    configs=[v['config'] for v in values if 'config' in v]
    require(len(configs)==1 and any('SBOM' in v for v in values),'complete native scan required')
    config=configs[0]
    require(config.get('scanner_name')=='govulncheck'
            and config.get('scanner_version')==scanner['version']
            and config.get('scan_mode')==mode and config.get('scan_level')=='symbol'
            and config.get('db')==DATABASE and config.get('db_last_modified'), 'pinned fresh native scanner identity')
    imported=[];unused=[]
    for value in values:
        if 'finding' not in value:continue
        finding=value['finding'];trace=finding.get('trace')
        require(re.fullmatch(r'GO-[0-9]{4}-[0-9]+',finding.get('osv','')) and trace
                and isinstance(trace[0].get('module'),str),'native finding schema')
        first=trace[0]
        item=dict(id=finding['osv'],module=first['module'],version=first.get('version'))
        # Module-only evidence is distinct from importing an affected package.
        # Do not discard imported-but-uncalled package findings or use an ignore list.
        (imported if first.get('package') or first.get('function') else unused).append(item)
    report=dict(mode=mode,scanner=scanner['version'],database=DATABASE,
                database_modified=config['db_last_modified'],imported_or_called=len(imported),
                affected_ids=sorted({v['id'] for v in imported}),
                unimported_module_advisories=sorted({json.dumps(v,sort_keys=True) for v in unused}))
    report['unimported_module_advisories']=[json.loads(v) for v in report['unimported_module_advisories']]
    if imported:raise Findings(report)
    return report


def main_module_report(data):
    rows=json.loads(data)
    require(isinstance(rows,list) and rows and all(isinstance(r,dict) and isinstance(r.get('path'),str)
            and isinstance(r.get('vulns'),list) for r in rows),'native module-index schema')
    main=[row for row in rows if row['path']=='github.com/restic/restic']
    # Extracted source has '(devel)' build metadata. An upstream main-module
    # advisory therefore requires explicit version/source review, never silence.
    require(not main,'new upstream restic advisory requires exact source/version review')
    return dict(module='github.com/restic/restic',module_index_sha256=digest(data),
                policy='refuse any upstream main-module advisory pending explicit review',matching_entries=0)


def compatibility(legacy, current, work, env):
    source=work/'compatibility';source.mkdir(mode=0o700)
    repository=source/'repository';repository.mkdir(mode=0o700)
    files=source/'activity';files.mkdir(mode=0o700)
    payload=b'NXRS public native-format compatibility witness\n'
    (files/'sample').write_bytes(payload)
    # Public deterministic synthetic input, never production custody material.
    key=hashlib.sha256(b'nxrs public native compatibility fixture').hexdigest().encode()
    def invoke(binary,args):
        return command([binary,'--no-cache','--quiet','--json','--repo','local:'+str(repository),
                        '--password-file','/dev/stdin',*args],cwd=source,env=env,timeout=60,fixture_input=key)
    invoke(legacy,['init','--repository-version','2'])
    output=records(invoke(legacy,['backup','--force','--host','nxrs-public-compatibility','activity']))
    ids=[value['snapshot_id'] for value in output if value.get('message_type')=='summary']
    require(len(ids)==1 and re.fullmatch('[0-9a-f]{64}',ids[0]),'legacy synthetic backup receipt')
    invoke(current,['check','--read-data'])
    restored=source/'restored';restored.mkdir(mode=0o700)
    invoke(current,['restore',ids[0]+':/activity','--target',str(restored),'--verify','--overwrite','never'])
    require(bounded(restored/'sample',4096)==payload,'rebuilt engine restores exact legacy synthetic data')


def inventory(info, recipe, artifact, sha):
    require(info['GoVersion']==recipe['toolchains'][artifact]['version'], 'compiled Go version')
    require(info.get('Main',{}).get('Path')=='github.com/restic/restic','compiled main module')
    settings={value['Key']:value['Value'] for value in info.get('Settings',[])}
    target_os,target_arch=artifact.split('_')
    require(settings.get('CGO_ENABLED')=='0' and settings.get('GOOS')==target_os
            and settings.get('GOARCH')==target_arch and settings.get('-tags')==TAGS
            and settings.get('-trimpath')=='true' and not any(k.startswith('vcs.') for k in settings),
            'compiled native settings')
    modules=info.get('Deps');require(isinstance(modules,list) and modules,'compiled module inventory')
    components=[dict(type='application',name='nxrs-restic',version=recipe['source']['version']+'-nxrs.1',
                     **{'bom-ref':'engine'},hashes=[dict(alg='SHA-256',content=sha)]),
                dict(type='library',name='Go standard library',version=info['GoVersion'],**{'bom-ref':'stdlib'})]
    refs=[]
    for index,module in enumerate(modules):
        require(not module.get('Replace') and module.get('Path') and module.get('Version')
                and module.get('Sum','').startswith('h1:'),'compiled module identity')
        ref='module-'+str(index);refs.append(ref)
        components.append(dict(type='library',name=module['Path'],version=module['Version'],**{'bom-ref':ref},
            properties=[dict(name='nxrs:go:module-sum',value=module['Sum'])]))
    # Go h1 hashes identify module directory contents, not an archive SHA-256.
    # Keep them as properties rather than mislabeling them as artifact hashes.
    return dict(bomFormat='CycloneDX',specVersion='1.6',serialNumber='urn:uuid:'+str(uuid.uuid4()),version=1,
                components=components,dependencies=[dict(ref='engine',dependsOn=['stdlib',*refs])],
                metadata=dict(properties=[dict(name='nxrs:scope',value='embedded compiled Go module identities; not a whole-product SBOM')]))


def json_new(path, value):
    with open(path,'x') as file:json.dump(value,file,indent=2,sort_keys=True);file.write('\n')


def build(artifact, output, *, cache=None, qualify=False):
    require(not any(Path(str(output)+suffix).exists() for suffix in ['', '.provenance.json','.cdx.json','.advisories.json']),
            'new artifact and report outputs required')
    recipe_bytes=bounded(RECIPE,65536);recipe=json.loads(recipe_bytes)
    builder_sha=digest(bounded(Path(__file__),1024*1024))
    require(recipe['format_version']==1 and recipe['build']==dict(cgo=False,tags=[TAGS],trimpath=True,
            buildvcs=False,symbols=True),'reviewed native build recipe')
    host={'Darwin':'darwin','Linux':'linux'}.get(platform.system(),'')+'_'+{'arm64':'arm64','aarch64':'arm64',
            'x86_64':'amd64','AMD64':'amd64'}.get(platform.machine(),'')
    require(host in recipe['toolchains'] and artifact in recipe['artifacts'],'qualified build host/target')
    with tempfile.TemporaryDirectory(prefix='nxrs-native-build-') as temporary:
        work=Path(temporary).resolve();store=Path(cache).resolve() if cache else work/'downloads'
        store.mkdir(mode=0o700,parents=True,exist_ok=True)
        sdk_pin=recipe['toolchains'][host]
        unpack(archive(sdk_pin,store,150*1024*1024),work/'sdk')
        unpack(archive(recipe['source'],store,40*1024*1024),work/'source')
        source=work/'source'/('restic-'+recipe['source']['version']);go=work/'sdk/go/bin/go'
        print('PASS: exact native source and host SDK archives',flush=True)
        require((source/'internal/global/global.go').is_file(),'exact source layout')
        for name,pin in recipe['modules'].items():
            require(name in ['go.mod','go.sum'] and pin['path']=='restic/'+name,'module recipe paths')
            data=bounded(ROOT/'deploy'/pin['path'],1024*1024)
            require(digest(data)==pin['sha256'],'frozen module checksum');(source/name).write_bytes(data)
        env={'PATH':str(go.parent)+':/usr/bin:/bin','LANG':'C','GOENV':'off','GOTOOLCHAIN':'local',
             'GOPATH':str(work/'gopath'),'GOCACHE':str(work/'cache'),'GOSUMDB':'sum.golang.org','GOWORK':'off',
             'GOPROXY':'https://proxy.golang.org','GOTELEMETRY':'off','GOAUTH':'off','CGO_ENABLED':'0','GOMAXPROCS':'2','TMPDIR':str(work)}
        target_os,target_arch=artifact.split('_');target_env=dict(env,GOOS=target_os,GOARCH=target_arch,GOAMD64='v1')
        binary=work/'engine'
        command([go,'build','-mod=readonly','-trimpath','-buildvcs=false','-tags',TAGS,'-o',binary,'./cmd/restic'],
                cwd=source,env=target_env)
        data=bounded(binary,64*1024*1024);sha=digest(data)
        print('Observed native artifact',artifact,sha,flush=True)
        require(sha==recipe['artifacts'][artifact]['sha256'],'reproduced native executable checksum')
        print('PASS: exact native executable reproduced',artifact,sha,flush=True)
        for name,pin in recipe['modules'].items():
            require(digest(bounded(source/name,1024*1024))==pin['sha256'],'build changed frozen module graph')
        scanner=recipe['scanner'];module=scanner['module']+'@'+scanner['version']
        module_info=json.loads(command([go,'mod','download','-json',module],cwd=work,env=env,limit=1024*1024))
        require(module_info.get('Sum')==scanner['sum'] and module_info.get('GoModSum')==scanner['go_mod_sum'],
                'reviewed native scanner module identity')
        command([go,'install','-trimpath','golang.org/x/vuln/cmd/govulncheck@'+scanner['version']],cwd=work,env=env)
        checker=work/'gopath/bin/govulncheck'
        reports=[]
        for mode,args in [('binary',['-mode','binary',binary]),('source',['-tags',TAGS,'./cmd/restic'])]:
            reports.append(advisory_report(command([checker,'-json',*args],cwd=source,env=target_env,timeout=300),mode,scanner))
        compressed=download(DATABASE+'/index/modules.json.gz',8*1024*1024)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as decoded:index=decoded.read(32*1024*1024+1)
        require(len(index)<=32*1024*1024,'native module-index expansion limit')
        upstream=main_module_report(index)
        print('PASS: fresh native source/binary imported-package gates and upstream advisory index',flush=True)
        if qualify:
            require(host==artifact,'execute native upstream tests only on the actual target')
            command([go,'test','-mod=readonly','-tags',TAGS,'./internal/crypto','./internal/repository',
                     './internal/restorer','./internal/backend/local'],cwd=source,env=env)
            negative=recipe['negative_control'][artifact]
            compressed=archive(dict(url=negative['url'],sha256=negative['archive_sha256']),store,40*1024*1024)
            decoder=bz2.BZ2Decompressor();old=decoder.decompress(compressed,max_length=64*1024*1024+1)
            require(len(old)<=64*1024*1024 and decoder.eof and not decoder.unused_data
                    and digest(old)==negative['binary_sha256'],'affected negative artifact checksum')
            affected=work/'affected-control';affected.write_bytes(old)
            try:advisory_report(command([checker,'-json','-mode','binary',affected],cwd=source,env=env,timeout=300),
                                'binary',scanner)
            except Findings:pass
            else:raise Failure('affected native negative control was admitted')
            # The affected binary is executed only here against a fresh owned,
            # public synthetic compatibility fixture; never a live backup/key.
            affected.chmod(0o700)
            compatibility(affected,binary,work,env)
            print('PASS: native crypto/storage, affected-artifact refusal and legacy-format restore controls',flush=True)
        for name,pin in recipe['modules'].items():
            require(digest(bounded(source/name,1024*1024))==pin['sha256'],'qualification changed frozen module files')
        info=json.loads(command([go,'version','-m','-json',binary],cwd=source,env=env,limit=1024*1024))
        bom=inventory(info,recipe,artifact,sha)
        bom_bytes=(json.dumps(bom,indent=2,sort_keys=True)+'\n').encode()
        materials=[dict(uri='nxrs:gateway/tools/backup_engine.py',digest=dict(sha256=builder_sha)),
                   dict(uri=recipe['source']['url'],digest=dict(sha256=recipe['source']['sha256'])),
                   dict(uri=sdk_pin['url'],digest=dict(sha256=sdk_pin['sha256'])),
                   dict(uri='nxrs:backup-dependencies.json',digest=dict(sha256=digest(recipe_bytes))),
                   *[dict(uri='nxrs:'+pin['path'],digest=dict(sha256=pin['sha256'])) for pin in recipe['modules'].values()]]
        summary=dict(scans=reports,upstream=upstream,scanner_module=scanner,
                     scanner_binary_sha256=digest(bounded(checker,64*1024*1024)),qualification_controls=qualify)
        summary_bytes=(json.dumps(summary,indent=2,sort_keys=True)+'\n').encode()
        provenance={'_type':'https://in-toto.io/Statement/v1','subject':[dict(name=output.name,digest=dict(sha256=sha))],
            'predicateType':'https://slsa.dev/provenance/v1','predicate':{
                'buildDefinition':dict(buildType='https://github.com/yongkyuns/nxrs/backup-engine/v1',
                    externalParameters=dict(target=artifact,recipe_sha256=digest(recipe_bytes)),
                    internalParameters=dict(build=recipe['build']),resolvedDependencies=materials),
                'runDetails':dict(builder=dict(id='nxrs:checksum-pinned-backup-builder'),
                    metadata=dict(finishedOn=datetime.now(timezone.utc).isoformat()),
                    byproducts=[dict(name=output.name+'.advisories.json',mediaType='application/json',
                        digest=dict(sha256=digest(summary_bytes))),
                        dict(name=output.name+'.cdx.json',mediaType='application/json',
                             digest=dict(sha256=digest(bom_bytes)))])}}
        # All checks precede publication. These are unsigned local/CI build records;
        # repository release review and authenticating their distribution remain separate.
        with open(Path(str(output)+'.advisories.json'),'xb') as file:file.write(summary_bytes)
        with open(Path(str(output)+'.cdx.json'),'xb') as file:file.write(bom_bytes)
        json_new(Path(str(output)+'.provenance.json'),provenance)
        with open(output,'xb',opener=lambda p,f:os.open(p,f,0o700)) as destination:destination.write(data)
        print('PASS: reproduced and audited native backup artifact',artifact,sha,flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact',choices=['linux_amd64','darwin_arm64'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cache',type=Path)
    parser.add_argument('--qualify',action='store_true')
    args=parser.parse_args()
    try:build(args.artifact,args.output,cache=args.cache,qualify=args.qualify)
    except Failure as error:
        parser.exit(1,'Native backup qualification failed: '+str(error)+'. No artifact is admitted.\n')
    except OSError as error:
        parser.exit(1,'Native build OS operation failed (errno '+str(error.errno)+'); no artifact is admitted.\n')
    except (ValueError,KeyError,subprocess.SubprocessError,tarfile.TarError):
        parser.exit(1,'Native backup build, identity or advisory qualification failed; no artifact is admitted.\n')

if __name__=='__main__':main()
