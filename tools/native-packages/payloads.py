#!/usr/bin/env python3
"""Authenticate the candidate's mapped Ubuntu images; never install or extract.

The chain is a pinned archive signer -> signed Release -> Packages SHA-256 ->
exact .deb SHA-256/control identity -> selected regular payload bytes/metadata.
This checks the distribution subset, not custom sources or complete release trust.
All commands and downloaded metadata remain in a private disposable directory.
"""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import importlib.util
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
import urllib.request
import candidate

u = candidate.ubuntu
ROOT = u.ROOT
SCOPE = 'authenticated-mapped-ubuntu-native-image-payloads'
POLICY = 'security/ubuntu-archive-trust.json'
KEYRING = Path('/usr/share/keyrings/ubuntu-archive-keyring.gpg')
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_TAR = 512 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024


def policy(value, now):
    u.require(type(value['schema']) is int and value['schema'] == 1 and value['scope'] == 'hosted-ubuntu-24.04-amd64-mapped-native-payloads', 'payload-policy-scope')
    u.require(value['archive'] == 'https://archive.ubuntu.com/ubuntu/' and value['signer']=='F6ECB3762474EDA9D21B7022871920D1991BC93C', 'payload-authority')
    u.require(value['suites'] == ['noble-updates','noble-security','noble','noble-backports']
              and value['components'] == ['main','universe','restricted','multiverse'], 'payload-repository-selection')
    begin=datetime.fromisoformat(value['reviewed_at']);end=datetime.fromisoformat(value['valid_until'])
    u.require(begin.tzinfo is not None and end.tzinfo is not None and begin<=now<end
              and 0<(end-begin).total_seconds()<=31*86400, 'payload-policy-expired')
    u.require(type(value['rolling_metadata_max_age_seconds']) is int and value['rolling_metadata_max_age_seconds']==7*86400
              and type(value['future_tolerance_seconds']) is int and value['future_tolerance_seconds']==300, 'payload-freshness-policy')


def signature(status, expected):
    rows=[]
    for line in status.decode('utf-8').splitlines():
        if line.startswith('[GNUPG:] '): rows.append(line.removeprefix('[GNUPG:] ').split())
    u.require(not any(r and r[0] in {'BADSIG','ERRSIG','NO_PUBKEY','EXPSIG','EXPKEYSIG','REVKEYSIG','KEYREVOKED','NODATA'} for r in rows), 'archive-signature-refused')
    valid=[r for r in rows if r and r[0]=='VALIDSIG']
    u.require(len(valid)==1 and len(valid[0]) in (10,11), 'archive-valid-signature-required')
    row=valid[0];fingerprint=row[10] if len(row)==11 else row[1]
    u.require(fingerprint==expected and row[7]=='1' and row[8] in {'8','9','10'}, 'archive-signer-or-digest')
    return fingerprint


def release(raw, suite, trust, now):
    u.require(len(raw)<=2*1024*1024, 'release-budget')
    fields={};hashes={};section=None
    for line in raw.decode('utf-8').splitlines():
        u.require(len(line)<=4096, 'release-line-budget')
        if line.startswith(' '):
            if section=='SHA256':
                values=line.split();u.require(len(values)==3, 'release-hash-row')
                sha,size,name=values;u.require(re.fullmatch('[a-f0-9]{64}',sha) and size.isdecimal(), 'release-sha256')
                path=PurePosixPath(name);u.require(not path.is_absolute() and '..' not in path.parts and path.as_posix()==name and name not in hashes, 'release-index-path')
                hashes[name]=dict(sha256=sha,size=int(size))
        elif line:
            key,value=line.split(':',1);u.require(key not in fields, 'duplicate-release-field');fields[key]=value.strip();section=key
    u.require(fields['Origin']=='Ubuntu' and fields['Label']=='Ubuntu' and fields['Suite']==suite
              and fields['Codename']=='noble' and fields['Version']=='24.04' and 'amd64' in fields['Architectures'].split(), 'release-identity')
    when=parsedate_to_datetime(fields['Date']);u.require(when.tzinfo is not None, 'release-date-zone')
    age=(now-when).total_seconds();u.require(age>=-trust['future_tolerance_seconds'], 'future-release')
    # The immutable base Release is older than its rolling update pockets.
    if suite!='noble':u.require(age<=trust['rolling_metadata_max_age_seconds'], 'stale-rolling-release')
    if 'Valid-Until' in fields:u.require(now<=parsedate_to_datetime(fields['Valid-Until']), 'expired-release')
    wanted={component+'/binary-amd64/Packages.gz' for component in trust['components']}
    u.require(wanted<=set(hashes) and all(0<hashes[n]['size']<=MAX_ARCHIVE for n in wanted), 'complete-package-indexes')
    return hashes,when.isoformat()


def paragraphs(stream, maximum=MAX_TAR):
    fields={};previous=None;total=0;paragraph=0
    while True:
        line=stream.readline(32769)
        if not line:
            if fields:yield fields
            break
        total+=len(line);paragraph+=len(line)
        u.require(total<=maximum and len(line)<=32768 and paragraph<=512*1024, 'package-index-budget')
        line=line.rstrip(b'\n')
        if not line:
            if fields:yield fields
            fields={};previous=None;paragraph=0
        elif line.startswith((b' ',b'\t')):
            u.require(previous is not None and previous not in {'Package','Version','Architecture','Source','Filename','Size','SHA256'}, 'continued-package-identity')
        else:
            key,value=line.split(b':',1);key=key.decode('ascii')
            u.require(re.fullmatch('[A-Za-z][A-Za-z0-9-]*',key) and key.lower() not in {k.lower() for k in fields}, 'duplicate-package-field')
            fields[key]=value.strip();previous=key


def source_identity(value,name,version):
    if not value:return name,version
    match=re.fullmatch(r'([a-z0-9+.-]+)(?: \(([^ ()]+)\))?',value)
    u.require(match is not None, 'archive-source-identity')
    return u.package_name(match[1]),u.package_version(match[2] or version)


def package_record(row, selected):
    name=row['Package'].decode('ascii');version=row['Version'].decode('ascii');arch=row['Architecture'].decode('ascii')
    u.require(name==selected['name'] and version==selected['version'] and arch==selected['architecture'], 'exact-archive-package-version')
    source,source_version=source_identity(row.get('Source',b'').decode('ascii'),name,version)
    u.require(source==selected['source'] and source_version==selected['source_version'], 'exact-archive-source-version')
    sha=row['SHA256'].decode('ascii');size=row['Size'].decode('ascii');filename=row['Filename'].decode('ascii')
    path=PurePosixPath(filename)
    u.require(re.fullmatch('[a-f0-9]{64}',sha) and size.isdecimal() and 0<int(size)<=MAX_ARCHIVE, 'package-archive-sha256')
    u.require(not path.is_absolute() and '..' not in path.parts and path.as_posix()==filename
              and re.fullmatch(r'pool/[A-Za-z0-9+_./~:-]+\.deb',filename), 'package-archive-path')
    return dict(filename=filename,size=int(size),sha256=sha,name=name,version=version,architecture=arch,source=source,source_version=source_version)


def member_path(name,aliases,directory=False):
    if directory:name=name.rstrip('/')
    if name.startswith('./'):name=name[2:]
    if name in ('','.'):return None
    p=PurePosixPath(name)
    u.require(not p.is_absolute() and '..' not in p.parts and p.as_posix()==name and '\\' not in name and '\0' not in name, 'canonical-payload-member')
    parts=list(p.parts)
    if parts[0] in aliases:parts=list(PurePosixPath(aliases[parts[0]]).parts)+parts[1:]
    return '/'+PurePosixPath(*parts).as_posix()


def payload_members(stream, expected, aliases):
    found={};seen=set();total=0;count=0
    with tarfile.open(fileobj=stream,mode='r|') as archive:
        for member in archive:
            count+=1;total+=member.size
            u.require(count<=250000 and 0<=member.size<=MAX_TAR and total<=MAX_TAR, 'payload-member-budget')
            name=member_path(member.name,aliases,member.isdir())
            if name is None:
                u.require(member.isdir(), 'payload-root-directory');continue
            # Ambiguous aliases for a selected image cannot choose different bytes.
            if name in expected:
                u.require(name not in seen and member.isreg(), 'unique-regular-native-payload')
                seen.add(name);want=expected[name]
                u.require(member.uid==want['uid'] and member.gid==want['gid'] and member.mode&0o7777==want['mode']
                          and member.size==want['size'], 'native-payload-metadata')
                source=archive.extractfile(member);u.require(source is not None, 'native-payload-file')
                h=hashlib.sha256();length=0
                while chunk:=source.read(1024*1024):h.update(chunk);length+=len(chunk)
                u.require(length==member.size and h.hexdigest()==want['sha256'], 'native-payload-bytes')
                found[name]=dict(member=member.name,sha256=h.hexdigest(),mode=want['mode'],uid=want['uid'],gid=want['gid'],size=length)
    u.require(set(found)==set(expected), 'all-mapped-payloads-required')
    return found


def protected(path,directory=False):
    info=path.lstat();kind=stat.S_ISDIR if directory else stat.S_ISREG
    u.require(kind(info.st_mode) and info.st_uid==info.st_gid==0 and not info.st_mode&0o022, 'protected-distribution-path')
    return info


def stable_stat(info):
    # Reading may update atime; ownership, content and inode identity must stay fixed.
    return tuple(getattr(info,name) for name in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_size','st_mtime_ns','st_ctime_ns'))


def installed(path,sha):
    u.require(path.is_absolute() and path.as_posix().startswith('/usr/') and path.resolve()==path, 'canonical-installed-image')
    for parent in path.parents:protected(parent,True)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd);u.require(stat.S_ISREG(before.st_mode) and before.st_uid==before.st_gid==0 and not before.st_mode&0o022, 'protected-native-image')
        h=hashlib.sha256()
        while chunk:=os.read(fd,1024*1024):h.update(chunk)
        after=os.fstat(fd);u.require(stable_stat(before)==stable_stat(after) and h.hexdigest()==sha and stable_stat(path.lstat())==stable_stat(before), 'unchanged-mapped-image')
        return dict(sha256=sha,uid=before.st_uid,gid=before.st_gid,mode=stat.S_IMODE(before.st_mode),size=before.st_size)
    finally:os.close(fd)


def aliases():
    values={}
    for old,new in {'bin':'usr/bin','sbin':'usr/sbin','lib':'usr/lib','lib64':'usr/lib64'}.items():
        path=Path('/'+old)
        if path.is_symlink():
            u.require(path.lstat().st_uid==0 and os.readlink(path) in (new,'/'+new) and path.resolve()==Path('/'+new), 'qualified-usrmerge-alias')
            protected(Path('/'+new),True);values[old]=new
    return values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Evidence:
    def __init__(self,work):
        self.work=work;self.logs=work/'logs';self.logs.mkdir(mode=0o700)
        spec=importlib.util.spec_from_file_location('payload_process',ROOT/'gateway/fuzz/run.py')
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.deadline=time.monotonic()+900;self.total=0;self.sequence=0
        self.opener=urllib.request.build_opener(NoRedirect())
    def command(self,args,phase,seconds=60):
        self.sequence+=1
        remaining=self.deadline-time.monotonic();u.require(remaining>0, 'payload-deadline')
        return self.runner.execute(args,self.logs,str(self.sequence)+'-'+phase,{'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C','PYTHONNOUSERSITE':'1'},min(seconds,remaining),cwd=self.work)
    def get(self,url,path,maximum,expected=None):
        remaining=self.deadline-time.monotonic();u.require(remaining>0, 'payload-deadline')
        request=urllib.request.Request(url,headers={'User-Agent':'nxrs-native-payload-verification'})
        with self.opener.open(request,timeout=min(30,remaining)) as response,path.open('xb') as output:
            u.require(response.status==200 and response.geturl()==url, 'exact-archive-response')
            h=hashlib.sha256();size=0
            while chunk:=response.read(1024*1024):
                size+=len(chunk);self.total+=len(chunk)
                u.require(size<=maximum and self.total<=MAX_TOTAL and time.monotonic()<self.deadline, 'archive-download-budget')
                h.update(chunk);output.write(chunk)
        if expected:archive_identity(size,h.hexdigest(),expected)
        return dict(sha256=h.hexdigest(),size=size)


def archive_identity(size,sha256,expected):
    u.require(size==expected['size'] and sha256==expected['sha256'], 'archive-digest')


def changed_archive_control(archive,expected):
    # Exercise the actual admission predicate using one changed byte in this .deb.
    h=hashlib.sha256();size=0
    with archive.open('rb') as source:
        first=source.read(1);u.require(bool(first), 'archive-control-input')
        h.update(bytes([first[0]^1]));size=1
        while chunk:=source.read(1024*1024):h.update(chunk);size+=len(chunk)
    try:archive_identity(size,h.hexdigest(),expected)
    except u.Refusal as error:
        u.require(str(error)=='archive-digest', 'archive-control-category');return
    raise u.Refusal('actual-changed-archive-control')


def verify_release(evidence,trust,suite,now):
    folder=evidence.work/suite;folder.mkdir(mode=0o700)
    signed=folder/'InRelease';clear=folder/'Release'
    downloaded=evidence.get(trust['archive']+'dists/'+suite+'/InRelease',signed,2*1024*1024)
    args=['/usr/bin/gpgv','--homedir',str(evidence.work/'gnupg'),'--keyring',str(KEYRING),'--weak-digest','SHA1','--status-fd','1','--output',str(clear),str(signed)]
    status,raw=evidence.command(args,'signature');u.require(status==0, 'archive-signature-status');signature(raw,trust['signer'])
    hashes,date=release(clear.read_bytes(),suite,trust,now)
    original=signed.read_bytes();u.require(original.count(b'Origin: Ubuntu')==1, 'signature-control-input')
    bad=folder/'tampered-InRelease';bad.write_bytes(original.replace(b'Origin: Ubuntu',b'Origin: Ubuntv',1))
    changed=args[:-1]+[str(bad)];changed[changed.index('--output')+1]=str(folder/'tampered-Release')
    status,raw=evidence.command(changed,'bad-signature')
    u.require(status!=0 and b'[GNUPG:] BADSIG ' in raw, 'actual-bad-signature-control')
    return folder,hashes,dict(suite=suite,inrelease_sha256=downloaded['sha256'],release_sha256=u.file_digest(clear),date=date,signer=trust['signer'],bad_signature_refused=True)


def authenticate(evidence,trust,inventory):
    groups={};expected={};resolved=aliases();records={};metadata=[];indexes=[]
    for name,row in inventory['files'].items():
        if row.get('origin')=='pinned-source':continue
        owner=row['binary_package'];u.require(owner in inventory['packages'], 'payload-owner-set')
        expected[name]=installed(Path(name),row['sha256']);groups.setdefault(owner,{})[name]=expected[name]
    u.require(set(groups)==set(inventory['packages']) and 1<=len(groups)<=256, 'complete-distribution-payload-owners')
    pending=set(groups);now=datetime.now(timezone.utc)
    for suite in trust['suites']:
        folder,hashes,proof=verify_release(evidence,trust,suite,now);metadata.append(proof)
        for component in trust['components']:
            index=component+'/binary-amd64/Packages.gz';path=folder/(component+'-Packages.gz')
            evidence.get(trust['archive']+'dists/'+suite+'/'+index,path,MAX_ARCHIVE,hashes[index])
            index_proof=dict(suite=suite,index=index,**hashes[index],release_sha256=proof['release_sha256']);indexes.append(index_proof)
            with gzip.open(path,'rb') as source:
                for row in paragraphs(source):
                    name=row.get('Package',b'').decode('ascii')
                    for owner in tuple(pending):
                        selected=inventory['packages'][owner]
                        if name==selected['name'] and row.get('Version',b'').decode('ascii')==selected['version'] and row.get('Architecture',b'').decode('ascii')==selected['architecture']:
                            records[owner]=dict(**package_record(row,selected),index=index_proof);pending.remove(owner)
            if not pending:break
        if not pending:break
    u.require(not pending, 'all-exact-package-versions-authenticated')
    matched={};controls=0
    for index,owner in enumerate(sorted(records)):
        record=records[owner];archive=evidence.work/('package-'+str(index)+'.deb')
        evidence.get(trust['archive']+record['filename'],archive,MAX_ARCHIVE,record)
        changed_archive_control(archive,record);controls+=1
        status,raw=evidence.command(['/usr/bin/dpkg-deb','--field',str(archive),'Package','Version','Architecture','Source'],'control')
        u.require(status==0, 'package-control-status');control=list(paragraphs(io.BytesIO(raw),1024*1024));u.require(len(control)==1, 'one-package-control')
        row=control[0];selected=inventory['packages'][owner]
        u.require(row['Package'].decode()==selected['name'] and row['Version'].decode()==selected['version'] and row['Architecture'].decode()==selected['architecture']
            and source_identity(row.get('Source',b'').decode(),selected['name'],selected['version'])==(selected['source'],selected['source_version']), 'authenticated-deb-control-identity')
        tar=evidence.work/'payload.tar'
        script=('import os,resource,sys\nfd=os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)\nos.dup2(fd,1);os.close(fd)\n'
                'resource.setrlimit(resource.RLIMIT_FSIZE,('+str(MAX_TAR)+','+str(MAX_TAR)+'))\nos.execv("/usr/bin/dpkg-deb",["dpkg-deb","--fsys-tarfile",sys.argv[1]])\n')
        status,_=evidence.command(['/usr/bin/python3','-c',script,str(archive),str(tar)],'payload-stream',120)
        u.require(status==0 and tar.stat().st_size<=MAX_TAR, 'bounded-deb-payload')
        with tar.open('rb') as source:found=payload_members(source,groups[owner],resolved)
        for name,value in found.items():matched[name]=dict(binary_package=owner,archive_sha256=record['sha256'],**value)
        tar.unlink();archive.unlink()
    u.require(set(matched)==set(expected), 'complete-mapped-distribution-payloads')
    for name,value in expected.items():u.require(installed(Path(name),value['sha256'])==value, 'stable-authenticated-images')
    return dict(signed_releases=metadata,package_indexes=indexes,packages=records,files=matched,usrmerge_aliases=resolved,
                bad_signature_refusals=len(metadata),changed_archive_refusals=controls)


def run(candidate_path,work,report):
    u.hosted() # Before filesystem changes, tools or network; never a personal host.
    u.require(not os.path.lexists(work) and not os.path.lexists(report), 'fresh-payload-work-and-report')
    raw,parent=candidate.read(candidate_path);head=u.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip();inputs=candidate.source_inputs()
    u.require(type(parent['schema']) is int and parent['schema']==1 and parent['scope']==candidate.SCOPE and parent['advisory_status']=='complete' and type(parent['qualified']) is bool
              and parent['checkout']==dict(clean=True,commit=head) and parent['inputs']==inputs, 'complete-current-candidate-for-payloads')
    u.require(isinstance(parent['findings'],list) and len(parent['findings'])<=2048 and parent['qualified']==(not parent['findings']), 'consistent-candidate-advisory-result')
    u.require(not u.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(), 'clean-payload-checkout')
    for owner,value in parent['inventory']['packages'].items():u.require(u.package(owner)==value, 'current-payload-package-identity')
    trust=u.parse((ROOT/POLICY).read_bytes());policy(trust,datetime.now(timezone.utc));protected(KEYRING)
    work.mkdir(mode=0o700);(work/'gnupg').mkdir(mode=0o700);evidence=Evidence(work)
    result=dict(schema=1,scope=SCOPE,qualified=False,verification_status='pending',checkout=dict(clean=True,commit=head),inputs=inputs,
        candidate_report_sha256=u.digest(raw),archive_trust_policy_sha256=u.file_digest(ROOT/POLICY),archive_keyring_sha256=u.file_digest(KEYRING),
        distribution_payloads_authenticated=False,package_payload_authenticated=False,custom_source_payload_authenticated=False,
        advisory_gate_qualified=parent['qualified'],advisory_findings=len(parent['findings']),full_native_advisory_closure=False,
        signature_key_revocation_lifecycle_qualified=False,bootstrap_environment_qualified=False,post_install_state_qualified=False,
        kernel_qualified=False,firmware_qualified=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)
    identity=u.save_report(report,result)
    try:
        proof=authenticate(evidence,trust,parent['inventory'])
        for owner,value in parent['inventory']['packages'].items():u.require(u.package(owner)==value, 'unchanged-payload-package-identity')
        for name,root in candidate.resources().items():
            u.require(u.file_digest(root)==parent['inventory']['roots'][name]['sha256']
                and sorted(p.as_posix() for p in u.dependencies(root))==parent['inventory']['roots'][name]['libraries'], 'unchanged-payload-root-and-links')
        u.require(candidate.source_inputs()==inputs and candidate.read(candidate_path)[0]==raw and u.file_digest(KEYRING)==result['archive_keyring_sha256']
                  and head==u.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
                  and not u.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(), 'unchanged-payload-proof-inputs')
        result.update(qualified=True,verification_status='complete',distribution_payloads_authenticated=True,**proof)
    except Exception as error:
        category=str(error) if isinstance(error,u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        result.update(qualified=False,verification_status='failed',failure_category=category);u.save_report(report,result,identity);raise
    u.save_report(report,result,identity);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--candidate',type=Path,required=True);parser.add_argument('--work',type=Path,required=True);parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    try:run(args.candidate,args.work,args.report)
    except Exception as error:
        category=str(error) if isinstance(error,u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Native package payload authentication refused ('+category+').\n')
    print('PASS: mapped Ubuntu payload bytes authenticated; advisory and complete release gates remain separate')


if __name__=='__main__':main()
