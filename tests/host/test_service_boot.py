"""Full-service VM topology, receipt continuity, failure scope and host refusal."""
import contextlib,io,copy,json,os,sys,tempfile,unittest
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
                unlock_refusal=dict(operation='cryptsetup-open',key_file_withheld=True,exit_code=1) if case=='locked' else None,
                clip_sha256='2'*64,clip_bytes=2000,decoded_frames=16,denial_sha256='3'*64,guest_proxy_abi=copy.deepcopy(abi),
                all_cgroups_joined=True,runtime_bind_readonly_same_images=True,runtime_bind_joined=True,proxy_store_exec_readonly=True,proxy_store_joined=True,mount_mapping_loop_joined=True,no_plaintext_fallback=True,ledger_committed=True,
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
                                  (2,'runtime_bind_readonly_same_images',False),(2,'runtime_bind_joined',False),
                                  (2,'proxy_store_exec_readonly',False),(2,'proxy_store_joined',False),
                                  (2,'unlock_refusal',None),(2,'unlock_refusal',dict(operation='cryptsetup-open',key_file_withheld=True,exit_code=True)),
                                  (2,'unlock_refusal',dict(operation='modprobe',key_file_withheld=True,exit_code=1)),
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

    def test_failed_guest_emits_only_validated_source_coordinates_and_remains_a_failure(self):
        locations=[dict(source='gateway/tests/test_vpn_deployment.py',line=146,error='TimeoutExpired')]
        raw=b'SERVICE_BOOT_LOCATIONS '+boot.units.encoded(locations)+b'\nSERVICE_BOOT_FAILURE qualify-guest-proxy\n'
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaisesRegex(ValueError,'service-guest-qualify-guest-proxy'):
            boot.serial_report(raw,{},'provisioned')
        self.assertEqual(output.getvalue(),'SERVICE_BOOT_LOCATIONS '+boot.units.encoded(locations).decode()+'\n')
        invalid=[dict(source='/private/synthetic/path',line=1,error='ValueError'),
                 dict(source=locations[0]['source'],line=True,error='ValueError'),
                 dict(source=locations[0]['source'],line=1,error='UnknownSecretClass'),
                 dict(**locations[0],message='synthetic-private-message')]
        for item in invalid:
            output=io.StringIO();raw=b'SERVICE_BOOT_LOCATIONS '+boot.units.encoded([item])+b'\nSERVICE_BOOT_FAILURE qualify-guest-proxy\n'
            with self.subTest(item=item),contextlib.redirect_stdout(output),self.assertRaises(ValueError):boot.serial_report(raw,{},'provisioned')
            self.assertEqual(output.getvalue(),'')
        duplicate=b'SERVICE_BOOT_LOCATIONS '+boot.units.encoded(locations)+b'\n'
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaises(ValueError):
            boot.serial_report(duplicate+duplicate+b'SERVICE_BOOT_FAILURE qualify-guest-proxy\n',{},'provisioned')
        self.assertEqual(output.getvalue(),'')

    def test_activation_diagnostics_are_bounded_validated_before_output_and_cannot_qualify_a_failure(self):
        rows=[dict(role=role,active=False,result=None if role in ('target','mount') else 'exit-code',
                   exit_status=None if role in ('target','mount') else 203) for role in boot.units.ACTIVATION_ROLES]
        line=b'SERVICE_BOOT_ACTIVATION '+boot.units.encoded(rows)+b'\n'
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaisesRegex(ValueError,'service-guest-request-credentials'):
            boot.serial_report(line+b'SERVICE_BOOT_FAILURE request-credentials\n',{},'provisioned')
        self.assertEqual(output.getvalue().strip(),'SERVICE_BOOT_ACTIVATION '+boot.units.encoded(rows).decode())
        invalid=[[],rows[::-1],rows+[rows[0]]]
        for change in (dict(role='/private/context'),dict(result='synthetic private result'),dict(exit_status=True),
                       dict(exit_status=256),dict(active=1),dict(message='synthetic private message')):
            changed=copy.deepcopy(rows);changed[0].update(change);invalid.append(changed)
        location=[dict(source='gateway/tests/service_boot_guest.py',line=1,error='ValueError')]
        for value in invalid:
            raw=b'SERVICE_BOOT_LOCATIONS '+boot.units.encoded(location)+b'\nSERVICE_BOOT_ACTIVATION '+boot.units.encoded(value)+b'\nSERVICE_BOOT_FAILURE request-credentials\n'
            output=io.StringIO()
            with self.subTest(value=value),contextlib.redirect_stdout(output),self.assertRaises(ValueError):boot.serial_report(raw,{},'provisioned')
            self.assertEqual(output.getvalue(),'')
        for raw in (line+line+b'SERVICE_BOOT_FAILURE request-credentials\n',
                    b'SERVICE_BOOT_ACTIVATION malformed\nSERVICE_BOOT_FAILURE request-credentials\n',
                    line+b'SERVICE_BOOT_REPORT {}\n'):
            output=io.StringIO()
            with contextlib.redirect_stdout(output),self.assertRaises(ValueError):boot.serial_report(raw,{},'provisioned')
            self.assertEqual(output.getvalue(),'')

    def test_structured_job_results_cannot_export_private_fields_or_turn_a_failure_into_success(self):
        jobs=[dict(role='device',operation='start',result='timeout')]
        line=b'SERVICE_BOOT_JOBS '+boot.units.encoded(jobs)+b'\n';output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaisesRegex(ValueError,'service-guest-request-credentials'):
            boot.serial_report(line+b'SERVICE_BOOT_FAILURE request-credentials\n',{},'provisioned')
        self.assertEqual(output.getvalue().strip(),'SERVICE_BOOT_JOBS '+boot.units.encoded(jobs).decode())
        invalid=[[jobs[0],jobs[0]]]+[[{**jobs[0],**change}] for change in
            [dict(role='/private/unit'),dict(operation='private'),dict(result='private'),dict(message='private')]]
        for value in invalid:
            raw=b'SERVICE_BOOT_LOCATIONS '+boot.units.encoded([dict(source='gateway/tests/service_boot_guest.py',line=1,error='ValueError')])+b'\nSERVICE_BOOT_JOBS '+boot.units.encoded(value)+b'\nSERVICE_BOOT_FAILURE request-credentials\n'
            output=io.StringIO()
            with self.subTest(value=value),contextlib.redirect_stdout(output),self.assertRaises(ValueError):boot.serial_report(raw,{},'provisioned')
            self.assertEqual(output.getvalue(),'')
        for raw in (line+line+b'SERVICE_BOOT_FAILURE request-credentials\n',
                    b'SERVICE_BOOT_JOBS malformed\nSERVICE_BOOT_FAILURE request-credentials\n',line+b'SERVICE_BOOT_REPORT {}\n'):
            output=io.StringIO()
            with contextlib.redirect_stdout(output),self.assertRaises(ValueError):boot.serial_report(raw,{},'provisioned')
            self.assertEqual(output.getvalue(),'')

    def test_early_pid1_timeouts_preserve_exact_owned_identity_without_exporting_console(self):
        device='dev-mapper-'+boot.units.MAPPING+'.device'
        mount=boot.units.MOUNT
        lines=[f'[   90.000000] systemd[1]: {device}: Job {device}/start timed out.',
               f'systemd[1]: Job {mount}/start timed out.',
               f'Job {device}/start timed out.',
               'systemd[1]: Job unrelated-private.service/start timed out.',
               f'systemd[7]: Job {device}/stop timed out.',
               f'systemd[1]: Job {device}/start timed out. private suffix',
               f'systemd[1]: private.service: Job {device}/stop timed out.',
               f'systemd[1]: Job {device}/private-operation timed out.']
        raw=('\n'.join(lines)+'\n').encode()
        expected=[dict(role='mount',operation='start',result='timeout'),
                  dict(role='device',operation='start',result='timeout')]
        self.assertEqual(boot.early_job_timeouts(raw),expected)
        self.assertNotIn('private',json.dumps(expected))
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaisesRegex(ValueError,'service-guest-request-credentials'):
            boot.serial_report(raw+b'SERVICE_BOOT_FAILURE request-credentials\n',{},'provisioned')
        self.assertEqual(output.getvalue(),'SERVICE_BOOT_EARLY_JOBS '+boot.units.encoded(expected).decode()+'\n')
        with self.assertRaisesRegex(ValueError,'bounded-service-boot-console'):boot.early_job_timeouts(b'x'*(1024*1024+1))

    def test_early_timeout_diagnostics_never_override_structured_refusal_or_complete_report(self):
        device='dev-mapper-'+boot.units.MAPPING+'.device'
        raw=f'systemd[1]: Job {device}/start timed out.\n'.encode()
        output=io.StringIO()
        invalid=b'SERVICE_BOOT_JOBS [{"role":"private"}]\nSERVICE_BOOT_FAILURE request-credentials\n'
        with contextlib.redirect_stdout(output),self.assertRaises(ValueError):boot.serial_report(raw+invalid,{},'provisioned')
        self.assertEqual(output.getvalue(),'')
        plan=self.plan();row=self.reports(plan)[0]
        for key in ('image_before_sha256','image_after_sha256','filesystem_clean','emulator_exited'):row.pop(key)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):value=boot.serial_report(raw+b'SERVICE_BOOT_REPORT '+boot.units.encoded(row)+b'\n',plan,'provisioned')
        self.assertEqual(value,row);self.assertEqual(output.getvalue(),'')

    def test_console_requires_one_complete_strict_report_and_never_uses_a_partial_success(self):
        plan=self.plan();row=self.reports(plan)[0]
        for key in ('image_before_sha256','image_after_sha256','filesystem_clean','emulator_exited'):row.pop(key)
        raw=b'SERVICE_BOOT_REPORT '+boot.units.encoded(row)+b'\n'
        self.assertEqual(boot.serial_report(raw,plan,'provisioned'),row)
        for data in (b'',raw+raw,b'SERVICE_BOOT_REPORT {}\n',raw+b'SERVICE_BOOT_FAILURE join-owned-cgroups\n'):
            with self.subTest(data=data[:50]),self.assertRaises(ValueError):boot.serial_report(data,plan,'provisioned')

if __name__=='__main__':unittest.main()
