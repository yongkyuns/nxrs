#!/usr/bin/env python3
"""Build and exercise the signed minimal proxy only on disposable hosted Ubuntu."""
import argparse
import copy
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import advisories

ROOT=advisories.ROOT
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import payloads
native=payloads.u
sys.path.insert(0,str(ROOT/'gateway/tools'))
import gateway_proxy_evidence as evidence
RUNTIME=evidence.RUNTIME
DISABLED=['charset','gzip','ssi','userid','access','auth_basic','mirror','autoindex','geo','map','split_clients','referer',
          'grpc','memcached','empty_gif','browser','upstream_hash','upstream_ip_hash','upstream_least_conn','upstream_random','upstream_keepalive','upstream_zone']


def require(value,category):native.require(value,category)
def inputs():return payloads.candidate.source_inputs()


def validate_sources(policy):
    require(set(policy['sources'])=={'nginx','pcre2'},'two-exact-proxy-sources')
    for name,fingerprint in [('nginx','D6786CE303D9A9022998DC6CC8464D549AF75C0A'),('pcre2','A95536204A3BB489715231282A98E77EB6F24CA8')]:
        pin=policy['sources'][name];advisories.version(pin['version'])
        require(pin['key']['fingerprint']==fingerprint and pin['root']==name+'-'+pin['version'],'reviewed-proxy-signing-root')
        urls={'nginx':'https://nginx.org/download/nginx-'+pin['version']+'.tar.gz',
              'pcre2':'https://github.com/PCRE2Project/pcre2/releases/download/pcre2-'+pin['version']+'/pcre2-'+pin['version']+'.tar.bz2'}
        require(pin['url']==urls[name] and pin['signature']['url']==pin['url']+('.asc' if name=='nginx' else '.sig'),'official-proxy-source-selection')
        require(pin['key']['url']==('https://nginx.org/keys/pluknet.key' if name=='nginx' else 'https://keys.openpgp.org/vks/v1/by-fingerprint/'+fingerprint),'official-proxy-key-location')
        for row in [pin,pin['signature'],pin['key']]:require(re.fullmatch('[a-f0-9]{64}',row['sha256']),'proxy-source-digest')


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urllib.parse.urlparse(newurl)
        require(parsed.scheme=='https' and parsed.hostname in {'nginx.org','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com','keys.openpgp.org'},'proxy-source-redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class Work:
    def __init__(self,work):
        self.work=work;self.phase='initialize';self.sequence=0;self.total=0;self.deadline=time.monotonic()+1200
        spec=importlib.util.spec_from_file_location('proxy_process',ROOT/'gateway/fuzz/run.py')
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C','HOME':str(work),'PYTHONNOUSERSITE':'1','SOURCE_DATE_EPOCH':'1792886400'}
        self.opener=urllib.request.build_opener(HTTPSRedirect())
    def execute(self,args,phase,seconds=60,cwd=None,accepted=(0,)):
        self.phase=phase;self.sequence+=1;left=self.deadline-time.monotonic();require(left>0,'proxy-deadline')
        status,raw=self.runner.execute(args,self.work,str(self.sequence)+'-'+phase,self.env,min(seconds,left),cwd=cwd or self.work)
        require(status in accepted,'proxy-native-status');return status,raw
    def get(self,url,path=None,expected=None,maximum=16*1024*1024):
        self.phase='download';left=self.deadline-time.monotonic();require(left>0,'proxy-deadline')
        require(urllib.parse.urlparse(url).scheme=='https','proxy-https-source')
        req=urllib.request.Request(url,headers={'User-Agent':'nxrs-native-proxy-qualification','Accept':'application/vnd.github+json' if 'api.github.com' in url else '*/*'})
        with self.opener.open(req,timeout=min(45,left)) as source:
            require(source.status==200 and 'rel="next"' not in source.headers.get('Link',''),'complete-proxy-source-response')
            chunks=[];size=0
            while chunk:=source.read(65536):
                size+=len(chunk);self.total+=len(chunk);require(size<=maximum and self.total<=64*1024*1024 and time.monotonic()<self.deadline,'proxy-download-budget');chunks.append(chunk)
        raw=b''.join(chunks)
        if expected:require(native.digest(raw)==expected,'exact-proxy-source-bytes')
        if path is not None:
            with path.open('xb') as output:output.write(raw)
        return raw


def extract(archive,destination,root):
    target=destination/root;require(not os.path.lexists(target),'new-proxy-source-tree');seen=set();count=0;total=0
    with tarfile.open(archive,mode='r:*') as source:
        for member in source:
            count+=1;total+=member.size;require(count<=20000 and 0<=member.size<=128*1024*1024 and total<=128*1024*1024,'proxy-source-extraction-budget')
            name=member.name.rstrip('/') if member.isdir() else member.name;p=PurePosixPath(name)
            require(len(name)<=512 and p.parts and p.parts[0]==root and not p.is_absolute() and '..' not in p.parts
                    and p.as_posix()==name and '\\' not in name and name not in seen and (member.isdir() or member.isreg()),'canonical-regular-proxy-source')
            seen.add(name);out=destination/p
            if member.isdir():out.mkdir(mode=0o700,parents=True,exist_ok=True)
            else:
                out.parent.mkdir(mode=0o700,parents=True,exist_ok=True);incoming=source.extractfile(member);require(incoming is not None,'proxy-source-file')
                size=0
                with out.open('xb') as output:
                    while chunk:=incoming.read(65536):size+=len(chunk);require(size<=member.size,'proxy-source-file-size');output.write(chunk)
                require(size==member.size,'complete-proxy-source-file');out.chmod(0o700 if member.mode&0o111 else 0o600)
    require(count>0 and target.is_dir(),'complete-proxy-source-tree');return target


def signed_source(work,name,pin):
    archive=work.work/(name+'.tar');signature=work.work/(name+'.sig');armored=work.work/(name+'.key');keyring=work.work/(name+'.gpg')
    work.get(pin['url'],archive,pin['sha256']);work.get(pin['signature']['url'],signature,pin['signature']['sha256'],65536)
    work.get(pin['key']['url'],armored,pin['key']['sha256'],1024*1024)
    work.execute(['/usr/bin/gpg','--batch','--no-options','--homedir',str(work.work/'gnupg'),'--no-default-keyring','--output',str(keyring),'--dearmor',str(armored)],'dearmor-'+name)
    keyring.chmod(0o400)
    args=['/usr/bin/gpgv','--homedir',str(work.work/'gnupg'),'--keyring',str(keyring),'--weak-digest','SHA1','--status-fd','1',str(signature),str(archive)]
    _,raw=work.execute(args,'signature-'+name);payloads.signature(raw,pin['key']['fingerprint'])
    raw=archive.read_bytes();require(bool(raw),'proxy-signature-control-input');changed=work.work/(name+'-changed.tar');changed.write_bytes(bytes([raw[0]^1])+raw[1:])
    status,raw=work.execute(args[:-1]+[str(changed)],'changed-signature-'+name,accepted=(1,2))
    require(status!=0 and b'[GNUPG:] BADSIG ' in raw,'actual-proxy-signature-refusal')
    return extract(archive,work.work,pin['root']),dict(signer=pin['key']['fingerprint'],sha256=pin['sha256'],changed_archive_refused=True)


def configuration(pcre):
    return ['--prefix='+str(RUNTIME),'--sbin-path='+str(RUNTIME/'bin/nginx'),'--with-http_ssl_module','--without-http-cache',
            '--with-pcre='+str(pcre),'--with-pcre-opt=-O2 -fPIC -fstack-protector-strong -D_FORTIFY_SOURCE=2',
            '--with-cc-opt=-O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2',
            '--with-ld-opt=-Wl,-z,relro,-z,now']+['--without-http_'+n+'_module' for n in DISABLED]


def run(directory):
    native.hosted() # Before any file writes, subprocesses, network or privileged operation.
    require(os.uname().machine=='x86_64' and os.getuid()!=0,'unprivileged-amd64-proxy-qualification')
    require(not os.path.lexists(directory) and not os.path.lexists(RUNTIME),'fresh-proxy-runtime')
    policy=native.parse((ROOT/advisories.POLICY).read_bytes());validate_sources(policy)
    head=native.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    require(not native.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-proxy-source')
    before=inputs();directory.mkdir(mode=0o700);(directory/'gnupg').mkdir(mode=0o700);work=Work(directory)
    work.execute(['sudo','-n','install','-d','-m','0755','-o',str(os.getuid()),'-g',str(os.getgid()),str(RUNTIME)],'new-owned-proxy-prefix')
    report=RUNTIME/'qualification.json'
    result=dict(schema=1,scope=evidence.SCOPE,qualified=False,verification_status='pending',checkout=dict(clean=True,commit=head),inputs=before,sources=policy['sources'],
        full_native_advisory_closure=False,bootstrap_environment_qualified=False,signature_key_revocation_lifecycle_qualified=False,
        package_payload_authenticated=False,kernel_qualified=False,firmware_qualified=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)
    identity=native.save_report(report,result)
    try:
        n=advisories.nginx_articles(work.get(advisories.NGINX,maximum=512*1024));p=advisories.pcre_articles(work.get(advisories.PCRE,maximum=1024*1024))
        reviewed=advisories.review(policy,n,p)
        for component,value in [('nginx','1.30.5'),('pcre2','10.48')]:
            changed=copy.deepcopy(policy);changed['sources'][component]['version']=value
            try:advisories.review(changed,n,p)
            except native.Refusal as error:require(str(error) in {'nginx-upstream-fix-required','pcre-upstream-fix-required'},'proxy-version-control-category')
            else:raise native.Refusal('actual-older-proxy-version-refusal')
        sources={};signatures={}
        for name,pin in policy['sources'].items():sources[name],signatures[name]=signed_source(work,name,pin)
        flags=configuration(sources['pcre2']);work.execute([sources['nginx']/'configure',*flags],'configure',cwd=sources['nginx'])
        work.execute(['/usr/bin/make','-j2'],'build',900,cwd=sources['nginx'])
        binary=sources['nginx']/'objs/nginx';archive=sources['pcre2']/'.libs/libpcre2-8.a'
        require(archive.is_file(),'actual-static-pcre-archive')
        probe=directory/'pcre-probe'
        work.execute(['/usr/bin/cc','-O2','-I'+str(sources['pcre2']/'src'),ROOT/'tests/host/native/proxy_pcre.c',archive,'-o',probe],'build-pcre-probe')
        _,raw=work.execute([probe],'pcre-probe');require(raw.strip()==b'PCRE2 10.49 static probe: JIT disabled, route acceptance and refusals pass','actual-pcre-version-and-route-probe')
        (RUNTIME/'bin').mkdir(mode=0o755);shutil.copyfile(binary,RUNTIME/'bin/nginx');(RUNTIME/'bin/nginx').chmod(0o755);binary=RUNTIME/'bin/nginx'
        _,raw=work.execute([binary,'-v'],'version');require(raw.strip()==('nginx version: nginx/'+policy['sources']['nginx']['version']).encode(),'actual-proxy-version')
        libraries=native.dependencies(binary);require(libraries and all(n.as_posix().startswith('/usr/') and 'pcre' not in n.name for n in libraries),'exact-static-proxy-linkage')
        modules=re.findall(r'^    &(ngx_[a-z0-9_]+_module),$',(sources['nginx']/'objs/ngx_modules.c').read_text(),re.M)
        require(modules and len(modules)==len(set(modules)) and {'ngx_http_ssl_module','ngx_http_proxy_module','ngx_http_rewrite_module','ngx_http_limit_req_module','ngx_http_limit_conn_module'}<=set(modules),'required-proxy-modules')
        require(not any('ngx_http_'+n+'_module' in modules for n in DISABLED) and not any('http_v2' in n or 'http_v3' in n or 'mail' in n or 'stream' in n for n in modules),'no-unselected-proxy-protocols')
        harness=ROOT/'tools/gateway-proxy/boundary.py';_,raw=work.execute(['/usr/bin/python3',harness,'--nginx',binary,'--report',directory/'boundary.json'],'https-boundaries',90)
        boundary=native.parse((directory/'boundary.json').read_bytes());require(boundary==dict(tests=4,failures=0,skips=0,log_discard_qualified=True,log_configuration_refusals=2),'complete-native-proxy-boundaries')
        require(inputs()==before and native.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()==head and not native.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-proxy-inputs')
        result.update(qualified=True,verification_status='complete',binary_sha256=native.file_digest(binary),libraries={n.as_posix():native.file_digest(n) for n in sorted(libraries)},
            static_pcre_archive_sha256=native.file_digest(archive),source_signatures=signatures,changed_signature_refusals=2,source_version_refusals=2,
            pcre2_probe_passed=True,pcre2_version=policy['sources']['pcre2']['version'],pcre2_jit=False,advisory_review=reviewed,modules=modules,
            configuration_sha256=native.file_digest(sources['nginx']/'objs/ngx_auto_config.h'),link_makefile_sha256=native.file_digest(sources['nginx']/'objs/Makefile'),
            https_boundary_tests=boundary['tests'],https_boundary_failures=boundary['failures'],https_boundary_skips=boundary['skips'],log_discard_qualified=boundary['log_discard_qualified'],log_configuration_refusals=boundary['log_configuration_refusals'])
        evidence.admit(result,ROOT,head,before)
    except Exception as error:
        category=str(error) if isinstance(error,native.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        result.update(qualified=False,verification_status='failed',failure_phase=work.phase,failure_category=category);native.save_report(report,result,identity);raise
    native.save_report(report,result,identity)
    print('PASS: signed minimal proxy, fixed static PCRE2, actual TLS/routes/log-discard; complete release and native gates remain separate')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True);args=parser.parse_args()
    try:run(args.work)
    except Exception as error:
        category=str(error) if isinstance(error,native.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Minimal native proxy qualification refused ('+category+').\n')


if __name__=='__main__':main()
