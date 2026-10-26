#!/usr/bin/env python3
"""Guest-only experiment driver and service; never a production admission API."""
import hashlib,importlib.util,json,os,re,stat,sys
from pathlib import Path
import package_boot_contract as c

BASE=Path('/var/lib/package-boot');CODE=Path('/usr/libexec/package-boot')
LIBDIR=Path('/usr/lib/x86_64-linux-gnu');LINK=LIBDIR/'libpcre2-8.so.0'
UNIT='package-boot-consumer.service'
stage='context'
last_native=None

def private(path,maximum):
    with open(path,'rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        before=os.fstat(source.fileno())
        c.require(stat.S_ISREG(before.st_mode) and before.st_uid==0 and before.st_mode&0o022==0
                  and before.st_nlink==1 and before.st_size<=maximum,'owned-guest-file')
        raw=source.read(maximum+1);after=os.fstat(source.fileno())
        c.require(len(raw)<=maximum and (before.st_dev,before.st_ino,before.st_mtime_ns,before.st_ctime_ns,before.st_size)==
                  (after.st_dev,after.st_ino,after.st_mtime_ns,after.st_ctime_ns,after.st_size),'unchanged-guest-file')
    return raw

def atomic(path,raw,mode=0o600):
    temporary=path.with_name('.'+path.name+'.pending')
    with open(temporary,'xb') as output:
        os.chmod(temporary,mode);output.write(raw);output.flush();os.fsync(output.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def context():
    c.require(sys.platform=='linux' and os.getuid()==0,'guest-root-required')
    case,nonce=c.command_case(Path('/proc/cmdline').read_text().strip())
    c.require(Path('/proc/1/comm').read_text().strip()=='systemd','real-systemd-pid1')
    raw=private(BASE/'plan.json',16384);plan=c.validate_plan(c.parse(raw))
    c.require(plan['nonce']==nonce and c.sha(c.encoded(plan)+b'\n')==c.sha(raw),'exact-owned-guest-plan')
    c.require(c.sha((CODE/'guest.py').read_bytes())==plan['guest_sha256'] and
              c.sha((CODE/'runner.py').read_bytes())==plan['runner_sha256'] and
              c.sha((CODE/'package_boot_contract.py').read_bytes())==plan['contract_sha256'],'exact-guest-code')
    c.require(c.sha(Path('/proc/1/exe').read_bytes())==plan['pid1_sha256'] and
              os.uname().release==plan['kernel_release'] and sorted(p.name for p in Path('/sys/class/net').iterdir())==['lo'],
              'isolated-exact-kernel-and-pid1')
    return case,plan

def execute(args,phase,accepted=(0,),seconds=60):
    global last_native
    c.require(phase in c.COMMANDS,'fixed-guest-native-phase');last_native=phase
    case,_=context();work=BASE/case/'commands';work.mkdir(parents=True,exist_ok=True)
    spec=importlib.util.spec_from_file_location('owned_guest_process',CODE/'runner.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    label=phase+'-'+str(len(list(work.iterdir())))
    status,raw=module.execute(args,work,label,{'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C','HOME':str(BASE)},seconds,cwd=work)
    c.require(status in accepted,'guest-command-'+phase)
    last_native=None
    return raw

def status():
    return execute(['/usr/bin/dpkg-query','-W','-f=${Status}\t${Version}\t${Architecture}','libpcre2-8-0'],'package-state').decode().strip()

def admit(plan):
    for path in [BASE,CODE,LIBDIR]:
        item=path.lstat();c.require(stat.S_ISDIR(item.st_mode) and item.st_uid==0 and item.st_mode&0o022==0,'protected-guest-parent')
    # Check bytes before dpkg-query, which can itself load PCRE2.
    c.require(LINK.is_symlink() and LINK.lstat().st_uid==0 and LINK.readlink()==Path(plan['library_name']),'selected-owned-package-image')
    c.require(c.sha(private(LIBDIR/plan['library_name'],4*1024*1024))==plan['library_sha256'],'unchanged-package-image')
    c.require(status()=='install ok installed\t'+plan['package_version']+'\tamd64','configured-approved-package')

def archive(plan,name):
    expected=plan['package_sha256'] if name=='candidate.deb' else plan['vendor_archive_sha256']
    c.require(c.sha(private(BASE/name,4*1024*1024))==expected,'exact-recovery-archive')
    return str(BASE/name)

def encrypted(plan,case):
    global stage
    stage='volume';work=BASE/case;volume=work/'volume';key=work/'key';wrong=work/'wrong-key'
    for path in [key,wrong]:
        with open(path,'xb') as output:os.chmod(path,0o600);output.write(os.urandom(32))
    with open(volume,'xb') as output:os.chmod(volume,0o600);output.truncate(64*1024*1024)
    mount=Path('/run/package-boot-volume');mount.mkdir(mode=0o700,exist_ok=True)
    mapping='package-boot-volume';device=Path('/dev/mapper')/mapping
    c.require(not device.exists(),'new-owned-guest-mapping')
    opened=False;mounted=False
    try:
        execute(['/usr/sbin/modprobe','loop'],'loop-module')
        execute(['/usr/sbin/modprobe','dm_crypt'],'dm-crypt-module')
        execute(['/usr/sbin/cryptsetup','luksFormat','--batch-mode','--type','luks2','--pbkdf','pbkdf2','--pbkdf-force-iterations','1000',
                 '--key-file',str(key),str(volume)],'format-volume')
        execute(['/usr/sbin/cryptsetup','open','--key-file',str(key),str(volume),mapping],'open-volume');opened=True
        execute(['/usr/sbin/mkfs.ext4','-q',str(device)],'format-encrypted-fs')
        execute(['/usr/bin/mount',str(device),str(mount)],'mount-encrypted-fs');mounted=True
        data=c.payload(plan['nonce'],case)
        with open(mount/'owned-data','xb') as output:output.write(data);output.flush();os.fsync(output.fileno())
        execute(['/usr/bin/umount',str(mount)],'unmount-written-fs');mounted=False
        execute(['/usr/sbin/cryptsetup','close',mapping],'close-written-volume');opened=False
        execute(['/usr/sbin/cryptsetup','open','--key-file',str(wrong),str(volume),mapping],'wrong-volume-key',accepted=(2,))
        c.require(not device.exists(),'wrong-key-created-no-mapping')
        execute(['/usr/sbin/cryptsetup','open','--key-file',str(key),str(volume),mapping],'reopen-volume');opened=True
        execute(['/usr/bin/mount','-o','ro',str(device),str(mount)],'mount-restored-fs');mounted=True
        c.require((mount/'owned-data').read_bytes()==data,'encrypted-data-readback')
        execute(['/usr/bin/umount',str(mount)],'unmount-restored-fs');mounted=False
        execute(['/usr/sbin/cryptsetup','close',mapping],'join-restored-volume');opened=False
        c.require(not device.exists(),'owned-mapping-joined')
        with open(volume,'rb') as source:
            tail=b''
            while chunk:=source.read(1024*1024):
                c.require(data not in tail+chunk,'no-plaintext-volume-copy');tail=(tail+chunk)[-len(data):]
        return c.sha(data)
    finally:
        if mounted:execute(['/usr/bin/umount',str(mount)],'cleanup-owned-mount')
        if opened:execute(['/usr/sbin/cryptsetup','close',mapping],'cleanup-owned-mapping')
        for path in [volume,key,wrong]:path.unlink(missing_ok=True)

def consume(case,plan):
    global stage
    stage='admission';admit(plan);stage='consumer'
    raw=execute([str(CODE/'probe'),str(LIBDIR/plan['library_name'])],'packaged-interpreter-jit')
    c.require(raw.strip()==b'PCRE2 10.49 shared probe: exact image, Unicode/JIT and ordinary matching pass','actual-packaged-probe')
    for program in ['/usr/bin/systemctl','/usr/sbin/cryptsetup']:
        raw=execute(['/usr/bin/ldd',program],'actual-consumer-linkage')
        paths=re.findall(rb'^\s*libpcre2-8\.so\.0 => (/[^\s]+) \(0x[0-9a-f]+\)$',raw,re.M)
        c.require(len(paths)==1 and Path(paths[0].decode()).resolve()==LIBDIR/plan['library_name'],'actual-consumer-package-image')
        execute([program,'--version'],'actual-packaged-command')
    payload=encrypted(plan,case)
    atomic(BASE/case/'consumer.json',c.encoded(dict(nonce=plan['nonce'],case=case,payload_sha256=payload,mapping_joined=True))+b'\n')

def driver(case,plan):
    global stage
    index=c.CASES.index(case);stage='ledger';ledger=BASE/'ledger.json'
    if index==0:c.require(not ledger.exists(),'fresh-first-boot-ledger');previous=None
    else:
        previous=c.parse(private(ledger,1024))
        c.require(set(previous)=={'nonce','sequence','boot_id_sha256'} and previous['nonce']==plan['nonce'] and previous['sequence']==index,
                  'persisted-prior-boot-generation')
    work=BASE/case;c.require(not work.exists(),'new-boot-case-storage');work.mkdir(mode=0o700)
    stage='admission';negative=case in c.NEGATIVE
    if negative:
        execute(['/usr/bin/systemctl','start',UNIT],'refused-start',accepted=(1,))
        raw=execute(['/usr/bin/systemctl','show',UNIT,'-p','ActiveState','-p','Result','-p','MainPID'],'refused-unit-state')
        fields=dict(line.split('=',1) for line in raw.decode().splitlines())
        c.require(fields==dict(ActiveState='failed',Result='exit-code',MainPID='0') and not (work/'consumer.json').exists(),
                  'real-prestart-refusal-no-consumer')
        if case=='unconfigured':execute(['/usr/bin/dpkg','--configure','-a'],'recover-configure',seconds=90)
        else:execute(['/usr/bin/dpkg','--install',archive(plan,'candidate.deb')],'recover-candidate',seconds=90)
        execute(['/usr/bin/systemctl','reset-failed',UNIT],'reset-recovered-unit')
    stage='consumer';execute(['/usr/bin/systemctl','start',UNIT],'run-consumer',seconds=150)
    result=c.parse(private(work/'consumer.json',1024))
    c.require(result==dict(nonce=plan['nonce'],case=case,payload_sha256=c.sha(c.payload(plan['nonce'],case)),mapping_joined=True),'fresh-consumer-result')
    stage='prepare';admit(plan)
    if case=='installed-again':
        execute(['/usr/bin/dpkg','--unpack',archive(plan,'candidate.deb')],'prepare-unconfigured',seconds=90)
        c.require(status()=='install ok unpacked\t'+plan['package_version']+'\tamd64','actual-unconfigured-next-boot')
    elif case=='unconfigured':
        execute(['/usr/bin/dpkg','--install',archive(plan,'vendor.deb')],'prepare-vendor',seconds=90)
        c.require(status()=='install ok installed\t'+plan['vendor_version']+'\tamd64','actual-vendor-next-boot')
    elif case=='vendor':
        altered=private(BASE/'changed-library.fixture',4*1024*1024)
        c.require(c.sha(altered)==plan['changed_library_sha256'],'exact-nonloaded-change-fixture')
        atomic(LIBDIR/plan['library_name'],altered,0o644)
    stage='ledger';boot=Path('/proc/sys/kernel/random/boot_id').read_bytes().strip();machine=Path('/etc/machine-id').read_bytes().strip()
    c.require(re.fullmatch(rb'[a-f0-9-]{36}',boot) and re.fullmatch(rb'[a-f0-9]{32}',machine),'actual-kernel-and-machine-identities')
    boot_digest=c.sha(boot);c.require(previous is None or previous['boot_id_sha256']!=boot_digest,'fresh-independent-kernel')
    atomic(ledger,c.encoded(dict(nonce=plan['nonce'],sequence=index+1,boot_id_sha256=boot_digest))+b'\n')
    report=dict(schema=1,scope=c.SCOPE,case=case,nonce=plan['nonce'],plan_sha256=c.sha(c.encoded(plan)+b'\n'),sequence=index+1,
        boot_id_sha256=boot_digest,machine_id_sha256=c.sha(machine),previous_boot_id_sha256=None if previous is None else previous['boot_id_sha256'],
        kernel_release=os.uname().release,pid1_sha256=plan['pid1_sha256'],network_devices=['lo'],admission_refused=negative,
        refusal_result='exit-code' if negative else None,consumer_passed=True,encrypted_payload_sha256=result['payload_sha256'],
        mapped_device_joined=True,prepared_next_state=c.NEXT[index],ledger_committed=True)
    c.validate(report,plan,case)
    with open('/dev/console','w') as console:console.write('PACKAGE_BOOT_REPORT '+c.encoded(report).decode()+'\n');console.flush()

def main():
    # No shutdown or mutation is possible until the dedicated guest context is
    # proven. A personal-host invocation fails before the finally block.
    case,plan=context();mode=sys.argv[1] if len(sys.argv)==2 else ''
    c.require(mode in ['driver','admit','consume'],'explicit-guest-role')
    if mode!='driver':
        try:admit(plan) if mode=='admit' else consume(case,plan)
        except Exception:sys.exit(2)
        return
    try:driver(case,plan)
    except Exception:
        with open('/dev/console','w') as console:
            console.write('PACKAGE_BOOT_FAILURE '+stage+'\n')
            if last_native in c.COMMANDS:console.write('PACKAGE_BOOT_COMMAND '+last_native+'\n')
            console.flush()
    finally:
        os.sync();os.execv('/usr/bin/systemctl',['systemctl','poweroff'])

if __name__=='__main__':main()
