#!/usr/bin/env python3
"""Assemble and install the source-qualified PCRE2 candidate in a fresh Ubuntu root.

This is a guarded release-packaging qualification, never a personal-host installer.
Unpack/configure recovery is not power-loss or system-boot qualification.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tarfile
import time

import pcre

u,e=pcre.ubuntu,pcre.evidence
ROOT=u.ROOT
PACKAGE='libpcre2-8-0'
VERSION='10.49-0nxrs1'
LIBDIR=Path('usr/lib/x86_64-linux-gnu')
DOC=Path('usr/share/doc')/PACKAGE
INPUTS=(*e.INPUTS,'tools/native-packages/pcre_package.py',
        'tests/host/test_pcre_package.py','.github/workflows/gateway-pcre-package.yml',
        'tools/native-packages/README.md')
FLAGS=('full_native_advisory_closure','full_source_build_reproducible','cold_boot_qualified',
       'power_loss_qualified','protected_rollback_qualified','whole_os_replacement_qualified',
       'release_signature_lifecycle_qualified','physical_qualified','deployment_permitted')
SCOPE='isolated-ubuntu-pcre2-package-install-recovery'
CONTROLS=('package_assembly_reproducible','archive_inventory_verified',
          'old_package_refused','damaged_archive_refused','unconfigured_package_refused',
          'unpack_configuration_recovered','reinstallation_verified',
          'package_owned_library_verified','actual_consumers_verified',
          'encrypted_header_and_key_controls','vendor_rollback_refused',
          'candidate_recovery_verified','changed_library_refused','changed_library_repaired')

def require(value,category):u.require(value,category)

def inputs():return {name:u.file_digest(ROOT/name) for name in INPUTS}

def hosted(work,source_work):
    u.hosted() # Before writes, downloads, subprocesses or privileged operations.
    require(os.getuid()!=0 and os.uname().machine=='x86_64','unprivileged-amd64-package-qualification')
    temporary=Path(os.environ.get('RUNNER_TEMP','/nonexistent-runner-temp')).resolve(strict=True)
    require(temporary.is_dir() and temporary!=Path('/'),'runner-temporary-directory')
    for path in (work,source_work):
        require(path.is_absolute() and path==path.resolve() and temporary in path.parents
                and len(path.relative_to(temporary).parts)==1,'owned-runner-package-path')
    require(not os.path.lexists(work) and source_work.is_dir(),'fresh-package-work-and-existing-source')

def libc_dependency(raw):
    # This exact recipe must need only the normal GNU C runtime.
    needed=re.findall(rb'\(NEEDED\).*?\[([^\]]+)\]',raw)
    require(needed==[b'libc.so.6'],'pcre-only-libc-dependency')
    versions={tuple(map(int,v.split(b'.'))) for v in re.findall(rb'\bGLIBC_([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b',raw)}
    require(versions and all(v[0]==2 for v in versions) and b'GLIBC_PRIVATE' not in raw,'public-glibc-requirements')
    return 'libc6 (>= '+'.'.join(map(str,max(versions)))+')'

def license_bytes(archive,pin):
    require(u.file_digest(archive)==pin['sha256'],'exact-source-license-archive')
    with tarfile.open(archive,'r:*') as source:
        rows=[v for v in source.getmembers() if v.name==pin['root']+'/LICENCE.md']
        require(len(rows)==1 and rows[0].isreg() and 0<rows[0].size<=65536,'signed-source-license')
        raw=source.extractfile(rows[0]).read(65537)
    require(len(raw)==rows[0].size and b'BSD' in raw and b'Copyright' in raw,'complete-source-license')
    return raw

def stage(directory,library,qualification,license_text,dependency):
    require(not directory.exists(),'fresh-package-stage')
    require(re.fullmatch(r'libpcre2-8\.so\.0\.[0-9]+\.[0-9]+',library.name),'package-library-name')
    directory.mkdir(mode=0o755)
    files={LIBDIR/library.name:library.read_bytes(),DOC/'copyright':license_text,
           DOC/'source-qualification.json':e.encoded(qualification)+b'\n'}
    for name,raw in files.items():
        path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw);path.chmod(0o644)
    (directory/LIBDIR/'libpcre2-8.so.0').symlink_to(library.name)
    control=directory/'DEBIAN';control.mkdir(mode=0o755)
    (control/'control').write_text('Package: '+PACKAGE+'\nVersion: '+VERSION+'\nArchitecture: amd64\n'
        'Source: nxrs-pcre2\nSection: libs\nPriority: optional\nMulti-Arch: same\n'
        'Maintainer: nxrs contributors <nxrs@invalid.example>\nDepends: '+dependency+'\n'
        'Description: nxrs qualification candidate for the PCRE2 8-bit runtime\n'
        ' Signed upstream source; isolated installation qualification, not a vendor release.\n')
    (control/'triggers').write_text('activate-noawait ldconfig\n')
    (control/'shlibs').write_text('libpcre2-8 0 '+PACKAGE+' (>= '+VERSION+')\n')
    (control/'md5sums').write_text(''.join(hashlib.md5(raw).hexdigest()+'  '+str(name)+'\n' for name,raw in sorted(files.items())))
    for path in directory.rglob('*'):
        if not path.is_symlink():path.chmod(0o755 if path.is_dir() else 0o644)
    return files

def inventory(raw,files,library):
    require(len(raw)<=4*1024*1024,'bounded-package-filesystem')
    seen=set();regular={};links={}
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
        for member in archive:
            name=member.name.rstrip('/')
            if name=='.':continue
            require(name.startswith('./') and '..' not in Path(name).parts and name not in seen,'canonical-unique-package-path')
            seen.add(name)
            require(member.uid==member.gid==0 and member.mode==(0o755 if member.isdir() else 0o777 if member.issym() else 0o644),
                    'root-owned-package-permissions')
            if member.isdir():continue
            path=Path(name[2:])
            if member.isreg():
                require(path in files and member.size==len(files[path]),'exact-package-regular-file')
                regular[path]=archive.extractfile(member).read(4*1024*1024)
            elif member.issym():links[path]=member.linkname
            else:raise u.Refusal('regular-package-files-and-soname-only')
    require(regular==files and links=={LIBDIR/'libpcre2-8.so.0':library.name},'complete-package-file-inventory')

def changed_fixture(data):
    # Change only a byte outside every ELF load segment. Recovery tools can
    # themselves load PCRE2; this control must not alter their instructions.
    require(len(data)>=64 and data[:7]==b'\x7fELF\x02\x01\x01' and data[-1:]==b'\0','owned-elf-trailer-fixture')
    offset=struct.unpack_from('<Q',data,32)[0]
    size,count=struct.unpack_from('<HH',data,54)
    require(size==56 and 1<=count<=128 and offset>=64 and offset+size*count<=len(data),'bounded-elf-program-headers')
    loads=[]
    for index in range(count):
        kind,flags,start,address,physical,length,memory,alignment=struct.unpack_from('<IIQQQQQQ',data,offset+index*size)
        require(start+length<=len(data),'bounded-elf-segment')
        if kind==1:loads.append((start,length))
    require(loads and all(not start<=len(data)-1<start+length for start,length in loads),'nonloaded-trailer-fixture')
    return data[:-1]+b'\1'

def installed_status(raw):
    require(raw.decode().strip()=='install ok installed\t'+VERSION+'\tamd64','configured-candidate-package-required')

def validate(value):
    require(set(value)=={'schema','scope','checkout','inputs','package','source_qualification_sha256','library_sha256','controls',*FLAGS},
            'classified-public-package-receipt')
    require(type(value['schema']) is int and value['schema']==1 and value['scope']==SCOPE,'package-receipt-schema')
    require(set(value['checkout'])=={'commit','clean'} and re.fullmatch('[a-f0-9]{40}',value['checkout']['commit'])
            and value['checkout']['clean'] is True,'clean-package-source')
    require(value['inputs']==inputs(),'exact-package-source-inputs')
    package=value['package']
    require(set(package)=={'name','version','architecture','sha256','dependency'} and
            (package['name'],package['version'],package['architecture'])==(PACKAGE,VERSION,'amd64')
            and re.fullmatch(r'libc6 \(>= 2\.[0-9]+(?:\.[0-9]+)?\)',package['dependency']),'package-identity')
    for digest in [package['sha256'],value['source_qualification_sha256'],value['library_sha256']]:
        require(re.fullmatch('[a-f0-9]{64}',digest),'package-receipt-digest')
    require(isinstance(value['controls'],dict) and set(value['controls'])==set(CONTROLS)
            and all(value['controls'][name] is True for name in CONTROLS),'complete-package-controls')
    require(all(value[name] is False for name in FLAGS),'package-qualification-scope')

def run(directory,source_work):
    hosted(directory,source_work)
    head=u.command(['git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    require(not u.command(['git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-package-checkout')
    before=inputs();proof=e.verify(ROOT,head)
    source_raw=e.private_read(e.RUNTIME/'qualification.json',32768)
    require(u.digest(source_raw)==proof['qualification_sha256'],'exact-public-package-source-receipt')
    q=u.parse(source_raw);e.validate(q,ROOT,head)
    library=e.RUNTIME/q['library'];raw=e.private_read(library,4*1024*1024)
    require(u.digest(raw)==q['library_sha256'],'exact-qualified-package-library')
    license_text=license_bytes(source_work/'pcre2.tar',q['source'])
    directory.mkdir(mode=0o700);work=pcre_source_work(directory);work.deadline=time.monotonic()+2100
    _,dynamic=work.execute(['/usr/bin/readelf','--dynamic','--version-info','--wide',library],'package-libc-requirements')
    dependency=libc_dependency(dynamic)
    packages=[]
    for name in ['first','second']:
        staged=directory/name;files=stage(staged,library,q,license_text,dependency)
        package=directory/(name+'.deb')
        work.execute(['/usr/bin/dpkg-deb','--root-owner-group','--uniform-compression','-Zxz','--threads-max=1','--build',staged,package],name+'-package')
        work.execute(['/usr/bin/dpkg-deb','--fsys-tarfile',package],name+'-inventory')
        # The bounded process runner returns only its last64KiB; the private
        # command log contains the complete bounded binary stream. Never parse
        # a tail as an archive and never publish this log.
        archive=(directory/(str(work.sequence)+'-'+name+'-inventory.log')).read_bytes()
        inventory(archive,files,library)
        packages.append(package)
    require(u.file_digest(packages[0])==u.file_digest(packages[1]),'reproducible-package-assembly')
    root=directory/'root'
    # Only paths below this new private directory are changed by root commands.
    work.execute(['sudo','-n','install','-d','-m','0755',root/'usr/sbin'],'new-isolated-root')
    policy=directory/'policy-rc.d';policy.write_text('#!/bin/sh\nexit 101\n');policy.chmod(0o755)
    work.execute(['sudo','-n','install','-m','0755',policy,root/'usr/sbin/policy-rc.d'],'deny-chroot-service-starts')
    work.execute(['sudo','-n','/usr/bin/timeout','--signal=KILL','1140','/usr/sbin/debootstrap','--arch=amd64','--variant=minbase','--merged-usr',
        '--include=cryptsetup-bin,systemd,libpcre2-8-0','--extra-suites=noble-updates,noble-security',
        '--keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg','--force-check-sig',
        'noble',root,'https://archive.ubuntu.com/ubuntu'],'signed-clean-ubuntu-root',1200)
    require(root.resolve()==root,'unchanged-isolated-root')
    def inside(args,phase,accepted=(0,),seconds=90):
        return work.execute(['sudo','-n','/usr/bin/timeout','--signal=KILL',str(seconds-5),'/usr/sbin/chroot',root,*args],phase,seconds,accepted=accepted)
    def query():return inside(['/usr/bin/dpkg-query','-W','-f=${Status}\t${Version}\t${Architecture}',PACKAGE],'package-status')[1]
    old_status=query();old_version=old_status.decode().split('\t')[1]
    require(u.compare(old_version,'lt',VERSION),'older-clean-root-vendor-package')
    old_link=root/LIBDIR/'libpcre2-8.so.0';old_name=old_link.readlink()
    require(not old_name.is_absolute() and len(old_name.parts)==1 and
            re.fullmatch(r'libpcre2-8\.so\.0\.[0-9]+\.[0-9]+',str(old_name)),'clean-root-vendor-library')
    old_hash=u.file_digest(root/LIBDIR/old_name)
    old_packages=list((root/'var/cache/apt/archives').glob('libpcre2-8-0_*_amd64.deb'))
    require(len(old_packages)==1,'retained-vendor-rollback-package')
    rollback=directory/'vendor.deb';shutil.copyfile(old_packages[0],rollback)
    work.execute(['sudo','-n','install','-m','0644',rollback,root/'tmp/vendor.deb'],'stage-vendor-rollback')
    work.execute(['sudo','-n','install','-m','0644',packages[0],root/'tmp/candidate.deb'],'stage-candidate-package')
    work.execute(['sudo','-n','install','-m','0755',source_work/'probe',root/'tmp/pcre-probe'],'stage-ordinary-probe')
    try:installed_status(old_status)
    except u.Refusal:pass
    else:raise u.Refusal('old-package-admitted')
    damaged=directory/'damaged.deb';damaged.write_bytes(packages[0].read_bytes()[:64])
    work.execute(['sudo','-n','install','-m','0644',damaged,root/'tmp/damaged.deb'],'stage-damaged-package')
    inside(['/usr/bin/dpkg','--install','/tmp/damaged.deb'],'damaged-package-refusal',accepted=(1,2))
    require(query()==old_status and u.file_digest(root/LIBDIR/old_name)==old_hash,'damaged-package-preserves-installed-state')
    inside(['/usr/bin/dpkg','--unpack','/tmp/candidate.deb'],'candidate-unpack')
    state=query();require(state.decode().split('\t')[0]=='install ok unpacked','actual-unconfigured-package-boundary')
    try:installed_status(state)
    except u.Refusal:pass
    else:raise u.Refusal('unconfigured-package-admitted')
    inside(['/usr/bin/dpkg','--configure','-a'],'recover-package-configuration')
    target=root/LIBDIR/library.name
    def verified_image():
        # Inspect bytes before invoking dpkg-query: its own dependency graph
        # may also load PCRE2 through libselinux.
        require(old_link.is_symlink() and old_link.readlink()==Path(library.name)
                and not target.is_symlink() and stat.S_ISREG(target.stat().st_mode)
                and target.stat().st_uid==0 and target.stat().st_mode&0o022==0
                and u.file_digest(target)==q['library_sha256'],'package-owned-selected-image')
        installed_status(query())
        _,owner=inside(['/usr/bin/dpkg-query','-S','/'+str(LIBDIR/library.name)],'actual-package-file-owner')
        require(owner.decode().strip() in [PACKAGE+': /'+str(LIBDIR/library.name),PACKAGE+':amd64: /'+str(LIBDIR/library.name)],'unique-candidate-file-owner')
    def operations():
        verified_image()
        _,probe=inside(['/tmp/pcre-probe','/'+str(LIBDIR/library.name)],'actual-packaged-interpreter-jit')
        require(probe.strip()==b'PCRE2 10.49 shared probe: exact image, Unicode/JIT and ordinary matching pass','packaged-ordinary-probe')
        for program in ['/usr/sbin/cryptsetup','/usr/bin/systemctl']:
            _,raw=inside(['/usr/bin/ldd',program],'packaged-system-tool-image')
            paths=re.findall(rb'^\s*libpcre2-8\.so\.0 => (/[^\s]+) \(0x[0-9a-f]+\)$',raw,re.M)
            require(len(paths)==1 and (root/paths[0].decode().lstrip('/')).resolve()==target.resolve(),
                    'actual-packaged-consumer-linkage')
            inside([program,'--version'],'packaged-system-tool-version')
        require(u.file_digest(target)==q['library_sha256'],'unchanged-packaged-consumer-image')
    operations()
    inside(['/usr/bin/dpkg','--install','/tmp/candidate.deb'],'idempotent-package-reinstall');operations()
    _,verified=inside(['/usr/bin/dpkg','--verify',PACKAGE],'verify-package-files');require(not verified.strip(),'verified-candidate-md5sums')
    _,audit=inside(['/usr/bin/dpkg','--audit'],'audit-isolated-package-database');require(not audit.strip(),'consistent-package-database')
    key=directory/'owned-key';key.write_bytes(os.urandom(32));key.chmod(0o600)
    wrong=directory/'wrong-key';wrong.write_bytes(os.urandom(32));wrong.chmod(0o600)
    for path in [key,wrong]:work.execute(['sudo','-n','install','-m','0600',path,root/'tmp'/path.name],'stage-owned-volume-key')
    volume=directory/'volume';volume.touch();volume.chmod(0o600)
    with volume.open('r+b') as output:output.truncate(32*1024*1024)
    work.execute(['sudo','-n','install','-m','0600',volume,root/'tmp/volume'],'stage-owned-volume')
    # Small KDF budget is a named synthetic case, never a product configuration.
    inside(['/usr/sbin/cryptsetup','luksFormat','--batch-mode','--type','luks2','--pbkdf','pbkdf2',
        '--pbkdf-force-iterations','1000','--key-file','/tmp/owned-key','/tmp/volume'],'packaged-actual-luks-format')
    inside(['/usr/sbin/cryptsetup','open','--test-passphrase','--key-file','/tmp/owned-key','/tmp/volume'],'packaged-accepted-volume-key')
    inside(['/usr/sbin/cryptsetup','open','--test-passphrase','--key-file','/tmp/wrong-key','/tmp/volume'],'packaged-refused-volume-key',accepted=(2,))
    inside(['/usr/sbin/cryptsetup','luksHeaderBackup','/tmp/volume','--header-backup-file','/tmp/header.backup'],'packaged-header-backup')
    inside(['/usr/sbin/cryptsetup','luksHeaderRestore','--batch-mode','/tmp/volume','--header-backup-file','/tmp/header.backup'],'packaged-header-restore')
    inside(['/usr/sbin/cryptsetup','open','--test-passphrase','--key-file','/tmp/owned-key','/tmp/volume'],'packaged-restored-volume-key')
    inside(['/usr/bin/dpkg','--install','/tmp/vendor.deb'],'explicit-vendor-rollback')
    require(query()==old_status and u.file_digest(root/LIBDIR/old_name)==old_hash,'exact-vendor-rollback')
    try:verified_image()
    except u.Refusal:pass
    else:raise u.Refusal('vendor-rollback-admitted')
    inside(['/usr/bin/dpkg','--install','/tmp/candidate.deb'],'reapply-qualified-package');operations()
    # Alter a closed owned image, refuse it before executing any consumer, then
    # repair using the exact package. No process uses the image during mutation.
    changed=directory/'changed-library';changed.write_bytes(changed_fixture(target.read_bytes()))
    work.execute(['sudo','-n','install','-m','0644',changed,target],'owned-image-change')
    try:verified_image()
    except u.Refusal:pass
    else:raise u.Refusal('changed-package-image-admitted')
    inside(['/usr/bin/dpkg','--install','/tmp/candidate.deb'],'repair-changed-image');operations()
    require(before==inputs() and e.verify(ROOT,head)==proof
            and head==u.command(['git','-C',str(ROOT),'rev-parse','HEAD']).strip()
            and not u.command(['git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-package-qualification-source')
    result=dict(schema=1,scope=SCOPE,checkout=dict(commit=head,clean=True),inputs=before,
        package=dict(name=PACKAGE,version=VERSION,architecture='amd64',sha256=u.file_digest(packages[0]),dependency=dependency),
        source_qualification_sha256=proof['qualification_sha256'],library_sha256=q['library_sha256'],
        controls=dict.fromkeys(CONTROLS,True),**dict.fromkeys(FLAGS,False))
    validate(result);raw=e.encoded(result)+b'\n';require(len(raw)<=16384,'bounded-public-package-receipt')
    (directory/'qualification.json').write_bytes(raw)
    (directory/'source-qualification.json').write_bytes(source_raw)
    print('PACKAGE PASS: signed-source PCRE2; reproducible archive assembly; isolated dpkg ownership, unpack recovery, rollback refusal, repaired image and actual encrypted-header controls')
    print('PACKAGE SCOPE: clean Ubuntu userspace, not system boot, power loss, protected rollback, whole OS or production release')

def phase_commands(work):
    original=work.execute
    def execute(args,phase,*values,**options):
        require(re.fullmatch('[a-z0-9-]{1,60}',phase),'classified-package-phase')
        try:return original(args,phase,*values,**options)
        except Exception as error:
            # A fixed phase identifies the failing operation without exporting
            # native output, command arguments, paths, keys or build logs.
            raise u.Refusal('package-phase-'+phase) from error
    work.execute=execute
    return work

def pcre_source_work(directory):
    import importlib.util
    sys.path.insert(0,str(ROOT/'tools/gateway-proxy'))
    spec=importlib.util.spec_from_file_location('package_source_tools',ROOT/'tools/gateway-proxy/qualify.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return phase_commands(module.Work(directory))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',type=Path,required=True);parser.add_argument('--source-work',type=Path,required=True)
    args=parser.parse_args()
    try:run(args.work,args.source_work)
    except Exception as error:
        category=str(error) if isinstance(error,u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Package qualification refused ('+category+').\n')

if __name__=='__main__':main()
