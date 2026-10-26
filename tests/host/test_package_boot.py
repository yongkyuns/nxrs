"""Virtual boot admission, persistent identity, artifact scope and host refusal."""
import copy,hashlib,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import package_boot as b
c=b.c

class VirtualBootTests(unittest.TestCase):
    def plan(self):
        return dict(nonce='a'*32,kernel_release='6.8.0-87-generic',pid1_sha256='1'*64,guest_sha256='2'*64,runner_sha256='3'*64,
            contract_sha256='4'*64,package_version=b.p.VERSION,package_sha256='5'*64,library_name='libpcre2-8.so.0.16.1',
            library_sha256='6'*64,changed_library_sha256='7'*64,vendor_version='10.42-4ubuntu2.1',vendor_archive_sha256='8'*64)

    def reports(self,plan):
        records=[]
        for index,case in enumerate(c.CASES):
            boot=c.sha(('kernel-'+case).encode());previous=None if not records else records[-1]['boot_id_sha256']
            records.append(dict(schema=1,scope=c.SCOPE,case=case,nonce=plan['nonce'],plan_sha256=c.sha(c.encoded(plan)+b'\n'),sequence=index+1,
                boot_id_sha256=boot,machine_id_sha256='9'*64,previous_boot_id_sha256=previous,kernel_release=plan['kernel_release'],
                pid1_sha256=plan['pid1_sha256'],network_devices=['lo'],admission_refused=case in c.NEGATIVE,
                refusal_result='exit-code' if case in c.NEGATIVE else None,consumer_passed=True,encrypted_payload_sha256=c.sha(c.payload(plan['nonce'],case)),
                previous_encrypted_payload_sha256=None if index==0 else c.sha(c.payload(plan['nonce'],c.CASES[index-1])),
                mapped_device_joined=True,prepared_next_state=c.NEXT[index],ledger_committed=True))
        return records

    def receipt(self):
        plan=self.plan();boots=self.reports(plan);prior='b'*64
        for index,row in enumerate(boots):
            after=c.sha(('disk-'+str(index)).encode());row.update(image_before_sha256=prior,image_after_sha256=after,filesystem_clean=True,emulator_exited=True);prior=after
        return dict(schema=1,scope=c.SCOPE,checkout=dict(commit='c'*40,clean=True),inputs=b.inputs(),
            package_qualification_sha256='d'*64,source_qualification_sha256='e'*64,package_sha256=plan['package_sha256'],library_sha256=plan['library_sha256'],
            kernel=dict(release=plan['kernel_release'],sha256='f'*64,initramfs_sha256='1'*64,
                package=dict(name='linux-image-'+plan['kernel_release'],version='6.8.0-87.88',architecture='amd64',source='linux-signed',source_version='6.8.0-87.88')),
            emulator=dict(binary_sha256='2'*64,bios_sha256='3'*64,package=dict(name='qemu-system-x86',version='1:8.2.2+ds-0ubuntu1',architecture='amd64',source='qemu',source_version='1:8.2.2+ds-0ubuntu1',
                purl='pkg:deb/ubuntu/qemu-system-x86@1%3A8.2.2+ds-0ubuntu1?arch=amd64&distro=noble')),
            fixture_plan=plan,image_bytes=b.IMAGE_BYTES,vcpus=2,memory_mib=1024,initial_image_sha256='b'*64,boots=boots,virtual_cold_boots_qualified=True,
            **dict.fromkeys(b.FLAGS,False))

    def test_personal_host_refuses_before_source_reads_files_commands_or_vm(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True),patch.object(b.p.u,'command') as command:
            path=Path(directory)/'new'
            with self.assertRaisesRegex(b.p.u.Refusal,'hosted-ubuntu-required'):b.run(path,Path(directory)/'missing-package')
            self.assertFalse(path.exists());command.assert_not_called()

    def test_guest_personal_host_refuses_before_poweroff_or_native_operation(self):
        spec=importlib.util.spec_from_file_location('owned_guest_test',ROOT/'gateway/tests/package_boot_guest.py')
        guest=importlib.util.module_from_spec(spec);spec.loader.exec_module(guest)
        with patch.object(guest.sys,'platform','linux'),patch.object(guest.os,'getuid',return_value=0),\
             patch.object(Path,'read_text',return_value='ordinary host kernel parameters'),patch.object(guest.os,'execv') as poweroff,\
             patch.object(guest,'execute') as execute:
            with self.assertRaisesRegex(ValueError,'explicit-owned-guest-case'):guest.main()
            poweroff.assert_not_called();execute.assert_not_called()

    def test_parent_receipt_refuses_links_writable_or_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'receipt';path.write_bytes(b'public fixture');path.chmod(0o600)
            self.assertEqual(b.parent_receipt(path,64),b'public fixture')
            link=Path(directory)/'alias';link.symlink_to(path)
            with self.assertRaises(OSError):b.parent_receipt(link,64)
            with self.assertRaises(b.p.u.Refusal):b.parent_receipt(path,3)
            path.chmod(0o666)
            with self.assertRaises(b.p.u.Refusal):b.parent_receipt(path,64)

    def test_virtual_image_must_be_private_regular_single_link_owned_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(b,'IMAGE_BYTES',128):
            path=Path(directory)/'image';path.write_bytes(b'x'*128);path.chmod(0o600);b.owned_image(path)
            link=Path(directory)/'symlink';link.symlink_to(path)
            with self.assertRaises(b.p.u.Refusal):b.owned_image(link)
            alias=Path(directory)/'hardlink';os.link(path,alias)
            with self.assertRaises(b.p.u.Refusal):b.owned_image(path)
            alias.unlink();path.chmod(0o644)
            with self.assertRaises(b.p.u.Refusal):b.owned_image(path)
            path.chmod(0o600);path.write_bytes(b'too short')
            with self.assertRaises(b.p.u.Refusal):b.owned_image(path)

    def test_emulator_has_no_network_host_shares_monitor_or_snapshot_and_quotes_disk_as_json(self):
        image=Path('/private fixture/image,with comma.img')
        command=[str(v) for v in b.qemu_command(image,Path('/kernel'),Path('/initrd'),'installed','a'*32)]
        self.assertEqual(command[command.index('-nic')+1],'none');self.assertEqual(command[command.index('-monitor')+1],'none')
        self.assertIn('q35,accel=tcg',command);self.assertNotIn('-snapshot',command)
        self.assertFalse(set(command)&{'-netdev','-fsdev','-virtfs','-enable-kvm','-qmp'})
        self.assertEqual(json.loads(command[command.index('-blockdev')+1])['filename'],str(image))
        with self.assertRaises(b.p.u.Refusal):b.qemu_command(image,Path('/kernel'),Path('/initrd'),'unknown','a'*32)

    def test_duplicate_missing_or_unknown_kernel_case_and_nonce_refuse(self):
        self.assertEqual(c.command_case('root=/dev/vda qual_case=installed qual_nonce='+'a'*32),('installed','a'*32))
        for text in ['root=/dev/vda','qual_case=other qual_nonce='+'a'*32,
            'qual_case=installed qual_case=vendor qual_nonce='+'a'*32,'qual_case=installed qual_nonce=bad']:
            with self.assertRaises(ValueError):c.command_case(text)
        for key,value in [('library_name','../../outside'),('package_version','10.42-old'),('unknown_private_field','private')]:
            plan=self.plan();plan[key]=value
            with self.assertRaises(ValueError):c.validate_plan(plan)

    def test_all_five_fresh_kernels_and_one_durable_identity_and_ledger_are_required(self):
        plan=self.plan();records=self.reports(plan);c.aggregate(records,plan)
        for mutate in [lambda rows:rows.pop(),lambda rows:rows[1].update(boot_id_sha256=rows[0]['boot_id_sha256']),
            lambda rows:rows[2].update(machine_id_sha256='0'*64),lambda rows:rows[3].update(previous_boot_id_sha256='0'*64),
            lambda rows:rows[2].update(previous_encrypted_payload_sha256=None),lambda rows:rows[2].update(admission_refused=False),lambda rows:rows[4].update(ledger_committed=False)]:
            changed=copy.deepcopy(records);mutate(changed)
            with self.assertRaises(ValueError):c.aggregate(changed,plan)

    def test_serial_only_accepts_one_complete_bound_report_and_fixed_failure_stage(self):
        plan=self.plan();record=self.reports(plan)[0];raw=b'ordinary private console\nPACKAGE_BOOT_REPORT '+c.encoded(record)+b'\r\n'
        self.assertEqual(b.serial_report(raw,plan,'installed'),record)
        for value in [b'',raw+raw,b'PACKAGE_BOOT_REPORT {"schema":1}\n']:
            with self.assertRaises((ValueError,b.p.u.Refusal)):b.serial_report(value,plan,'installed')
        with self.assertRaisesRegex(b.p.u.Refusal,'guest-failure-volume'):b.serial_report(b'PACKAGE_BOOT_FAILURE volume\n',plan,'installed')
        with self.assertRaisesRegex(b.p.u.Refusal,'guest-command-loop-module'):
            b.serial_report(b'PACKAGE_BOOT_FAILURE volume\nPACKAGE_BOOT_COMMAND loop-module\n',plan,'installed')
        with self.assertRaisesRegex(b.p.u.Refusal,'classified-guest-command'):
            b.serial_report(b'PACKAGE_BOOT_FAILURE volume\nPACKAGE_BOOT_COMMAND private-data\n',plan,'installed')

    def test_receipt_refuses_scope_inflation_private_fields_unjoined_disks_and_dirty_shutdown(self):
        value=self.receipt();b.validate(value)
        for flag in b.FLAGS:
            changed=copy.deepcopy(value);changed[flag]=True
            with self.subTest(flag=flag),self.assertRaises(b.p.u.Refusal):b.validate(changed)
        for mutate in [lambda row:row.update(private_log='private'),lambda row:row['boots'][1].update(image_before_sha256='0'*64),
            lambda row:row['boots'][0].update(filesystem_clean=False),lambda row:row['emulator']['package'].update(private_path='/private/value'),
            lambda row:row.update(vcpus=2.0)]:
            changed=copy.deepcopy(value);mutate(changed)
            with self.assertRaises((ValueError,b.p.u.Refusal)):b.validate(changed)

if __name__=='__main__':unittest.main()
