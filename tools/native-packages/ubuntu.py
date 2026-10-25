#!/usr/bin/env python3
"""Inventory actual hosted-Ubuntu native gateway dependencies; gate Ubuntu OSV findings."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
ECOSYSTEM = 'Ubuntu:24.04:LTS'
API = 'https://api.osv.dev/v1/'
INPUTS = ('tools/native-packages/ubuntu.py','tools/native-packages/README.md',
          'tests/host/test_ubuntu_native_packages.py','.github/workflows/ubuntu-native-packages.yml',
          'tools/ci-apt.py','gateway/fuzz/run.py','gateway/Cargo.toml','gateway/Cargo.lock',
          'gateway/ingest/Cargo.toml','rust-toolchain.toml')
SYSTEM_ROOTS = dict(ffmpeg='/usr/bin/ffmpeg',ffprobe='/usr/bin/ffprobe',
                    openssl='/usr/bin/openssl',nginx='/usr/sbin/nginx',
                    cryptsetup='/usr/sbin/cryptsetup',python='/usr/bin/python3')
REPOSITORY_ROOTS = dict(publisher='nxrs-rtsp-publisher',reader='nxrs-rtsp-reader',
                       controller='nxrs-gateway-control',recorder='nxrs-camera-activity')


class Refusal(ValueError): pass


def require(value, category):
    if not value: raise Refusal(category)


def digest(raw): return hashlib.sha256(raw).hexdigest()


def file_digest(path, algorithm='sha256'):
    h=hashlib.new(algorithm)
    with open(path,'rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def parse(raw):
    def unique(items):
        result={}
        for key,value in items:
            require(key not in result,'duplicate-json-key');result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)


def hosted():
    require(sys.platform=='linux' and os.environ.get('GITHUB_ACTIONS')=='true'
            and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted','hosted-ubuntu-required')
    release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    require(release.get('ID','').strip('"')=='ubuntu' and release.get('VERSION_ID','').strip('"')=='24.04','ubuntu-24-04-required')


def execute(command, limit=2*1024*1024):
    spec=importlib.util.spec_from_file_location('ubuntu_native_process',ROOT/'gateway/fuzz/run.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    with tempfile.TemporaryDirectory(prefix='nxrs-ubuntu-command-') as directory:
        status,raw=runner.execute(command,Path(directory),'command',
            {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'},30)
    require(len(raw)<=limit,'native-tool-output-budget')
    return status,raw


def command(args):
    status,raw=execute(args);require(status==0,'native-tool-status');return raw.decode()


def package_name(value):
    require(isinstance(value,str) and re.fullmatch('[a-z0-9][a-z0-9+.-]{0,127}',value),'package-name')
    return value


def package_version(value):
    require(isinstance(value,str) and re.fullmatch('[0-9][A-Za-z0-9.+:~_-]{0,199}',value),'package-version')
    return value


def compare(a,operation,b):
    require(operation in ['lt','le','ge'],'version-operation')
    status,_=execute(['/usr/bin/dpkg','--compare-versions',package_version(a),operation,package_version(b)])
    require(status in [0,1],'debian-version-status');return status==0


def affected(record,name,selected,less=compare):
    package_name(name);package_version(selected)
    identity=record['id'];require(re.fullmatch(r'(?:UBUNTU-CVE-[0-9]{4}-[0-9]{4,}|USN-[0-9]+-[0-9]+|LSN-[0-9]+-[0-9]+)',identity),'ubuntu-advisory-id')
    withdrawal=record.get('withdrawn')
    if withdrawal is not None:
        require(isinstance(withdrawal,str) and len(withdrawal)<=64 and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z',withdrawal),'withdrawal-time')
        return False
    matches=[v for v in record['affected'] if v.get('package',{}).get('ecosystem')==ECOSYSTEM
             and v['package'].get('name')==name]
    require(matches,'matching-source-advisory-required')
    result=False
    for item in matches:
        ranges=item.get('ranges',[])
        require(isinstance(ranges,list) and len(ranges)<=128,'advisory-ranges')
        for interval in ranges:
            require(interval['type']=='ECOSYSTEM','debian-range-scheme')
            events=interval['events'];require(isinstance(events,list) and 1<=len(events)<=128,'debian-range-events')
            start=None
            for event in events:
                require(isinstance(event,dict) and len(event)==1,'debian-event-schema')
                kind,value=next(iter(event.items()));package_version(value)
                if kind=='introduced':
                    require(start is None,'range-open-twice');start=value
                else:
                    require(start is not None and kind in ['fixed','last_affected','limit'],'range-endpoint')
                    admitted=(start=='0' or less(selected,'ge',start)) and less(selected,'le' if kind=='last_affected' else 'lt',value)
                    result=result or admitted;start=None
            if start is not None:result=result or start=='0' or less(selected,'ge',start)
        if not ranges:
            versions=item.get('versions');require(isinstance(versions,list),'explicit-version-range')
            result=result or selected in versions
    return result


def fetch(endpoint,value=None):
    require(endpoint in ['querybatch','query'] or re.fullmatch(r'vulns/(?:UBUNTU-CVE-[0-9]{4}-[0-9]{4,}|USN-[0-9]+-[0-9]+|LSN-[0-9]+-[0-9]+)',endpoint),'official-api-endpoint')
    data=None if value is None else json.dumps(value).encode()
    request=urllib.request.Request(API+endpoint,data,headers={'Content-Type':'application/json','User-Agent':'nxrs-native-package-qualification'})
    with urllib.request.urlopen(request,timeout=30) as source:raw=source.read(8*1024*1024+1)
    require(len(raw)<=8*1024*1024,'advisory-response-budget');return parse(raw),digest(raw)


def query(source_packages):
    require(1<=len(source_packages)<=256,'source-package-budget')
    names=sorted(source_packages)
    requests=[dict(package=dict(ecosystem=ECOSYSTEM,name=package_name(n)),version=package_version(source_packages[n])) for n in names]
    value,response_sha=fetch('querybatch',dict(queries=requests))
    results=value['results'];require(isinstance(results,list) and len(results)==len(names),'complete-query-batch')
    ids={};receipts=[response_sha]
    for name,request,initial in zip(names,requests,results):
        items=initial;seen=set();tokens=set()
        for page in range(32):
            require(isinstance(items,dict) and set(items)<=set(['vulns','next_page_token']),'query-result-schema')
            vulns=items.get('vulns',[]);require(isinstance(vulns,list) and len(vulns)<=2048,'query-finding-budget')
            for item in vulns:
                identity=item['id'];require(isinstance(identity,str) and re.fullmatch(r'(?:UBUNTU-CVE-[0-9]{4}-[0-9]{4,}|USN-[0-9]+-[0-9]+|LSN-[0-9]+-[0-9]+)',identity) and identity not in seen,'duplicate-or-unknown-advisory-result')
                seen.add(identity)
            token=items.get('next_page_token')
            if not token:break
            require(isinstance(token,str) and 0<len(token)<=2048 and token not in tokens,'query-page-token')
            tokens.add(token);items,sha=fetch('query',dict(request,page_token=token));receipts.append(sha)
        else:raise Refusal('query-pagination-budget')
        ids[name]=sorted(seen)
    require(sum(map(len,ids.values()))<=2048,'total-findings-budget')
    identities=sorted({v for values in ids.values() for v in values})
    def read(identity):
        value,sha=fetch('vulns/'+identity);require(value['id']==identity,'advisory-response-identity')
        return identity,(value,sha)
    with ThreadPoolExecutor(max_workers=4) as pool:records=dict(pool.map(read,identities))
    findings=[];withdrawn=[]
    for name in names:
        for identity in ids[name]:
            value,sha=records[identity]
            matched=affected(value,name,source_packages[name])
            if value.get('withdrawn') is not None:
                require(not matched,'withdrawn-advisory-active')
                withdrawn.append(dict(source_package=name,source_version=source_packages[name],advisory=identity,record_sha256=sha,withdrawn=value['withdrawn']))
                continue
            require(matched,'service-range-disagreement')
            require(isinstance(value['modified'],str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z',value['modified']) and len(value['modified'])<=64,'advisory-modification-time')
            findings.append(dict(source_package=name,source_version=source_packages[name],advisory=identity,
                                 record_sha256=sha,modified=value['modified']))
    return findings,receipts,withdrawn


def dependencies(path):
    text=command(['/usr/bin/ldd',str(path)]);result=set()
    for line in text.splitlines():
        line=line.strip()
        if not line:continue
        if re.fullmatch(r'linux-vdso\.so\.[0-9]+ \(0x[0-9a-f]+\)',line):continue
        match=re.fullmatch(r'(?:[^\s]+ => )?(/[^\s]+) \(0x[0-9a-f]+\)',line)
        require(match is not None,'resolved-native-loader-entry')
        candidate=Path(match[1]).resolve();require(candidate.is_file(),'resolved-native-image')
        result.add(candidate)
    require(result,'nonempty-native-linkage');return result


def owner(path):
    candidates=[path.as_posix()]
    if candidates[0].startswith(('/usr/lib/','/usr/lib64/','/usr/bin/','/usr/sbin/')):
        candidates.append(candidates[0][4:])
    found=set()
    for candidate in candidates:
        status,raw=execute(['/usr/bin/dpkg-query','--search',candidate])
        if status==1:continue
        require(status==0,'package-owner-query')
        for line in raw.decode().splitlines():
            match=re.fullmatch(r'([a-z0-9+.-]+(?::[a-z0-9]+)?): (/.+)',line)
            require(match is not None,'package-owner-schema')
            if Path(match[2]).resolve()==path:found.add(match[1])
    if len(found)!=1:
        public=path.as_posix()
        if len(public)<=512 and re.fullmatch(r'/usr/(?:lib|lib64|bin|sbin)/[A-Za-z0-9_./+-]+',public):
            print('UBUNTU_NATIVE_OWNER_REFUSAL '+json.dumps(dict(system_image=public,matches=len(found)),sort_keys=True),flush=True)
        raise Refusal('unique-distribution-owner')
    return found.pop()


def package(identity):
    require(re.fullmatch('[a-z0-9+.-]+(?::[a-z0-9]+)?',identity),'binary-package-identity')
    text=command(['/usr/bin/dpkg-query','--show','--showformat=${Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\t${db:Status-Status}',identity])
    values=text.split('\t');require(len(values)==6 and values[5]=='installed','installed-native-package')
    name,version,arch,source,source_version,_=values
    package_name(name);package_version(version);package_name(source);package_version(source_version)
    require(arch in ['amd64','all'],'qualified-package-architecture')
    return dict(name=name,version=version,architecture=arch,source=source,source_version=source_version,
                purl='pkg:deb/ubuntu/'+name+'@'+version.replace(':','%3A')+'?arch='+arch+'&distro=noble')


def inventory():
    roots={**{k:Path(v).resolve() for k,v in SYSTEM_ROOTS.items()},
           **{k:(ROOT/'gateway/target/debug'/v).resolve() for k,v in REPOSITORY_ROOTS.items()}}
    require(all(p.is_file() for p in roots.values()),'complete-native-root-set')
    links={name:dependencies(path) for name,path in roots.items()}
    images=set(p for values in links.values() for p in values)
    images.update(roots[name] for name in SYSTEM_ROOTS)
    # Include actual system-Python TLS/hash extension images, loaded by this tool.
    import ssl
    for line in Path('/proc/self/maps').read_text().splitlines():
        parts=line.split(maxsplit=5)
        if len(parts)==6 and parts[5].startswith('/') and ('.so' in parts[5]):
            path=Path(parts[5]).resolve();require(path.is_file(),'mapped-native-image');images.add(path)
    require(1<=len(images)<=512,'native-image-budget')
    packages={};files={}
    for path in sorted(images):
        require(path.as_posix().startswith(('/usr/','/lib/')),'distribution-image-location')
        identity=owner(path)
        if identity not in packages:packages[identity]=package(identity)
        files[path.as_posix()]=dict(sha256=file_digest(path),binary_package=identity)
    sources={}
    for value in packages.values():
        name=value['source'];version=value['source_version']
        require(name not in sources or sources[name]==version,'unique-source-version');sources[name]=version
    return dict(roots={name:dict(sha256=file_digest(path),origin='ubuntu' if name in SYSTEM_ROOTS else 'repository-build',
                image=path.as_posix() if name in SYSTEM_ROOTS else 'gateway/target/debug/'+REPOSITORY_ROOTS[name],
                libraries=sorted(p.as_posix() for p in links[name])) for name,path in roots.items()},
                files=files,packages=packages,source_packages=sources)


def source_inputs():
    names=set(INPUTS)
    for crate in ['ingest','activity','control','private-file','media-process','media-sandbox']:
        names.update(str(p.relative_to(ROOT)) for p in (ROOT/'gateway'/crate/'src').rglob('*.rs'))
        names.add('gateway/'+crate+'/Cargo.toml')
    return {name:file_digest(ROOT/name) for name in sorted(names)}


def run(report):
    hosted();require(not os.path.lexists(report),'new-public-report')
    head=command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    require(not command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-native-source')
    before=source_inputs()
    # Warm the same HTTP/TLS/threaded collector path before snapshotting its
    # system-Python mappings; later lazy imports must not change the inventory.
    control,_,_=query({'ffmpeg':'7:6.1.1-3ubuntu5'})
    actual=inventory()
    value=dict(schema=1,scope='hosted-ubuntu-native-gateway-startup-images-and-system-python-tls',qualified=False,
        advisory_status='pending',checkout=dict(commit=head,clean=True),inputs=before,inventory=actual,ecosystem=ECOSYSTEM,
        advisory_api=API,advisory_responses_sha256=[],findings=None,withdrawn_records=[],
        negative_control=dict(package='ffmpeg',version='7:6.1.1-3ubuntu5',installed_for_control=False,findings=control),
        checked_on=datetime.now(timezone.utc).isoformat(),package_payload_authenticated=False,
        dynamic_plugin_coverage=False,kernel_qualified=False,full_native_advisory_closure=False,
        firmware_qualified=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)
    identity=save_report(report,value)
    try:
        findings,receipts,withdrawn=query(actual['source_packages'])
        require(any(v['advisory']=='UBUNTU-CVE-2025-59734' for v in control),'actual-affected-ubuntu-control')
        require(before==source_inputs() and actual==inventory()
                and command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()==head
                and not command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(), 'unchanged-native-inputs')
        value.update(qualified=not findings,advisory_status='complete',findings=findings,
                     withdrawn_records=withdrawn,advisory_responses_sha256=receipts)
    except Exception as error:
        category=str(error) if isinstance(error,Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        value.update(qualified=False,advisory_status='failed',failure_category=category)
        save_report(report,value,identity)
        raise
    save_report(report,value,identity)
    return not findings


def save_report(report,value,identity=None):
    raw=(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
    require(len(raw)<=1024*1024,'public-native-report-budget')
    if identity is None:
        with open(report,'xb') as output:output.write(raw)
        info=report.lstat();return info.st_dev,info.st_ino
    current=report.lstat()
    require((current.st_dev,current.st_ino)==identity and not report.is_symlink(),'owned-report-publication')
    with tempfile.TemporaryDirectory(prefix='nxrs-native-report-',dir=report.parent) as directory:
        staged=Path(directory)/'public.json';staged.write_bytes(raw);staged.chmod(0o644)
        os.replace(staged,report)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    try:accepted=run(args.report)
    except Exception as error:
        category=str(error) if isinstance(error,Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Ubuntu native package qualification refused ('+category+').\n')
    if not accepted:parser.exit(1,'Ubuntu native package advisories remain unresolved; bounded public report retained.\n')
    print('PASS: current Ubuntu startup-native package advisory scope; other release gates remain open')


if __name__=='__main__':main()
