"""Full-service VM topology, receipt continuity, failure scope and host refusal."""
import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import service_boot as boot
import service_boot_proxy as proxy

class ServiceBootTests(unittest.TestCase):
    def plan(self):
        return dict(nonce='a'*32,kernel_release='fixture-kernel',pid1_sha256='b'*64,
            parent_proxy_qualification_sha256='c'*64,parent_proxy_review_sha256='d'*64,
            proxy_libraries={name:'e'*64 for name in boot.stage_proxy.LIBRARIES},
            files={'/var/lib/service-boot/bin/nginx':'f'*64})

    def reports(self,plan):
        abi=dict(schema=1,scope=proxy.SCOPE,parent_qualification_sha256=plan['parent_proxy_qualification_sha256'],
            parent_review_sha256=plan['parent_proxy_review_sha256'],binary_sha256='f'*64,libraries=plan['proxy_libraries'],
            boundaries=copy.deepcopy(proxy.BOUNDARY),source_policy_sha256=boot.p.u.file_digest(ROOT/'security/gateway-proxy-dependencies.json'),
            guest_abi_qualified=True,whole_os_qualified=False,full_native_advisory_closure=False,physical_qualified=False,
            fips_validated=False,deployment_permitted=False)
        records=[];disk='0'*64;previous=None
        for index,case in enumerate(boot.units.CASES):
            boot_id=boot.p.u.digest(case.encode());after=boot.p.u.digest(('disk-'+case).encode())
            row=dict(schema=1,scope='owned-generated-gateway-service-boot',case=case,sequence=index+1,nonce=plan['nonce'],
                plan_sha256=boot.p.u.digest(boot.units.encoded(plan)+b'\n'),boot_id_sha256=boot_id,machine_id_sha256='1'*64,
                previous_boot_id_sha256=previous,kernel_release=plan['kernel_release'],pid1_sha256=plan['pid1_sha256'],
                network_devices=['lo'],automatic_start_qualified=case!='locked',locked_boot_refused=case=='locked',
                clip_sha256='2'*64,clip_bytes=2000,decoded_frames=16,denial_sha256='3'*64,guest_proxy_abi=copy.deepcopy(abi),
                all_cgroups_joined=True,mount_mapping_loop_joined=True,no_plaintext_fallback=True,ledger_committed=True,
                production_key_custody_qualified=False,device_tls_qualified=False,vpn_qualified=False,power_loss_qualified=False,
                physical_qualified=False,verified_boot_chain_qualified=False,protected_rollback_qualified=False,fips_validated=False,
                deployment_permitted=False,image_before_sha256=disk,image_after_sha256=after,filesystem_clean=True,emulator_exited=True)
            records.append(row);disk=after;previous=boot_id
        return records

    def test_workflow_admits_every_executed_source_and_uploads_only_explicit_public_json(self):
        import ast,re
        text=(ROOT/'.github/workflows/gateway-service-boot.yml').read_text()
        def missing(text):
            event=re.search(r'^  pull_request:\n(.*?)(?=^  [A-Za-z_]+:|^\S|\Z)',text,re.M|re.S)
            self.assertIsNotNone(event)
            patterns=[ast.literal_eval(line.strip()[2:]) for line in event[1].splitlines() if line.strip().startswith('- ')]
            return {name for name in boot.inputs() if not any(name==pattern or
                pattern.endswith('/**') and name.startswith(pattern[:-2]) for pattern in patterns)}
        self.assertEqual(missing(text),set())
        changed=text.replace("      - 'gateway/**'\n",'')
        self.assertIn('gateway/tests/service_boot_guest.py',missing(changed))
        artifact=text.split('          path: |\n',1)[1].split('          if-no-files-found:',1)[0]
        paths=[line.strip() for line in artifact.splitlines() if line.strip()]
        self.assertEqual(len(paths),9)
        self.assertTrue(all(path.endswith('.json') and '*' not in path for path in paths))
        self.assertNotIn('always()',text)

    def test_host_refuses_before_reads_native_commands_privilege_files_or_vm(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{},clear=True),\
             patch.object(boot,'inputs') as inputs,patch.object(boot.p.u,'command') as command,\
             patch.object(boot.package_boot,'prepare_guest') as prepare:
            args=SimpleNamespace(work=Path(temporary)/'new',package_work=Path(temporary)/'absent')
            with self.assertRaisesRegex(ValueError,'hosted-ubuntu-required'):boot.run(args)
            self.assertFalse(args.work.exists());inputs.assert_not_called();command.assert_not_called();prepare.assert_not_called()

    def test_virtual_machine_has_one_owned_disk_and_no_network_shares_monitor_or_device_passthrough(self):
        args=boot.qemu_command(Path('/owned/root.img'),Path('/owned/kernel'),Path('/owned/initrd'),'locked','a'*32)
        text=' '.join(map(str,args));self.assertIn('-nic none',text);self.assertIn('-monitor none',text)
        self.assertIn('-machine q35,accel=tcg',text);self.assertIn('-smp 4 -m 2048',text)
        self.assertIn('service_case=locked service_nonce='+'a'*32,text)
        self.assertNotIn('-virtfs',text);self.assertNotIn('-fsdev',text);self.assertNotIn('hostfwd',text);self.assertNotIn('vfio',text)
        block=args[args.index('-blockdev')+1];self.assertEqual(json.loads(block)['filename'],'/owned/root.img')
        self.assertIs(json.loads(block)['cache']['no-flush'],False)
        for case,nonce in [('other','a'*32),('locked','a'*31),('locked','a'*32+';')]:
            with self.subTest(case=case,nonce=nonce),self.assertRaises(ValueError):
                boot.qemu_command(Path('/owned/root.img'),Path('/owned/kernel'),Path('/owned/initrd'),case,nonce)

    def test_four_independent_kernels_retain_exact_recording_denial_machine_and_disk_chain(self):
        plan=self.plan();rows=self.reports(plan);boot.aggregate(rows,plan,'0'*64)
        for index,field,value in [(1,'clip_sha256','4'*64),(1,'denial_sha256','4'*64),(1,'machine_id_sha256','4'*64),
                                  (1,'boot_id_sha256',rows[0]['boot_id_sha256']),(1,'previous_boot_id_sha256',None),
                                  (1,'image_before_sha256','4'*64),(2,'locked_boot_refused',False),(2,'no_plaintext_fallback',False),
                                  (3,'filesystem_clean',False),(3,'emulator_exited',False),(3,'physical_qualified',True)]:
            changed=copy.deepcopy(rows);changed[index][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):boot.aggregate(changed,plan,'0'*64)
        for changed in (rows[:3],rows[::-1],rows+[rows[-1]]):
            with self.assertRaises(ValueError):boot.aggregate(changed,plan,'0'*64)

    def test_proxy_guest_abi_requires_actual_boundaries_and_exact_planned_images(self):
        plan=self.plan();rows=self.reports(plan)
        for key,value in [('libraries',{}),('binary_sha256','4'*64),('boundaries',dict(proxy.BOUNDARY,skips=1)),
                          ('parent_qualification_sha256','4'*64),('physical_qualified',True)]:
            changed=copy.deepcopy(rows);changed[0]['guest_proxy_abi'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):boot.aggregate(changed,plan,'0'*64)

    def test_console_requires_one_complete_strict_report_and_never_uses_a_partial_success(self):
        plan=self.plan();row=self.reports(plan)[0]
        for key in ('image_before_sha256','image_after_sha256','filesystem_clean','emulator_exited'):row.pop(key)
        raw=b'SERVICE_BOOT_REPORT '+boot.units.encoded(row)+b'\n'
        self.assertEqual(boot.serial_report(raw,plan,'provisioned'),row)
        for data in (b'',raw+raw,b'SERVICE_BOOT_REPORT {}\n',raw+b'SERVICE_BOOT_FAILURE join-owned-cgroups\n'):
            with self.subTest(data=data[:50]),self.assertRaises(ValueError):boot.serial_report(data,plan,'provisioned')

if __name__=='__main__':unittest.main()
