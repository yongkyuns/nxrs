#!/usr/bin/env python3
"""Boot only an owned generated package image on disposable hosted Ubuntu."""
import argparse,hashlib,json,os,re,secrets,stat,sys,time
from pathlib import Path
import pcre_package as p

ROOT=p.ROOT
sys.path.insert(0,str(ROOT/'tests/host'))
import package_boot_contract as c

INPUTS=(*p.INPUTS,'tools/native-packages/package_boot.py','gateway/tests/package_boot_guest.py',
        'tests/host/package_boot_contract.py','tests/host/test_package_boot.py','.github/workflows/gateway-package-boot.yml')
FLAGS=('full_native_advisory_closure','full_source_build_reproducible','cold_boot_qualified',
       'power_loss_qualified','protected_rollback_qualified','whole_os_replacement_qualified',
       'verified_boot_chain_qualified','production_admission_qualified','release_signature_lifecycle_qualified',
       'emulator_security_qualified','kernel_qualified','firmware_qualified','bootstrap_environment_qualified','physical_qualified','fips_validated','deployment_permitted')
IMAGE_BYTES=4*1024*1024*1024
GUEST=ROOT/'gateway/tests/package_boot_guest.py'
CONTRACT=ROOT/'tests/host/package_boot_contract.py'
RUNNER=ROOT/'gateway/fuzz/run.py'
BIOS=Path('/usr/share/seabios/bios-256k.bin')
QEMU=Path('/usr/bin/qemu-system-x86_64')

CONSUMER='[Unit]\nDescription=Owned package boot qualification consumer\nAfter=local-fs.target\n[Service]\nType=oneshot\nExecStartPre=/usr/bin/python3 /usr/libexec/package-boot/guest.py admit\nExecStart=/usr/bin/python3 /usr/libexec/package-boot/guest.py consume\nTimeoutStartSec=150\nKillMode=control-group\n'
DRIVER='[Unit]\nDescription=Owned package boot qualification controller\nAfter=local-fs.target\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/libexec/package-boot/guest.py driver\nTimeoutStartSec=210\nKillMode=control-group\n[Install]\nWantedBy=multi-user.target\n'

def require(value,category):p.require(value,category)
def inputs():return {name:p.u.file_digest(ROOT/name) for name in INPUTS}

def owned_image(path):
    item=path.lstat()
    require(stat.S_ISREG(item.st_mode) and item.st_uid==os.getuid() and item.st_nlink==1
            and item.st_mode&0o077==0 and item.st_size==IMAGE_BYTES,'owned-private-regular-boot-image')

def qemu_command(image,kernel,initrd,case,nonce):
    require(case in c.CASES and re.fullmatch('[a-f0-9]{32}',nonce),'explicit-virtual-boot-case')
    return [QEMU,'-machine','q35,accel=tcg','-cpu','max','-smp','2','-m','1024',
        '-nodefaults','-display','none','-serial','stdio','-monitor','none','-no-reboot','-nic','none',
        '-bios',BIOS,'-kernel',kernel,'-initrd',initrd,
        '-append','root=/dev/vda rw console=ttyS0 quiet loglevel=3 panic=1 systemd.show_status=no systemd.log_level=warning qual_case='+case+' qual_nonce='+nonce,
        '-blockdev',json.dumps(dict(driver='file',filename=str(image),**{'node-name':'rootfile','cache':{'direct':False,'no-flush':False}})),
        '-blockdev','driver=raw,node-name=rootdisk,file=rootfile','-device','virtio-blk-pci,drive=rootdisk,romfile=',
        '-object','rng-random,id=entropy,filename=/dev/urandom','-device','virtio-rng-pci,rng=entropy']

def serial_report(raw,plan,case):
    require(len(raw)<=1024*1024,'bounded-private-guest-console')
    failure=re.findall(rb'^PACKAGE_BOOT_FAILURE ([a-z]+)\r?$',raw,re.M)
    if failure:
        stage=failure[-1].decode();require(stage in c.STAGES,'classified-guest-stage')
        commands=re.findall(rb'^PACKAGE_BOOT_COMMAND ([a-z-]+)\r?$',raw,re.M)
        if commands:
            command=commands[-1].decode();require(command in c.COMMANDS,'classified-guest-command')
            raise p.u.Refusal('guest-command-'+command)
        raise p.u.Refusal('guest-failure-'+stage)
    rows=re.findall(rb'^PACKAGE_BOOT_REPORT (\{[^\r\n]+\})\r?$',raw,re.M)
    require(len(rows)==1 and len(rows[0])<=8192,'one-complete-guest-report')
    return c.validate(c.parse(rows[0]),plan,case)

def validate(value):
    fields={'schema','scope','checkout','inputs','package_qualification_sha256','source_qualification_sha256',
            'package_sha256','library_sha256','kernel','emulator','fixture_plan','image_bytes','vcpus','memory_mib',
            'initial_image_sha256','boots','virtual_cold_boots_qualified',*FLAGS}
    require(set(value)==fields and type(value['schema']) is int and value['schema']==1 and value['scope']==c.SCOPE,'classified-public-virtual-boot-receipt')
    require(value['inputs']==inputs() and value['checkout']['clean'] is True and
            set(value['checkout'])=={'commit','clean'} and re.fullmatch('[a-f0-9]{40}',value['checkout']['commit']),'exact-clean-boot-source')
    plan=c.validate_plan(value['fixture_plan'])
    require(value['package_sha256']==plan['package_sha256'] and value['library_sha256']==plan['library_sha256'],'boot-package-image-binding')
    for name in ['package_qualification_sha256','source_qualification_sha256','initial_image_sha256']:
        require(re.fullmatch('[a-f0-9]{64}',value[name]),'virtual-boot-provenance-digests')
    require(value['image_bytes']==IMAGE_BYTES and type(value['image_bytes']) is int and type(value['vcpus']) is int and value['vcpus']==2 and type(value['memory_mib']) is int and value['memory_mib']==1024
            and value['virtual_cold_boots_qualified'] is True,'qualified-virtual-resource-case')
    require(all(value[flag] is False for flag in FLAGS),'virtual-boot-scope-limits')
    kernel=value['kernel'];emulator=value['emulator']
    require(set(kernel)=={'release','sha256','initramfs_sha256','package'} and kernel['release']==plan['kernel_release'],'exact-guest-kernel')
    require(set(emulator)=={'binary_sha256','bios_sha256','package'},'exact-emulator-identity')
    for digest in [kernel['sha256'],kernel['initramfs_sha256'],emulator['binary_sha256'],emulator['bios_sha256']]:
        require(re.fullmatch('[a-f0-9]{64}',digest),'boot-runtime-digest')
    record=kernel['package'];require(set(record)=={'name','version','architecture','source','source_version'}
        and record['name']=='linux-image-'+kernel['release'] and record['architecture']=='amd64','installed-kernel-package')
    for name in ['name','source']:p.u.package_name(record[name])
    for name in ['version','source_version']:p.u.package_version(record[name])
    tool=emulator['package']
    require(set(tool)=={'name','version','architecture','source','source_version','purl'} and tool['name']=='qemu-system-x86'
            and tool['architecture']=='amd64','hosted-emulator-package')
    for name in ['name','source']:p.u.package_name(tool[name])
    for name in ['version','source_version']:p.u.package_version(tool[name])
    require(tool['purl']=='pkg:deb/ubuntu/'+tool['name']+'@'+tool['version'].replace(':','%3A')+'?arch=amd64&distro=noble','public-emulator-package-origin')
    records=value['boots'];require(isinstance(records,list) and len(records)==5,'five-complete-boot-cases')
    guest=[];previous=value['initial_image_sha256']
    extra={'image_before_sha256','image_after_sha256','filesystem_clean','emulator_exited'}
    for record in records:
        require(set(record)==c.FIELDS|extra and record['image_before_sha256']==previous and
                re.fullmatch('[a-f0-9]{64}',record['image_after_sha256']) and record['filesystem_clean'] is True
                and record['emulator_exited'] is True,'joined-persistent-disk-chain')
        guest.append({name:record[name] for name in c.FIELDS});previous=record['image_after_sha256']
    c.aggregate(guest,plan)

def run(directory,package_work):
    p.hosted(directory,package_work) # Before writes, native, privilege or VM operations.
    head=p.u.command(['git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    require(not p.u.command(['git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-virtual-boot-checkout')
    before=inputs();package_raw=(package_work/'qualification.json').read_bytes();package=p.u.parse(package_raw);p.validate(package)
    source_raw=(package_work/'source-qualification.json').read_bytes();source=p.u.parse(source_raw);p.e.validate(source,ROOT,head)
    require(package['checkout']==source['checkout']==dict(commit=head,clean=True) and
            p.u.digest(source_raw)==package['source_qualification_sha256'] and package['library_sha256']==source['library_sha256'],
            'verified-package-before-virtual-boot')
    require(package_work.resolve()==package_work and (package_work/'root').is_dir(),'owned-generated-guest-root')
    directory.mkdir(mode=0o700);work=p.pcre_source_work(directory);work.deadline=time.monotonic()+2400
    root=package_work/'root'
    def inside(args,phase,seconds=90):
        limit=min(seconds,int(work.deadline-time.monotonic())-5);require(limit>=10,'virtual-boot-operation-deadline')
        return work.execute(['sudo','-n','/usr/bin/timeout','--signal=KILL',str(limit-5),'/usr/bin/unshare','--mount','--propagation','private',
            '/bin/sh','-c','mount -t proc proc "$1/proc"; shift; exec chroot "$@"','owned-guest-preparation',root,root,*args],phase,limit)
    config=directory/'initramfs.conf';config.write_text('MODULES=most\nBUSYBOX=auto\nCOMPRESS=gzip\n')
    work.execute(['sudo','-n','install','-D','-m','0644',config,root/'etc/initramfs-tools/initramfs.conf'],'guest-initramfs-config')
    apt=['/usr/bin/env','DEBIAN_FRONTEND=noninteractive','/usr/bin/apt-get','-o','Acquire::Retries=1',
         '-o','Acquire::https::Timeout=30','-o','APT::Update::Error-Mode=any','-o','Acquire::AllowInsecureRepositories=false',
         '-o','APT::Get::AllowUnauthenticated=false']
    inside([*apt,'update'],'guest-signed-kernel-index',300)
    inside([*apt,'install','-y','--no-install-recommends','linux-image-virtual','initramfs-tools','busybox-initramfs',
            'python3','systemd-sysv','udev','e2fsprogs','kmod','dmsetup'],'guest-signed-kernel-install',900)
    _,init=inside(['/usr/bin/readlink','-f','/sbin/init'],'guest-real-pid1-entry')
    require(init.strip()==b'/usr/lib/systemd/systemd','installed-systemd-init-entry')
    kernels=list((root/'boot').glob('vmlinuz-*'));require(len(kernels)==1 and not kernels[0].is_symlink(),'one-installed-guest-kernel')
    release=kernels[0].name.removeprefix('vmlinuz-');require(re.fullmatch('[0-9][a-z0-9.+-]{1,95}',release),'public-installed-kernel-release')
    initrd=root/'boot'/('initrd.img-'+release);require(initrd.is_file() and not initrd.is_symlink(),'generated-guest-initramfs')
    _,record=inside(['/usr/bin/dpkg-query','-W','-f=${Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\t${db:Status-Status}',
                     'linux-image-'+release],'guest-kernel-package')
    rows=record.decode().split('\t');require(len(rows)==6 and rows[-1]=='installed','configured-guest-kernel')
    kernel_package=dict(zip(['name','version','architecture','source','source_version'],rows[:5]))
    for original,target in [(kernels[0],directory/'kernel'),(initrd,directory/'initrd')]:
        work.execute(['sudo','-n','install','-m','0600','-o',str(os.getuid()),'-g',str(os.getgid()),original,target],'copy-private-guest-boot-file')
    standard=root/p.LIBDIR/(Path(source['library']).name)
    require(p.u.file_digest(standard)==package['library_sha256'],'kernel-install-retains-approved-library')
    _,state=inside(['/usr/bin/dpkg-query','-W','-f=${Status}\t${Version}\t${Architecture}',p.PACKAGE],'approved-package-after-kernel-install');p.installed_status(state)
    altered=directory/'changed-library.fixture';altered.write_bytes(p.changed_fixture(standard.read_bytes()))
    plan=dict(nonce=secrets.token_hex(16),kernel_release=release,pid1_sha256=p.u.file_digest(root/'usr/lib/systemd/systemd'),
        guest_sha256=p.u.file_digest(GUEST),runner_sha256=p.u.file_digest(RUNNER),contract_sha256=p.u.file_digest(CONTRACT),
        package_version=p.VERSION,package_sha256=package['package']['sha256'],library_name=standard.name,library_sha256=package['library_sha256'],
        changed_library_sha256=p.u.file_digest(altered),vendor_version=package['vendor_package']['version'],vendor_archive_sha256=package['vendor_package']['archive_sha256'])
    c.validate_plan(plan);policy=directory/'plan.json';policy.write_bytes(c.encoded(plan)+b'\n')
    consumer=directory/'consumer.service';consumer.write_text(CONSUMER)
    driver=directory/'driver.service';driver.write_text(DRIVER)
    fstab=directory/'fstab';fstab.write_text('/dev/vda / ext4 defaults 0 1\n')
    empty=directory/'empty';empty.write_bytes(b'')
    staged=[(GUEST,'usr/libexec/package-boot/guest.py'),(CONTRACT,'usr/libexec/package-boot/package_boot_contract.py'),
        (RUNNER,'usr/libexec/package-boot/runner.py'),(root/'tmp/pcre-probe','usr/libexec/package-boot/probe'),
        (package_work/'first.deb','var/lib/package-boot/candidate.deb'),(package_work/'vendor.deb','var/lib/package-boot/vendor.deb'),
        (altered,'var/lib/package-boot/changed-library.fixture'),(policy,'var/lib/package-boot/plan.json'),
        (consumer,'etc/systemd/system/package-boot-consumer.service'),(driver,'etc/systemd/system/package-boot-driver.service'),
        (fstab,'etc/fstab'),(empty,'etc/machine-id')]
    # The source-build path is explicit in this named workflow, not discovered
    # by platform identity or a production provider API.
    for original,target in staged:
        mode='0755' if target.endswith('/probe') else '0644'
        work.execute(['sudo','-n','install','-D','-m',mode,original,root/target],'stage-owned-guest-fixture')
    work.execute(['sudo','-n','install','-d','-m','0755',root/'etc/systemd/system/multi-user.target.wants'],'guest-fixture-target')
    work.execute(['sudo','-n','ln','-s','../package-boot-driver.service',root/'etc/systemd/system/multi-user.target.wants/package-boot-driver.service'],
                 'enable-owned-guest-driver')
    for unit in ['serial-getty@ttyS0.service','getty@tty1.service']:
        work.execute(['sudo','-n','ln','-s','/dev/null',root/'etc/systemd/system'/unit],'disable-guest-login-fixture')
    # Only known synthetic files from the parent fixture are retired; no image,
    # log, seed, key, header or package archive is uploaded by this experiment.
    for name in ['owned-key','wrong-key','volume','header.backup','candidate.deb','vendor.deb','pcre-probe','damaged.deb']:
        work.execute(['sudo','-n','rm','-f','--',root/'tmp'/name],'retire-parent-private-fixture')
    mountpoints=[line.split()[4].replace('\\040',' ') for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    require(not any(name==str(root) or name.startswith(str(root)+'/') for name in mountpoints),'no-host-mounts-below-guest-root')
    image=directory/'root.img'
    with open(image,'xb') as output:os.chmod(image,0o600);output.truncate(IMAGE_BYTES)
    owned_image(image)
    limit=min(180,int(work.deadline-time.monotonic())-5);require(limit>=10,'private-image-deadline')
    work.execute(['sudo','-n','/usr/bin/timeout','--signal=KILL',str(limit-5),'/usr/sbin/mkfs.ext4','-q','-F','-d',root,image],
                 'assemble-owned-private-root-image',limit)
    initial=p.u.file_digest(image);boots=[]
    for case in c.CASES:
        owned_image(image);image_before=p.u.file_digest(image)
        phase='virtual-boot-'+case
        try:work.execute(qemu_command(image,directory/'kernel',directory/'initrd',case,plan['nonce']),phase,300)
        except Exception:
            private_log=directory/(str(work.sequence)+'-'+phase+'.log')
            raw=p.diagnostic_tail(private_log)
            if b'PACKAGE_BOOT_FAILURE ' in raw:serial_report(raw,plan,case)
            raise
        raw=(directory/(str(work.sequence)+'-'+phase+'.log')).read_bytes()
        report=serial_report(raw,plan,case)
        work.execute(['/usr/sbin/e2fsck','-f','-n',image],'check-shutdown-filesystem',90)
        _,header=work.execute(['/usr/sbin/dumpe2fs','-h',image],'check-clean-filesystem-state')
        require(re.findall(rb'^Filesystem state:\s*(.+)$',header,re.M)==[b'clean'],'orderly-guest-filesystem-shutdown')
        boots.append(dict(report,image_before_sha256=image_before,image_after_sha256=p.u.file_digest(image),filesystem_clean=True,emulator_exited=True))
    c.aggregate([{name:row[name] for name in c.FIELDS} for row in boots],plan)
    require(before==inputs() and head==p.u.command(['git','-C',str(ROOT),'rev-parse','HEAD']).strip()
            and not p.u.command(['git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-virtual-boot-source')
    result=dict(schema=1,scope=c.SCOPE,checkout=dict(commit=head,clean=True),inputs=before,
        package_qualification_sha256=p.u.digest(package_raw),source_qualification_sha256=p.u.digest(source_raw),
        package_sha256=package['package']['sha256'],library_sha256=package['library_sha256'],fixture_plan=plan,
        kernel=dict(release=release,sha256=p.u.file_digest(directory/'kernel'),initramfs_sha256=p.u.file_digest(directory/'initrd'),package=kernel_package),
        emulator=dict(binary_sha256=p.u.file_digest(QEMU),bios_sha256=p.u.file_digest(BIOS),package=p.u.package(p.u.owner(QEMU))),
        image_bytes=IMAGE_BYTES,vcpus=2,memory_mib=1024,initial_image_sha256=initial,boots=boots,virtual_cold_boots_qualified=True,
        **dict.fromkeys(FLAGS,False))
    validate(result);raw=c.encoded(result)+b'\n';require(len(raw)<=32768,'bounded-public-virtual-boot-receipt')
    (directory/'qualification.json').write_bytes(raw)
    (directory/'package-qualification.json').write_bytes(package_raw);(directory/'source-qualification.json').write_bytes(source_raw)
    print('VIRTUAL PACKAGE BOOT PASS: five fresh kernels; real PID1 startup admission, persistent recovery and mapped encrypted data; device/release gates remain open')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--package-work',type=Path,required=True);args=parser.parse_args()
    try:run(args.work,args.package_work)
    except Exception as error:
        category=str(error) if isinstance(error,p.u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Virtual package boot refused ('+category+').\n')

if __name__=='__main__':main()
