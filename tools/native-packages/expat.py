#!/usr/bin/env python3
"""Qualify a signed Expat candidate only in a disposable hosted Ubuntu image."""
import argparse,hashlib,importlib.util,json,os,re,secrets,sys
from pathlib import Path
import ubuntu
ROOT=ubuntu.ROOT
sys.path.insert(0,str(ROOT/'gateway/tools'))
import system_xml_evidence as evidence
RUNTIME,LINK=evidence.RUNTIME,evidence.LINK
INPUTS=evidence.INPUTS

def require(value,category):ubuntu.require(value,category)

def policy(value):
    require(set(value)=={'schema','scope','source','vendor_baseline','matched_distribution_advisories','documented_fix_releases','unresolved_published_advisories','upstream_issue','upstream_advisory_closure','full_native_advisory_closure','deployment_permitted'} and
            type(value['schema']) is int and value['schema']==1 and value['scope']=='hosted-system-python-expat-candidate','reviewed-system-xml-policy')
    p=value['source']
    require(set(p)=={'version','root','url','sha256','signature','key'} and
            set(p['signature'])=={'url','sha256'} and set(p['key'])=={'url','sha256','fingerprint','reference'},'exact-system-xml-source-policy')
    require(p['version']=='2.9.0' and p['root']=='expat-2.9.0' and p['key']['fingerprint']=='3176EF7DB2367F1FCA4F306B1F9B0E909AF37285','reviewed-system-xml-signer-and-version')
    require(p['url']=='https://github.com/libexpat/libexpat/releases/download/R_2_9_0/expat-2.9.0.tar.bz2' and
            p['signature']['url']==p['url']+'.asc' and p['key']['url']=='https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x3176ef7db2367f1fca4f306b1f9b0e909af37285','official-system-xml-source')
    # This exact certificate renews the existing primary and signing subkey.
    # Keep expiry/revocation verification enabled; never refresh trust implicitly.
    require(p['key']['sha256']=='48a06f7b782958ecbab278a54581a92102a8b003bcf85fea4df2d9154699c2ba','reviewed-system-xml-certificate-renewal')
    for row in (p,p['signature'],p['key']):require(re.fullmatch('[a-f0-9]{64}',row['sha256']),'pinned-system-xml-source-digest')
    require(p['key']['reference']=='https://github.com/libexpat/libexpat/issues/397#issuecomment-609777835','published-system-xml-key-authority')
    baseline=value['vendor_baseline']
    require(set(baseline)=={'binary_package','source_package','version','library','sha256','authenticated_source_reference'} and
            baseline['binary_package']=='libexpat1' and baseline['source_package']=='expat' and
            baseline['version']=='2.6.1-2ubuntu0.6' and baseline['library']=='libexpat.so.1.9.1' and
            baseline['sha256']=='286682ecbc5e59a638963b1a4e6351e65eb32fcf4bdcb9cb7569b6a61fe06a8d' and
            baseline['authenticated_source_reference']=='https://github.com/yongkyuns/nxrs/actions/runs/40317275553','reviewed-authenticated-system-xml-baseline')
    require(value['matched_distribution_advisories']==['CVE-2025-66382','CVE-2026-102633','CVE-2026-77214','CVE-2026-93990'],'exact-system-xml-distribution-records')
    require(value['documented_fix_releases']=={'CVE-2026-102633':'2.9.0','CVE-2026-77214':'2.9.0','CVE-2026-93990':'2.8.5'} and
            value['unresolved_published_advisories']==['CVE-2025-66382'],'unresolved-system-xml-record-retained')
    require(value['upstream_issue']==dict(url='https://api.github.com/repos/libexpat/libexpat/issues/1160',reference='https://github.com/libexpat/libexpat/issues/1160'),'independent-system-xml-upstream-status')
    require(all(value[k] is False for k in ('upstream_advisory_closure','full_native_advisory_closure','deployment_permitted')),'no-system-xml-closure-override')
    return value

def exports(raw):
    result=set()
    for line in raw.decode().splitlines():
        fields=line.split()
        if fields and fields[0].startswith('XML_'):
            require(re.fullmatch('XML_[A-Za-z0-9_]+',fields[0]) and fields[0] not in result,'unique-system-xml-export');result.add(fields[0])
    require(evidence.REQUIRED<=result and len(result)<=512,'complete-system-xml-export-set')
    return sorted(result)

def soname(raw):
    values=re.findall(rb'\(SONAME\).*?\[([^\]]+)\]',raw);require(values==[b'libexpat.so.1'],'same-system-xml-soname');return values[0].decode()

def built_library(source):
    # The signed release declares libexpat in lib/Makefile.am, so libtool's
    # shared images are in lib/.libs, not the top-level build directory.
    libraries=[p for p in (source/'lib/.libs').glob('libexpat.so.*') if p.is_file() and not p.is_symlink()]
    require(len(libraries)==1 and libraries[0].name=='libexpat.so.1.13.0','one-system-xml-built-image')
    return libraries[0]

def test_summary(raw,log):
    rows=re.findall(rb'^# (TOTAL|PASS|SKIP|XFAIL|FAIL|XPASS|ERROR):\s+([0-9]+)\s*$',raw,re.M)
    require(len(rows)==7 and len({key for key,_ in rows})==7,'unique-system-xml-suite-summary')
    fields={key.decode():int(value) for key,value in rows}
    require(fields==dict(TOTAL=1,PASS=1,SKIP=0,XFAIL=0,FAIL=0,XPASS=0,ERROR=0) and b'PASS: runtests' in raw,'complete-system-xml-upstream-tests')
    checks=re.findall(rb'^100%: Checks: ([0-9]+), Failed: 0\r?$',log,re.M)
    require(len(checks)==1 and 1<=int(checks[0])<=100000,'actual-system-xml-test-checks')
    return dict(total=1,passed=1,skipped=0,failed=0,errors=0,checks=int(checks[0]))

def published_fixes(raw,required):
    require(len(raw)<=1024*1024,'bounded-system-xml-changelog');text=raw.decode()
    releases=list(re.finditer(r'^Release ([0-9]+\.[0-9]+\.[0-9]+)[^\n]*\n',text,re.M));require(1<=len(releases)<=512,'system-xml-release-history')
    result={}
    for i,match in enumerate(releases):
        version=match[1]
        if tuple(map(int,version.split('.')))>(2,9,0):continue
        block=text[match.end():releases[i+1].start() if i+1<len(releases) else len(text)]
        security=re.search(r'^\s*Security fixes:\s*\n(.*?)(?=^\s*(?:Bug fixes|Other changes|New features|Infrastructure|Special thanks):|\Z)',block,re.M|re.S)
        if not security:continue
        for identity in required:
            if re.search(r'\b'+re.escape(identity)+r'\b',security[1]):result.setdefault(identity,version)
    require(set(result)==set(required),'published-system-xml-fixes-required');return result

def upstream_status(raw,pin):
    require(len(raw)<=128*1024,'bounded-system-xml-upstream-issue');v=ubuntu.parse(raw)
    require(v.get('number')==1160 and v.get('html_url')==pin['reference'] and v.get('state') in ('open','closed') and isinstance(v.get('body'),str) and len(v['body'])<=65536,'official-system-xml-upstream-issue')
    count=len(re.findall(r'^\s*- \[ \]',v['body'],re.M));require(count<=256,'bounded-unresolved-system-xml-items')
    return dict(reference=pin['reference'],state=v['state'],updated_at=v['updated_at'],body_sha256=hashlib.sha256(v['body'].encode()).hexdigest(),unchecked_items=count,unchecked_items_are_not_vulnerability_count=True)

def public_failure_locations(error):
    sys.path.insert(0,str(ROOT/'gateway/tests'))
    import failure_location
    names=('tools/native-packages/expat.py','gateway/tools/system_xml_evidence.py',
           'tools/gateway-proxy/qualify.py','tools/native-packages/payloads.py','tools/native-packages/ubuntu.py')
    return failure_location.trace_locations(error,{str(ROOT/name):name for name in names})

def redirect(work,target,phase):
    temporary=LINK.with_name('.expat-qualification-'+secrets.token_hex(8));require(not os.path.lexists(temporary),'new-system-xml-selection')
    work.execute(['sudo','-n','/usr/bin/ln','-s','--',target,temporary],phase+'-link')
    try:work.execute(['sudo','-n','/usr/bin/mv','-T','--',temporary,LINK],phase+'-rename')
    except BaseException:work.execute(['sudo','-n','/usr/bin/rm','-f','--',temporary],phase+'-unlink',accepted=(0,1));raise

def work_location(directory):
    value=os.environ.get('RUNNER_TEMP','');parent=Path(value)
    require(bool(value) and parent.is_absolute() and directory.is_absolute() and directory.parent==parent and
            re.fullmatch('[A-Za-z0-9_-]{1,64}',directory.name) and not os.path.lexists(directory),'owned-runner-system-xml-work')
    info=parent.stat()
    require(parent.resolve()==parent and parent.is_dir() and info.st_uid==os.getuid() and not info.st_mode&0o022,'protected-runner-system-xml-work-parent')
    return directory

def source_tools():
    # qualify.py imports its reviewed sibling advisories.py. A dynamic file
    # load does not add that directory to the fresh process's import path.
    sys.path.insert(0,str(ROOT/'tools/gateway-proxy'))
    spec=importlib.util.spec_from_file_location('system_xml_source_build',ROOT/'tools/gateway-proxy/qualify.py')
    tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(tools)
    return tools

def run(directory):
    ubuntu.hosted() # No personal-host reads, writes, native commands or downloads.
    require(os.uname().machine=='x86_64' and os.getuid()!=0,'unprivileged-amd64-system-xml-qualification')
    work_location(directory);require(not os.path.lexists(RUNTIME),'fresh-system-xml-work')
    pin=policy(ubuntu.parse((ROOT/evidence.POLICY).read_bytes()));baseline=pin['vendor_baseline']
    require(LINK.is_symlink(),'existing-system-xml-soname-link');old_target=LINK.readlink();old=LINK.resolve(strict=True)
    require(not old_target.is_absolute() and old_target.parts==(baseline['library'],) and old.parent==LINK.parent,'known-system-xml-vendor-image')
    package=ubuntu.package(ubuntu.owner(old));header=Path('/usr/include/expat.h');header_package=ubuntu.package(ubuntu.owner(header))
    require(package['name']==baseline['binary_package'] and package['source']==baseline['source_package'] and package['version']==package['source_version']==header_package['source_version']==baseline['version'] and ubuntu.file_digest(old)==baseline['sha256'],'authenticated-system-xml-baseline-byte-join')
    head=ubuntu.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip();require(not ubuntu.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-system-xml-source')
    before=evidence.inputs(ROOT);directory.mkdir(mode=0o700);(directory/'gnupg').mkdir(mode=0o700)
    tools=source_tools();work=tools.Work(directory)
    work.env.update(CFLAGS='-O2 -fPIC -fstack-protector-strong -D_FORTIFY_SOURCE=2 -ffile-prefix-map='+str(directory)+'=/usr/src/expat',CXXFLAGS='-O2 -fstack-protector-strong',LDFLAGS='-Wl,-z,relro,-z,now')
    selected=None;owned_prefix=False
    try:
        source,signed=tools.signed_source(work,'expat',pin['source'])
        fixes=published_fixes((source/'Changes').read_bytes(),pin['documented_fix_releases'])
        require(fixes==pin['documented_fix_releases'],'exact-published-system-xml-fix-releases')
        reports=upstream_status(work.get(pin['upstream_issue']['url'],maximum=128*1024),pin['upstream_issue'])
        work.execute([source/'configure','--prefix='+str(RUNTIME),'--enable-shared','--disable-static','--without-docbook','--without-examples','--without-xmlwf','--with-tests'],'configure-system-xml',180,cwd=source)
        work.execute(['/usr/bin/make','-j2'],'build-system-xml',300,cwd=source)
        _,raw=work.execute(['/usr/bin/make','check'],'test-system-xml',300,cwd=source)
        with open(source/'tests/runtests.log','rb') as output:log=output.read(1024*1024+1)
        require(len(log)<=1024*1024,'bounded-system-xml-test-log');tests=test_summary(raw,log)
        library=built_library(source)
        _,raw=work.execute(['/usr/bin/readelf','--dynamic','--wide',library],'system-xml-soname');name=soname(raw)
        symbols=[]
        for path,phase in ((old,'vendor'),(library,'candidate')):
            _,raw=work.execute(['/usr/bin/nm','--dynamic','--defined-only','--extern-only','--format=posix',path],phase+'-xml-exports');symbols.append(exports(raw))
        require(set(symbols[0])<=set(symbols[1]),'preserved-system-xml-symbols')
        work.execute(['sudo','-n','/usr/bin/mkdir','-m','0755',RUNTIME],'new-system-xml-prefix');owned_prefix=True
        work.execute(['sudo','-n','/usr/bin/mkdir','-m','0755',RUNTIME/'lib'],'new-system-xml-libdir')
        installed=RUNTIME/'lib'/library.name
        work.execute(['sudo','-n','/usr/bin/install','-m','0644','-o','0','-g','0',library,installed],'seal-system-xml-image')
        for alias in ('libexpat.so','libexpat.so.1'):work.execute(['sudo','-n','/usr/bin/ln','-s',library.name,RUNTIME/'lib'/alias],'link-system-xml-image')
        probe=directory/'probe'
        work.execute(['/usr/bin/cc','-O2','-Wall','-Wextra','-Werror','-I/usr/include',ROOT/'tests/host/native/system_xml.c','-L'+str(RUNTIME/'lib'),'-lexpat','-ldl','-o',probe],'build-system-xml-probe')
        work.execute([probe,installed],'refuse-wrong-system-xml-image',accepted=(1,))
        work.execute([probe,old],'refuse-old-system-xml-version',accepted=(2,))
        _,raw=work.execute(['/lib64/ld-linux-x86-64.so.2','--library-path',RUNTIME/'lib',probe,installed],'private-system-xml-probe');require(raw.strip()==b'EXPAT_2_9_0_ABI_AND_LOADER_OK','private-system-xml-sdk-controls')
        require(LINK.readlink()==old_target and ubuntu.file_digest(old)==baseline['sha256'],'unchanged-system-xml-original')
        selected=installed;redirect(work,installed,'select-system-xml')
        _,raw=work.execute([probe,installed],'selected-system-xml-probe');require(raw.strip()==b'EXPAT_2_9_0_ABI_AND_LOADER_OK','selected-system-xml-sdk-controls')
        _,raw=work.execute(['/usr/bin/python3','-I',ROOT/'tests/host/native/system_xml.py',installed],'actual-system-python-xml');python=ubuntu.parse(raw)
        redirect(work,old_target,'rollback-system-xml');work.execute([probe,old],'actual-system-xml-rollback',accepted=(2,))
        require(ubuntu.file_digest(old)==baseline['sha256'],'preserved-system-xml-vendor-bytes')
        redirect(work,installed,'reselect-system-xml')
        _,raw=work.execute([probe,installed],'reselected-system-xml-probe');require(raw.strip()==b'EXPAT_2_9_0_ABI_AND_LOADER_OK','reselected-system-xml-controls')
        _,raw=work.execute(['/usr/bin/python3','-I',ROOT/'tests/host/native/system_xml.py',installed],'recovered-system-python-xml');require(ubuntu.parse(raw)==python,'same-recovered-system-python-xml')
        require(before==evidence.inputs(ROOT) and head==ubuntu.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip() and not ubuntu.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-system-xml-source')
        result=dict(schema=1,scope=evidence.SCOPE,qualified=True,verification_status='complete',checkout=dict(commit=head,clean=True),inputs=before,source=pin['source'],source_signature=signed,
                    vendor=dict(name=package['name'],version=package['version'],source=package['source'],source_version=package['source_version'],sha256=baseline['sha256'],header_sha256=ubuntu.file_digest(header),header_source_version=header_package['source_version']),
                    soname=name,library='lib/'+installed.name,library_sha256=ubuntu.file_digest(installed),old_exports=symbols[0],new_exports=symbols[1],configuration_sha256=ubuntu.file_digest(source/'config.log'),
                    upstream_tests=tests,published_fixes=fixes,unresolved_published_advisories=pin['unresolved_published_advisories'],upstream_reports=reports,python=python,private_probe=True,selected_probe=True,wrong_image_refusals=2,rollback_control_executed=True,**dict.fromkeys(evidence.FLAGS,False))
        evidence.validate(result,ROOT,head);raw=evidence.encoded(result)+b'\n';require(len(raw)<=65536,'bounded-system-xml-receipt');public=directory/'qualification.json';public.write_bytes(raw)
        work.execute(['sudo','-n','/usr/bin/install','-m','0644','-o','0','-g','0',public,RUNTIME/'qualification.json'],'publish-system-xml-receipt');evidence.verify(ROOT,head)
    except BaseException:
        if owned_prefix and (RUNTIME/'qualification.json').exists():work.execute(['sudo','-n','/usr/bin/rm','-f','--',RUNTIME/'qualification.json'],'invalidate-system-xml-receipt',accepted=(0,1))
        if selected is not None and LINK.is_symlink() and LINK.readlink()==selected and ubuntu.file_digest(old)==baseline['sha256']:redirect(work,old_target,'restore-original-system-xml')
        raise
    print('PASS: scoped signed Expat, vendor ABI, upstream tests, actual system Python and rollback; upstream/native closure stays unqualified')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True)
    try:run(parser.parse_args().work)
    except Exception as error:
        locations=public_failure_locations(error)
        if locations:print('SYSTEM_XML_FAILURE_LOCATIONS '+json.dumps(locations,sort_keys=True,separators=(',',':')),flush=True)
        category=str(error) if isinstance(error,ubuntu.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'System XML qualification refused ('+category+').\n')

if __name__=='__main__':main()
