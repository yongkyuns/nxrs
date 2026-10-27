#!/usr/bin/env python3
"""Four fresh kernels around the complete encrypted gateway service group.

Only disposable hosted Ubuntu may prepare the private root and run this VM.
No NIC, host filesystem share, monitor, physical device or production key is
attached. Public output is one strict bounded JSON receipt, never image/logs.
"""
import argparse,hashlib,json,os,re,secrets,stat,sys,time
from pathlib import Path
import package_boot as package_boot
import guest_source

p=package_boot.p;ROOT=p.ROOT
sys.path.insert(0,str(ROOT/'gateway/tests'));sys.path.insert(0,str(ROOT/'gateway/tools'))
import service_boot_units as units
import encrypted_service_reference as service
import gateway_media_evidence,gateway_proxy_evidence,stage_proxy
import system_xml_evidence,service_boot_xml as xml

IMAGE_BYTES=8*1024*1024*1024
FLAGS=('device_tls_qualified','vpn_qualified','production_key_custody_qualified','power_loss_qualified',
       'physical_qualified','verified_boot_chain_qualified','protected_rollback_qualified',
       'whole_os_qualified','full_native_advisory_closure','fips_validated','deployment_permitted')
EXTRA_INPUTS=('tools/native-packages/service_boot.py','tools/native-packages/guest_source.py',
    'gateway/tests/service_boot_guest.py','gateway/tests/service_boot_units.py','gateway/tests/service_boot_proxy.py',
    'tests/host/test_guest_source.py','tests/host/test_service_boot_guest.py','tests/host/test_service_boot_units.py',
    'tests/host/test_service_boot_proxy.py','tests/host/test_service_boot.py','.github/workflows/gateway-service-boot.yml',
    'gateway/tests/service_boot_mount_reference.py','tests/host/test_service_boot_mount_reference.py',
    'gateway/tests/service_boot_xml.py','tests/host/test_service_boot_xml.py',
    '.github/workflows/gateway-service-boot-order.yml')
SCOPE='hosted-generated-encrypted-gateway-four-boots'

def require(value,category):p.require(value,category)
def inputs():return {name:p.u.file_digest(ROOT/name) for name in sorted(set(package_boot.INPUTS)|set(service.source_inputs())|set(EXTRA_INPUTS))}

def owned_image(path):
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1 and
            info.st_mode&0o077==0 and info.st_size==IMAGE_BYTES,'owned-private-service-boot-image')

def qemu_command(image,kernel,initrd,case,nonce):
    require(case in units.CASES and re.fullmatch('[a-f0-9]{32}',nonce),'named-service-boot-case')
    return [package_boot.QEMU,'-machine','q35,accel=tcg','-cpu','max','-smp','4','-m','2048',
        '-nodefaults','-display','none','-serial','stdio','-monitor','none','-no-reboot','-nic','none',
        '-bios',package_boot.BIOS,'-kernel',kernel,'-initrd',initrd,
        '-append','root=/dev/vda rw console=ttyS0 quiet loglevel=3 panic=1 systemd.show_status=no systemd.log_level=warning '
        'service_case='+case+' service_nonce='+nonce,
        '-blockdev',json.dumps(dict(driver='file',filename=str(image),**{'node-name':'rootfile','cache':{'direct':False,'no-flush':False}})),
        '-blockdev','driver=raw,node-name=rootdisk,file=rootfile','-device','virtio-blk-pci,drive=rootdisk,romfile=',
        '-object','rng-random,id=entropy,filename=/dev/urandom','-device','virtio-rng-pci,rng=entropy']

def validate_guest(value,plan,case):
    false=('production_key_custody_qualified','device_tls_qualified','vpn_qualified','power_loss_qualified',
        'physical_qualified','verified_boot_chain_qualified','protected_rollback_qualified','fips_validated','deployment_permitted')
    fields={'schema','scope','case','sequence','nonce','plan_sha256','boot_id_sha256','machine_id_sha256',
        'previous_boot_id_sha256','kernel_release','pid1_sha256','network_devices','automatic_start_qualified',
        'locked_boot_refused','unlock_refusal','clip_sha256','clip_bytes','decoded_frames','denial_sha256','guest_proxy_abi','guest_xml_abi',
        'all_cgroups_joined','mount_mapping_loop_joined','no_plaintext_fallback','ledger_committed','runtime_bind_readonly_same_images','runtime_bind_joined','proxy_store_exec_readonly','proxy_store_joined',*false}
    require(set(value)==fields and type(value['schema']) is int and value['schema']==2 and
        value['scope']=='owned-generated-gateway-service-boot' and value['case']==case and
        type(value['sequence']) is int and value['sequence']==units.CASES.index(case)+1 and
        value['nonce']==plan['nonce'] and value['plan_sha256']==p.u.digest(units.encoded(plan)+b'\n'),'exact-service-boot-report')
    require(value['kernel_release']==plan['kernel_release'] and value['pid1_sha256']==plan['pid1_sha256'] and
        value['network_devices']==['lo'] and value['automatic_start_qualified'] is (case!='locked') and
        value['locked_boot_refused'] is (case=='locked'),'actual-autostart-or-locked-refusal')
    refusal=value['unlock_refusal']
    if case=='locked':
        require(isinstance(refusal,dict) and set(refusal)=={'operation','key_file_withheld','exit_code'} and
            refusal['operation']=='cryptsetup-open' and refusal['key_file_withheld'] is True and
            type(refusal['exit_code']) is int and refusal['exit_code'] in (1,2),'actual-missing-key-refusal-receipt')
    else:require(refusal is None,'no-unobserved-unlock-refusal')
    for name in ('boot_id_sha256','machine_id_sha256','clip_sha256','denial_sha256'):
        require(isinstance(value[name],str) and re.fullmatch('[a-f0-9]{64}',value[name]),'bounded-service-boot-identities')
    require(type(value['clip_bytes']) is int and 1<=value['clip_bytes']<=32*1024*1024 and
        type(value['decoded_frames']) is int and 12<=value['decoded_frames']<=10000,'fully-decoded-retained-clip')
    require(all(value[name] is True for name in ('all_cgroups_joined','mount_mapping_loop_joined','no_plaintext_fallback','ledger_committed','runtime_bind_readonly_same_images','runtime_bind_joined','proxy_store_exec_readonly','proxy_store_joined'))
        and all(value[name] is False for name in false),'joined-boot-resources-and-scope')
    xml.validate(value['guest_xml_abi'],plan)
    proxy=value['guest_proxy_abi']
    import service_boot_proxy
    proxy_fields={'schema','scope','parent_qualification_sha256','parent_review_sha256','binary_sha256','libraries','boundaries',
        'source_policy_sha256','guest_abi_qualified','whole_os_qualified','full_native_advisory_closure','physical_qualified','fips_validated','deployment_permitted'}
    require(set(proxy)==proxy_fields and type(proxy['schema']) is int and proxy['schema']==1 and
        proxy['scope']==service_boot_proxy.SCOPE and proxy['parent_qualification_sha256']==plan['parent_proxy_qualification_sha256'] and
        proxy['parent_review_sha256']==plan['parent_proxy_review_sha256'] and proxy['libraries']==plan['proxy_libraries'] and
        proxy['binary_sha256']==plan['files']['/var/lib/service-boot/bin/nginx'] and
        service_boot_proxy.complete_boundaries(proxy['boundaries']) and proxy['source_policy_sha256']==p.u.file_digest(ROOT/'security/gateway-proxy-dependencies.json') and
        proxy['guest_abi_qualified'] is True and all(proxy[k] is False for k in
            ('whole_os_qualified','full_native_advisory_closure','physical_qualified','fips_validated','deployment_permitted')),'actual-guest-proxy-abi')
    return value

def early_job_timeouts(raw):
    # PID 1 can finish an early device/mount job before journald is available.
    # Privately recognize only its fixed timeout format for our exact units.
    # No descriptions, messages, paths, identifiers or raw console bytes leave
    # this function; this is diagnostic evidence, never startup admission.
    require(len(raw)<=1024*1024,'bounded-service-boot-console')
    names={units.PROVIDER:'provider',units.TARGET:'target',units.MOUNT:'mount',
        'dev-mapper-'+units.MAPPING+'.device':'device',units.PREFIX+'-monitor-stop.service':'stopper'}
    names.update({units.PREFIX+'-'+role+'.service':role for role in ('control','media','proxy','activity')})
    rows=[]
    for name,role in names.items():
        identity=re.escape(name.encode('ascii'))
        pattern=(rb'(?:^|systemd\[1\]: )(?:'+identity+rb': )?Job '+identity+
                 rb'/(start|stop|reload|restart) timed out\.\r?$')
        for operation in re.findall(pattern,raw,re.M):
            row=dict(role=role,operation=operation.decode('ascii'),result='timeout')
            if row not in rows:rows.append(row)
    require(len(rows)<=16,'bounded-early-job-timeouts')
    return units.public_job_results(rows)

def serial_report(raw,plan,case):
    require(len(raw)<=1024*1024,'bounded-service-boot-console')
    failures=re.findall(rb'^SERVICE_BOOT_FAILURE ([a-z][a-z-]{0,63})\r?$',raw,re.M)
    if failures:
        locations=re.findall(rb'^SERVICE_BOOT_LOCATIONS (\[[^\r\n]+\])\r?$',raw,re.M)
        require(len(locations)<=1 and all(len(value)<=4096 for value in locations),'bounded-public-boot-locations')
        location_rows=units.public_failure_locations(p.u.parse(locations[0])) if locations else None
        activation=re.findall(rb'^SERVICE_BOOT_ACTIVATION (\[[^\r\n]+\])\r?$',raw,re.M)
        require(raw.count(b'SERVICE_BOOT_ACTIVATION ')==len(activation)<=1 and
            all(len(value)<=2048 for value in activation),'bounded-public-activation')
        activation_rows=units.public_activation(p.u.parse(activation[0])) if activation else None
        jobs=re.findall(rb'^SERVICE_BOOT_JOBS (\[[^\r\n]+\])\r?$',raw,re.M)
        require(raw.count(b'SERVICE_BOOT_JOBS ')==len(jobs)<=1 and all(len(value)<=4096 for value in jobs),'bounded-public-boot-jobs')
        job_rows=units.public_job_results(p.u.parse(jobs[0])) if jobs else None
        early_rows=early_job_timeouts(raw)
        if location_rows:print('SERVICE_BOOT_LOCATIONS '+units.encoded(location_rows).decode(),flush=True)
        if activation_rows:print('SERVICE_BOOT_ACTIVATION '+units.encoded(activation_rows).decode(),flush=True)
        if job_rows:print('SERVICE_BOOT_JOBS '+units.encoded(job_rows).decode(),flush=True)
        if early_rows:print('SERVICE_BOOT_EARLY_JOBS '+units.encoded(early_rows).decode(),flush=True)
        raise p.u.Refusal('service-guest-'+failures[-1].decode())
    require(b'SERVICE_BOOT_ACTIVATION ' not in raw and b'SERVICE_BOOT_JOBS ' not in raw,'activation-diagnostics-only-for-failure')
    rows=re.findall(rb'^SERVICE_BOOT_REPORT (\{[^\r\n]+\})\r?$',raw,re.M)
    require(len(rows)==1 and len(rows[0])<=16384,'one-complete-service-boot-report')
    return validate_guest(p.u.parse(rows[0]),plan,case)

def aggregate(rows,plan,initial):
    require(isinstance(rows,list) and len(rows)==4,'four-complete-service-boots')
    previous=initial;boot_ids=set();first=None;last=None
    extra={'image_before_sha256','image_after_sha256','filesystem_clean','emulator_exited'}
    for case,row in zip(units.CASES,rows):
        validate_guest({k:v for k,v in row.items() if k not in extra},plan,case)
        require(extra<=set(row) and row['image_before_sha256']==previous and
            re.fullmatch('[a-f0-9]{64}',row['image_after_sha256']) and row['filesystem_clean'] is True and
            row['emulator_exited'] is True,'same-persistent-service-disk-chain')
        require(row['boot_id_sha256'] not in boot_ids and row['previous_boot_id_sha256']==last,'four-fresh-kernel-generations')
        boot_ids.add(row['boot_id_sha256']);last=row['boot_id_sha256'];previous=row['image_after_sha256']
        if first is None:first=row
        for key in ('machine_id_sha256','clip_sha256','clip_bytes','decoded_frames','denial_sha256','guest_proxy_abi','guest_xml_abi'):
            require(row[key]==first[key],'retained-recording-denial-and-guest-abi')

def validate(value):
    fields={'schema','scope','checkout','inputs','package_qualification_sha256','source_qualification_sha256',
        'media_qualification_sha256','isolation_qualification_sha256','fixture_plan','source_snapshot_sha256',
        'source_files','source_bytes','git_configuration_copied','gitlink_contents_copied','kernel','emulator',
        'image_bytes','vcpus','memory_mib','initial_image_sha256','boots','virtual_service_cold_boots_qualified',*FLAGS}
    require(set(value)==fields and type(value['schema']) is int and value['schema']==2 and value['scope']==SCOPE,
            'exact-public-service-boot-receipt')
    plan=units.validate_plan(value['fixture_plan'])
    require(value['checkout']==dict(commit=plan['head'],clean=True) and value['inputs']==inputs(),
            'same-current-service-boot-source')
    for name in ('package_qualification_sha256','source_qualification_sha256','media_qualification_sha256',
                 'isolation_qualification_sha256','source_snapshot_sha256','initial_image_sha256'):
        require(isinstance(value[name],str) and re.fullmatch('[a-f0-9]{64}',value[name]),'service-boot-proof-digest')
    require(type(value['source_files']) is int and 1<=value['source_files']<=guest_source.MAX_FILES and
            type(value['source_bytes']) is int and 1<=value['source_bytes']<=guest_source.MAX_BYTES and
            value['git_configuration_copied'] is False and value['gitlink_contents_copied'] is False,'bounded-public-source-snapshot')
    require(type(value['image_bytes']) is int and value['image_bytes']==IMAGE_BYTES and
            type(value['vcpus']) is int and value['vcpus']==4 and type(value['memory_mib']) is int and value['memory_mib']==2048 and
            value['virtual_service_cold_boots_qualified'] is True and all(value[name] is False for name in FLAGS),
            'named-service-boot-resources-and-scope')
    kernel=value['kernel'];emulator=value['emulator']
    require(set(kernel)=={'release','sha256','initramfs_sha256','package'} and kernel['release']==plan['kernel_release'] and
            set(emulator)=={'binary_sha256','bios_sha256','package'},'service-boot-runtime-inventory')
    for digest in (kernel['sha256'],kernel['initramfs_sha256'],emulator['binary_sha256'],emulator['bios_sha256']):
        require(isinstance(digest,str) and re.fullmatch('[a-f0-9]{64}',digest),'exact-service-boot-runtime-digest')
    record=kernel['package']
    require(set(record)=={'name','version','architecture','source','source_version'} and
            record['name']=='linux-image-'+kernel['release'] and record['architecture']=='amd64','actual-service-guest-kernel-package')
    tool=emulator['package']
    require(set(tool)=={'name','version','architecture','source','source_version','purl'} and
            tool['name']=='qemu-system-x86' and tool['architecture']=='amd64','actual-service-emulator-package')
    for row in (record,tool):
        for name in ('name','source'):p.u.package_name(row[name])
        for name in ('version','source_version'):p.u.package_version(row[name])
    require(tool['purl']=='pkg:deb/ubuntu/qemu-system-x86@'+tool['version'].replace(':','%3A')+'?arch=amd64&distro=noble',
            'service-emulator-package-origin')
    aggregate(value['boots'],plan,value['initial_image_sha256'])
    return value

def run(args):
    p.hosted(args.work,args.package_work) # Before reads, writes, native/privilege/VM.
    require(os.getuid()!=0,'unprivileged-service-boot-controller')
    before=inputs();head=p.u.command(['git','-C',ROOT,'rev-parse','HEAD']).strip()
    require(not p.u.command(['git','-C',ROOT,'status','--porcelain']).strip(),'clean-service-boot-checkout')
    sys.path.insert(0,str(ROOT/'tools/native-packages'));import candidate
    # Admit signed source, exact installed bytes and this controller's actual
    # interpreter mapping before allocating any guest root or virtual disk.
    xml_parent=candidate.xml_runtime(head)
    xml_raw=system_xml_evidence.private_read(xml.QUALIFICATION,65536)
    require(p.u.digest(xml_raw)==xml_parent['proof']['qualification_sha256'],'bound-service-boot-xml-parent')
    proxy=gateway_proxy_evidence.verify(ROOT,head,candidate.source_inputs())
    parent_raw,parent=gateway_proxy_evidence.read(args.proxy_review/'qualification.json')
    review_raw,review=gateway_proxy_evidence.read(args.proxy_review/'review.json')
    require(p.u.digest(parent_raw)==proxy['qualification_sha256']==review['qualification_sha256'] and
        p.u.file_digest(args.proxy_review/'nginx')==proxy['binary_sha256']==review['binary_sha256'],'bound-service-boot-proxy')
    media=gateway_media_evidence.verify(ROOT,args.media_runtime,ffmpeg=args.ffmpeg,publisher=args.publisher,reader=args.reader)
    require(media['checkout']==dict(commit=head,clean=True),'same-service-boot-media-source')
    isolation_raw=package_boot.parent_receipt(args.isolation_qualification,65536);isolation=p.u.parse(isolation_raw)
    require(p.u.digest(isolation_raw)==media['isolation_sha256'] and isolation['executables']['launcher']==p.u.file_digest(args.sandbox)
        and isolation['executables']['ffmpeg']==p.u.file_digest(args.ffmpeg),'same-service-boot-isolation')
    guest=package_boot.prepare_guest(args.work,args.package_work);work=guest.work;root=guest.root
    # One bounded extension admits four real service boots, not additional
    # retries or hidden emulator parallelism. The enclosing workflow is bounded.
    work.deadline=time.monotonic()+3600
    apt=['/usr/bin/env','DEBIAN_FRONTEND=noninteractive','/usr/bin/apt-get','-o','Acquire::Retries=1',
        '-o','Acquire::https::Timeout=30','-o','APT::Update::Error-Mode=any','-o','Acquire::AllowInsecureRepositories=false',
        '-o','APT::Get::AllowUnauthenticated=false','install','-y','--no-install-recommends',
        'ffmpeg','openssl','libssl3t64','iproute2','passwd']
    guest.inside(apt,'signed-guest-service-dependencies',600)
    require(p.u.file_digest(guest.standard)==guest.package['library_sha256'],'guest-services-retain-qualified-pcre-package')
    xml_probe=args.work/'xml-probe'
    work.execute(['/usr/bin/cc','-O2','-Wall','-Wextra','-Werror','-I/usr/include',
        ROOT/'tests/host/native/system_xml.c','-L'+str(xml.RUNTIME/'lib'),'-lexpat','-ldl','-o',xml_probe],
        'build-service-guest-xml-probe')
    archive=args.work/'source-snapshot.tar';manifest=guest_source.snapshot(ROOT,archive,head)
    guest_source.stage_guest(guest,archive,manifest)
    files={};links={}
    def install(original,target,mode='0755'):
        original=Path(original).resolve(strict=True)
        work.execute(['sudo','-n','install','-D','-m',mode,original,root/target],'stage-owned-service-runtime')
        require(p.u.file_digest(original)==p.u.file_digest(root/target),'same-staged-service-runtime')
        files['/'+target]=p.u.file_digest(original)
    for name in ('control','media','proxy','activity','sandbox','publisher','reader','ffmpeg'):
        install(getattr(args,name),'var/lib/service-boot/bin/'+name)
    install(args.proxy_review/'nginx','var/lib/service-boot/bin/nginx')
    install(xml_probe,str(xml.PROBE).lstrip('/'))
    install(xml.LIBRARY,str(xml.LIBRARY).lstrip('/'),'0644')
    install(xml.QUALIFICATION,str(xml.QUALIFICATION).lstrip('/'),'0644')
    xml.validate_plan(dict(system_xml_runtime=xml_parent['proof'],files=files))
    guest_link=root/str(xml.LINK).lstrip('/')
    require(guest_link.is_symlink() and re.fullmatch(r'libexpat\.so\.[0-9.]+',os.readlink(guest_link)) and
        guest_link.resolve(strict=True).parent==guest_link.parent,'known-private-guest-xml-soname')
    work.execute(['sudo','-n','ln','-sfn','--',xml.LINK_TARGET,guest_link],'select-owned-guest-xml-image')
    require(os.readlink(guest_link)==xml.LINK_TARGET and guest_link.resolve(strict=True)==root/str(xml.LIBRARY).lstrip('/'),
        'exact-private-guest-xml-selection')
    install(args.proxy.with_name('nxrs-gateway-proxy-install'),'var/lib/service-boot/bin/installer')
    # Retain exact compiled media SONAMEs and relative aliases. Distribution
    # dependencies come from signed guest packages and are exercised there.
    prefix=Path('/usr/lib/nxrs-gateway-media-qualification')
    require(args.media_runtime==prefix,'selected-qualified-media-prefix')
    library_paths=sorted((prefix/'lib').glob('*.so*'));require(1<=len(library_paths)<=128,'bounded-owned-media-libraries')
    for original in library_paths:
        target=str(original).removeprefix('/')
        if original.is_symlink():
            link=os.readlink(original)
            require(re.fullmatch('[A-Za-z0-9_.+-]+',link) and original.resolve().parent==prefix/'lib','relative-owned-media-soname')
            work.execute(['sudo','-n','install','-d','-m','0755',(root/target).parent],'stage-media-library-parent')
            work.execute(['sudo','-n','ln','-s',link,root/target],'stage-owned-media-soname')
            links['/'+target]=link
        else:install(original,target,'0644')
    # Verify every alias after its final target has been installed.
    for original in library_paths:require(p.u.file_digest(original)==p.u.file_digest(root/str(original).removeprefix('/')),'same-staged-media-alias')
    for name,raw in [('proxy-parent.json',parent_raw),('proxy-review.json',review_raw),
                     ('source-manifest.json',units.encoded(manifest)+b'\n')]:
        staged=args.work/name;staged.write_bytes(raw);staged.chmod(0o600)
        install(staged,'var/lib/service-boot/'+name,'0644')
    # These are public proofs, not keys. Finish read-only byte verification as
    # the unprivileged controller before protecting the private guest directory.
    guest_libraries={name:p.u.file_digest(root/path.removeprefix('/')) for name,path in stage_proxy.LIBRARIES.items()}
    for path,expected in files.items():require(p.u.file_digest(root/path.removeprefix('/'))==expected,'exact-guest-file-inventory')
    plan=dict(schema=2,scope='owned-generated-service-boot-plan',nonce=secrets.token_hex(16),head=head,
        kernel_release=guest.release,pid1_sha256=p.u.file_digest(root/'usr/lib/systemd/systemd'),
        source_manifest_sha256=p.u.digest(units.encoded(manifest)+b'\n'),inputs=service.source_inputs(),files=files,
        parent_proxy_qualification_sha256=p.u.digest(parent_raw),parent_proxy_review_sha256=p.u.digest(review_raw),
        system_xml_runtime=xml_parent['proof'],
        proxy_inputs=candidate.source_inputs(),proxy_libraries=guest_libraries,unit_sha256=units.unit_hashes(),runtime_links=links)
    units.validate_plan(plan)
    work.execute(['sudo','-n','chmod','0700',root/'var/lib/service-boot'],'private-service-boot-state')
    plan_file=args.work/'plan.json';plan_file.write_bytes(units.encoded(plan)+b'\n');plan_file.chmod(0o600)
    work.execute(['sudo','-n','install','-m','0600',plan_file,root/'var/lib/service-boot/plan.json'],'stage-private-guest-plan')
    for name,text in units.units().items():
        staged=args.work/('unit-'+p.u.digest(name.encode())+'.fixture');staged.write_text(text)
        work.execute(['sudo','-n','install','-D','-m','0644',staged,root/'etc/systemd/system'/name],'stage-service-boot-unit')
    wants=root/'etc/systemd/system/multi-user.target.wants'
    work.execute(['sudo','-n','install','-d','-m','0755',wants],'guest-service-boot-autostart')
    work.execute(['sudo','-n','ln','-s','../'+units.CHECKER,wants/units.CHECKER],'enable-service-boot-checker')
    for unit in ['serial-getty@ttyS0.service','getty@tty1.service']:
        work.execute(['sudo','-n','ln','-s','/dev/null',root/'etc/systemd/system'/unit],'disable-guest-login')
    for name,text in [('fstab','/dev/vda / ext4 defaults 0 1\n'),('machine-id','')]:
        staged=args.work/name;staged.write_text(text)
        work.execute(['sudo','-n','install','-m','0644',staged,root/'etc'/name],'stage-owned-guest-root-policy')
    for name in ['owned-key','wrong-key','volume','header.backup','candidate.deb','vendor.deb','pcre-probe','damaged.deb']:
        work.execute(['sudo','-n','rm','-f','--',root/'tmp'/name],'retire-parent-private-fixture')
    mountpoints=[line.split()[4].replace('\\040',' ') for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    require(not any(name==str(root) or name.startswith(str(root)+'/') for name in mountpoints),'no-host-mounts-below-service-guest')
    guest_source.verify_tree(root/'usr/src/nxrs',manifest,(0,0))
    image=args.work/'root.img'
    with open(image,'xb') as output:os.chmod(image,0o600);output.truncate(IMAGE_BYTES)
    owned_image(image)
    work.execute(['sudo','-n','/usr/bin/timeout','--signal=KILL','175','/usr/sbin/mkfs.ext4','-q','-F','-d',root,image],
        'assemble-owned-service-boot-image',180)
    initial=p.u.file_digest(image);boots=[]
    for case in units.CASES:
        owned_image(image);image_before=p.u.file_digest(image);phase='service-boot-'+case
        try:work.execute(qemu_command(image,args.work/'kernel',args.work/'initrd',case,plan['nonce']),phase,630)
        except Exception:
            raw=p.diagnostic_tail(args.work/(str(work.sequence)+'-'+phase+'.log'))
            if b'SERVICE_BOOT_FAILURE ' in raw:serial_report(raw,plan,case)
            raise
        raw=(args.work/(str(work.sequence)+'-'+phase+'.log')).read_bytes();report=serial_report(raw,plan,case)
        work.execute(['/usr/sbin/e2fsck','-f','-n',image],'check-service-boot-filesystem',90)
        _,header=work.execute(['/usr/sbin/dumpe2fs','-h',image],'check-clean-service-boot-state')
        require(re.findall(rb'^Filesystem state:\s*(.+)$',header,re.M)==[b'clean'],'clean-service-guest-shutdown')
        boots.append(dict(report,image_before_sha256=image_before,image_after_sha256=p.u.file_digest(image),filesystem_clean=True,emulator_exited=True))
    aggregate(boots,plan,initial);guest.verify_unchanged()
    require(before==inputs() and head==p.u.command(['git','-C',ROOT,'rev-parse','HEAD']).strip() and
        not p.u.command(['git','-C',ROOT,'status','--porcelain']).strip(),'unchanged-service-boot-source')
    result=dict(schema=2,scope=SCOPE,checkout=dict(commit=head,clean=True),inputs=before,
        package_qualification_sha256=p.u.digest(guest.package_raw),source_qualification_sha256=p.u.digest(guest.source_raw),
        media_qualification_sha256=p.u.file_digest(args.media_runtime/'qualification.json'),isolation_qualification_sha256=p.u.digest(isolation_raw),
        fixture_plan=plan,source_snapshot_sha256=manifest['archive_sha256'],source_files=len(manifest['entries']),
        source_bytes=manifest['source_bytes'],git_configuration_copied=False,gitlink_contents_copied=False,
        kernel=dict(release=guest.release,sha256=p.u.file_digest(args.work/'kernel'),initramfs_sha256=p.u.file_digest(args.work/'initrd'),package=guest.kernel_package),
        emulator=dict(binary_sha256=p.u.file_digest(package_boot.QEMU),bios_sha256=p.u.file_digest(package_boot.BIOS),package=p.u.package(p.u.owner(package_boot.QEMU))),
        image_bytes=IMAGE_BYTES,vcpus=4,memory_mib=2048,initial_image_sha256=initial,boots=boots,
        virtual_service_cold_boots_qualified=True,**dict.fromkeys(FLAGS,False))
    validate(result)
    raw=units.encoded(result)+b'\n';require(len(raw)<=262144,'bounded-public-service-boot-receipt')
    (args.work/'qualification.json').write_bytes(raw)
    print('SERVICE BOOT PASS: four fresh kernels; actual autostart, retained recording and denial, locked-key refusal and recovery; device/release gates remain open')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('work','package-work','control','media','proxy','activity','sandbox','publisher','reader','ffmpeg','media-runtime','proxy-review','isolation-qualification'):
        parser.add_argument('--'+name,required=True,type=Path)
    try:run(parser.parse_args())
    except Exception as error:
        category=str(error) if isinstance(error,p.u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Virtual encrypted service boot refused ('+category+').\n')

if __name__=='__main__':main()
