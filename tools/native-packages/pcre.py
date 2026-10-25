#!/usr/bin/env python3
"""Replace PCRE2 only in an explicitly guarded disposable hosted qualification."""
import argparse,copy,hashlib,importlib.util,json,os
from pathlib import Path
import re,secrets,shutil,sys
import ubuntu

ROOT=ubuntu.ROOT
sys.path.insert(0,str(ROOT/'gateway/tools'))
import system_regex_evidence as evidence
INPUTS=evidence.INPUTS
RUNTIME,LINK=evidence.RUNTIME,evidence.LINK

def require(value,category):ubuntu.require(value,category)

def exports(raw):
    result=set()
    for line in raw.decode('utf-8').splitlines():
        parts=line.split()
        if parts and parts[0].startswith('pcre2_'):
            require(evidence.EXPORT.fullmatch(parts[0]),'recognized-pcre-symbol-version-required')
            require(parts[0] not in result,'unique-pcre-export');result.add(parts[0])
    require(evidence.REQUIRED<=evidence.export_bases(result) and len(result)<=512,'complete-pcre-export-set')
    return sorted(result)

def soname(raw):
    values=re.findall(rb'\(SONAME\).*?\[([^\]]+)\]',raw)
    require(values==[b'libpcre2-8.so.0'],'matching-pcre-soname')
    return values[0].decode()

def test_summary(raw):
    text=raw.decode('utf-8')
    require(sorted(re.findall(r'^PASS: ([A-Za-z0-9_]+)$',text,re.M))==
            sorted(['pcre2posix_test','pcre2_jit_test','RunTest','RunGrepTest']),'actual-pcre-upstream-suites')
    rows=re.findall(r'^# (TOTAL|PASS|SKIP|XFAIL|FAIL|XPASS|ERROR):\s+([0-9]+)\s*$',text,re.M)
    require(len(rows)==7 and len({name for name,_ in rows})==7,'unique-pcre-test-counts')
    fields={name:int(value) for name,value in rows}
    require(fields==dict(TOTAL=4,PASS=4,SKIP=0,XFAIL=0,FAIL=0,XPASS=0,ERROR=0),'complete-pcre-upstream-tests')
    return dict(total=4,passed=4,skipped=0,failed=0,errors=0)

def compatible(old,new):
    require(evidence.compatible_exports(old,new),'preserved-pcre-abi')

def redirect(work,target,phase):
    # Rename a new symlink; never truncate or overwrite an in-use ELF image.
    temporary=LINK.with_name('.pcre2-qualification-'+secrets.token_hex(8))
    require(not os.path.lexists(temporary),'new-pcre-link')
    work.execute(['sudo','-n','/usr/bin/ln','-s','--',target,temporary],phase+'-link')
    try:work.execute(['sudo','-n','/usr/bin/mv','-T','--',temporary,LINK],phase+'-rename')
    except BaseException:
        work.execute(['sudo','-n','/usr/bin/rm','-f','--',temporary],phase+'-unlink',accepted=(0,1))
        raise

def run(directory):
    ubuntu.hosted() # Before writes, native commands, network or privileged actions.
    require(os.uname().machine=='x86_64' and os.getuid()!=0,'unprivileged-amd64-pcre-qualification')
    require(not os.path.lexists(directory) and not os.path.lexists(RUNTIME),'fresh-system-regex-runtime')
    require(LINK.is_symlink(),'existing-pcre-soname-link')
    old_target=LINK.readlink();old=LINK.resolve()
    require(not old_target.is_absolute() and len(old_target.parts)==1
            and re.fullmatch(r'libpcre2-8\.so\.0\.[0-9]+\.[0-9]+',str(old_target))
            and old.parent==LINK.parent,'expected-distribution-pcre-link')
    owner=ubuntu.package(ubuntu.owner(old))
    require(owner['name']=='libpcre2-8-0' and owner['source']=='pcre2','distribution-pcre-owner')
    head=ubuntu.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    require(not ubuntu.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-system-regex-source')
    before=evidence.inputs(ROOT);old_digest=ubuntu.file_digest(old)
    sys.path.insert(0,str(ROOT/'tools/gateway-proxy'))
    spec=importlib.util.spec_from_file_location('pcre_source_build',ROOT/'tools/gateway-proxy/qualify.py')
    source_tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(source_tools)
    policy=ubuntu.parse((ROOT/source_tools.advisories.POLICY).read_bytes());source_tools.validate_sources(policy)
    directory.mkdir(mode=0o700);(directory/'gnupg').mkdir(mode=0o700);work=source_tools.Work(directory)
    work.env.update(CFLAGS='-O2 -fPIC -fstack-protector-strong -D_FORTIFY_SOURCE=2 -ffile-prefix-map='+str(directory)+'=/usr/src/pcre2',
                    LDFLAGS='-Wl,-z,relro,-z,now')
    selected=None
    try:
        rows=source_tools.advisories.pcre_articles(work.get(source_tools.advisories.PCRE,maximum=1024*1024))
        reviewed=source_tools.advisories.review_pcre(policy,rows)
        changed=copy.deepcopy(policy);changed['sources']['pcre2']['version']='10.48'
        try:source_tools.advisories.review_pcre(changed,rows)
        except ubuntu.Refusal as error:require(str(error)=='pcre-upstream-fix-required','affected-pcre-control')
        else:raise ubuntu.Refusal('affected-pcre-version-was-admitted')
        source,signed=source_tools.signed_source(work,'pcre2',policy['sources']['pcre2'])
        abi=policy['shared_pcre2_abi'];require(abi['path']=='src/libpcre2-8.sym','reviewed-pcre-version-script-path')
        work.execute([source/'configure','--prefix='+str(RUNTIME),'--enable-shared','--disable-static',
            '--enable-pcre2-8','--disable-pcre2-16','--disable-pcre2-32','--enable-jit','--enable-unicode',
            '--disable-pcre2grep-libz','--disable-pcre2grep-libbz2'],'configure-pcre',cwd=source)
        version_script_sha256=ubuntu.file_digest(source/abi['path'])
        require(version_script_sha256==abi['sha256'],'exact-signed-pcre-version-script')
        work.execute(['/usr/bin/make','-j2'],'build-pcre',300,cwd=source)
        _,raw=work.execute(['/usr/bin/make','check'],'check-pcre',180,cwd=source);tests=test_summary(raw)
        libraries=[p for p in (source/'.libs').glob('libpcre2-8.so.*') if p.is_file() and not p.is_symlink()]
        require(len(libraries)==1 and re.fullmatch(r'libpcre2-8\.so\.0\.[0-9]+\.[0-9]+',libraries[0].name),'one-built-pcre-image')
        library=libraries[0]
        _,raw=work.execute(['/usr/bin/readelf','--dynamic','--wide',library],'pcre-soname');name=soname(raw)
        _,raw=work.execute(['/usr/bin/nm','--dynamic','--defined-only','--extern-only','--format=posix',old],'old-pcre-exports');old_exports=exports(raw)
        _,raw=work.execute(['/usr/bin/nm','--dynamic','--defined-only','--extern-only','--format=posix',library],'new-pcre-exports');new_exports=exports(raw)
        require(new_exports==abi['default_exports'],'exact-reviewed-pcre-default-versions')
        compatible(old_exports,new_exports)
        work.execute(['sudo','-n','install','-d','-m','0755','-o',str(os.getuid()),'-g',str(os.getgid()),RUNTIME],'new-pcre-prefix')
        (RUNTIME/'lib').mkdir(mode=0o755);(RUNTIME/'lib').chmod(0o755);installed=RUNTIME/'lib'/library.name
        shutil.copyfile(library,installed);installed.chmod(0o644)
        for link in ['libpcre2-8.so','libpcre2-8.so.0']:(RUNTIME/'lib'/link).symlink_to(library.name)
        work.execute(['sudo','-n','/usr/bin/chown','-R','0:0',RUNTIME],'seal-pcre-prefix')
        probe=directory/'probe'
        work.execute(['/usr/bin/cc','-O2','-Wall','-Wextra','-Werror','-I'+str(source/'src'),ROOT/'tests/host/native/system_regex.c',
            '-L'+str(RUNTIME/'lib'),'-lpcre2-8','-ldl','-o',probe],'build-shared-pcre-probe')
        expected=b'PCRE2 10.49 shared probe: exact image, Unicode/JIT and ordinary matching pass'
        _,raw=work.execute(['/lib64/ld-linux-x86-64.so.2','--library-path',RUNTIME/'lib',probe,installed],'private-shared-pcre-probe')
        require(raw.strip()==expected,'private-shared-pcre-controls')
        require(LINK.readlink()==old_target and ubuntu.file_digest(old)==old_digest,'unchanged-old-pcre-selection')
        selected=installed;redirect(work,installed,'select-pcre')
        _,raw=work.execute([probe,installed],'installed-shared-pcre-probe');require(raw.strip()==expected,'actual-selected-pcre-image')
        for program in ['/usr/sbin/cryptsetup','/usr/bin/systemctl']:
            links=ubuntu.dependencies(Path(program));require(installed in links and old not in links,'actual-system-tools-use-fixed-pcre')
            work.execute([program,'--version'],'system-tool-version')
        require(before==evidence.inputs(ROOT) and head==ubuntu.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
            and not ubuntu.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-system-regex-source')
        result=dict(schema=1,scope=evidence.SCOPE,qualified=True,verification_status='complete',checkout=dict(commit=head,clean=True),inputs=before,
            source=policy['sources']['pcre2'],source_signature=signed,upstream_advisories=reviewed,affected_source_version_refused=True,
            upstream_tests=tests,jit_enabled=True,loaded_library_probe_passed=True,old_exports=old_exports,new_exports=new_exports,
            version_script_sha256=version_script_sha256,soname=name,library='lib/'+installed.name,library_sha256=ubuntu.file_digest(installed),old_library_sha256=old_digest,
            configuration_sha256=ubuntu.file_digest(source/'config.log'),consumer_names=['cryptsetup','service_manager'],
            **dict.fromkeys(evidence.FLAGS,False))
        evidence.validate(result,ROOT,head);raw=evidence.encoded(result)+b'\n';require(len(raw)<=32768,'bounded-system-regex-receipt')
        public=directory/'qualification.json';public.write_bytes(raw)
        work.execute(['sudo','-n','/usr/bin/install','-m','0644','-o','0','-g','0',public,RUNTIME/'qualification.json'],'publish-pcre-receipt')
        evidence.verify(ROOT,head)
    except BaseException:
        if selected is not None and LINK.is_symlink() and LINK.readlink()==selected and ubuntu.file_digest(old)==old_digest:
            redirect(work,old_target,'restore-original-pcre')
        if (RUNTIME/'qualification.json').exists():
            work.execute(['sudo','-n','/usr/bin/rm','-f','--',RUNTIME/'qualification.json'],'invalidate-pcre-receipt',accepted=(0,1))
        raise
    print('PASS: signed shared PCRE2, upstream suites, compatible ABI and actual system-tool linkage; whole-OS/release gates remain open')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True)
    try:run(parser.parse_args().work)
    except Exception as error:
        category=str(error) if isinstance(error,ubuntu.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Shared system regex qualification refused ('+category+').\n')

if __name__=='__main__':main()
