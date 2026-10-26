"""Guest admission, actual target properties and durable recovery refusals."""
import errno,hashlib,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'))
import service_boot_guest as guest

class ServiceBootGuestTests(unittest.TestCase):
    def test_public_failure_trace_and_nested_https_locations_withhold_messages_and_private_paths(self):
        error=None
        try:exec(compile("raise ValueError('synthetic-private-message')",str(guest.service.ROOT/'gateway/tests/service_boot_proxy.py'),'exec'))
        except ValueError as caught:error=caught
        nested={'failure_locations':[
            dict(source='gateway/tests/test_vpn_deployment.py',line=146,error='TimeoutExpired',message='synthetic-private-message'),
            dict(source='/private/synthetic/path.py',line=3,error='ValueError')]}
        with patch.object(guest,'protected',return_value=json.dumps(nested).encode()):
            value=guest.public_locations(error)
        self.assertIn(dict(source='gateway/tests/test_vpn_deployment.py',line=146,error='TimeoutExpired'),value)
        self.assertIn(dict(source='gateway/tests/service_boot_proxy.py',line=1,error='ValueError'),value)
        encoded=json.dumps(value)
        self.assertNotIn('synthetic-private-message',encoded);self.assertNotIn('/private/',encoded)
        self.assertTrue(all(set(item)=={'source','line','error'} for item in value))

    def test_preparation_failure_requires_exact_bounded_public_diagnostics(self):
        value=dict(phase='qualify-guest-proxy',command_exit=None,locations=[])
        self.assertEqual(guest.preparation_failure(value),value)
        for change in (dict(message='synthetic-private-message'),dict(command_exit=True),dict(command_exit=256),
                       dict(locations=[dict(source='/private/source',line=1,error='ValueError')])):
            with self.subTest(change=change),self.assertRaises(ValueError):guest.preparation_failure({**value,**change})

    def test_personal_kernel_refuses_before_native_operations_mutation_or_poweroff(self):
        with patch.object(guest.sys,'platform','linux'),patch.object(guest.os,'getuid',return_value=0),\
             patch.object(Path,'read_text',return_value='ordinary personal kernel parameters'),\
             patch.object(guest,'command') as command,patch.object(guest.os,'execv') as poweroff,\
             patch.object(guest,'prepare') as prepare,patch.object(guest,'check') as check:
            with self.assertRaisesRegex(ValueError,'explicit-service-boot-case'):guest.main()
            for action in (command,poweroff,prepare,check):action.assert_not_called()

    def test_activation_snapshot_excludes_paths_messages_and_unknown_result_values(self):
        state=dict(ActiveState='failed',Result='synthetic private result value',ControlGroup='/private/context')
        with patch.object(guest,'state_of',return_value=state),\
             patch.object(guest,'activation',return_value=dict(ActiveState='inactive')),\
             patch.object(guest.os.path,'ismount',return_value=True),patch.object(guest,'command',return_value=b'203\n'):
            rows=guest.activation_snapshot()
        self.assertEqual([item['role'] for item in rows],list(guest.b.ACTIVATION_ROLES))
        self.assertTrue(rows[2]['active']);self.assertFalse(rows[1]['active'])
        self.assertEqual(rows[0]['result'],'other');self.assertEqual(rows[0]['exit_status'],203)
        self.assertNotIn('private',json.dumps(rows))

    def test_native_operations_require_prior_guest_context(self):
        with patch.object(guest,'admitted',False),patch.object(guest.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'guest-context-before-native-operations'):
                guest.command(['systemctl','poweroff'],'not-admitted')
            run.assert_not_called()

    def test_target_activation_uses_available_target_properties_and_refuses_duplicates(self):
        valid=b'LoadState=loaded\nActiveState=active\nSubState=active\n'
        with patch.object(guest,'command',return_value=valid) as command:
            self.assertEqual(guest.activation(guest.b.TARGET)['ActiveState'],'active')
            args=command.call_args[0][0]
            self.assertNotIn('MainPID',args);self.assertNotIn('Result',args)
        for raw in (valid+b'ActiveState=active\n',valid.replace(b'SubState=active\n',b''),valid.replace(b'loaded',b'not-found')):
            with self.subTest(raw=raw),patch.object(guest,'command',return_value=raw),self.assertRaises(ValueError):
                guest.activation(guest.b.TARGET)

    def recovery(self,*,clip_changed=False,viewer_allowed=False,denial_changed=False,prior_clip_changed=False,
                 case='recovered',failure_phase='unlock-test-volume',failure_exit=1,key_present=False):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        base=Path(temporary.name);volume=base/'volume';volume.mkdir();state=base/'state';state.mkdir()
        clip=b'owned retained clip bytes';denial=b'owned prior denial snapshot';(state/'revocations.json').write_bytes(denial)
        boot=b'12345678-1234-1234-1234-123456789abc';machine=b'a'*32
        plan=dict(nonce='b'*32,kernel_release='fixture-kernel',pid1_sha256='d'*64)
        prior=dict(nonce=plan['nonce'],sequence=guest.CASES.index(case),boot_id_sha256='e'*64,machine_id_sha256=guest.sha(machine),
            clip_sha256=guest.sha(clip),denial_sha256=guest.sha(denial))
        if prior_clip_changed:prior['clip_sha256']='f'*64
        retained=dict(event_id='1'*32,clip_sha256=guest.sha(clip),clip_bytes=len(clip),decoded_frames=16,denial_sha256=guest.sha(denial))
        (base/'ledger.json').write_bytes(guest.b.encoded(prior));(base/'retained.json').write_bytes(guest.b.encoded(retained))
        (base/('proxy-'+case+'.json')).write_bytes(guest.b.encoded(dict(guest_abi_qualified=True)))
        if case=='locked':
            (base/'prepare-failure-locked.json').write_bytes(guest.b.encoded(dict(phase=failure_phase,command_exit=failure_exit,locations=[])))
            (base/'held-key').write_bytes(b'x'*32)
            if key_present:(base/'volume.key').write_bytes(b'y'*32)
        if denial_changed:(state/'revocations.json').write_bytes(b'changed denials')
        def request(path,role='owner',method='GET',body=None,internal=False):
            if path=='/v1/cameras/cam_reference/activity' and role in (None,'publisher','recorder'):return 401,b'{}'
            if path.endswith('/clip'):return 200,clip+b'changed' if clip_changed else clip
            if path=='/v1/cameras':return (200 if role=='owner' or viewer_allowed else 403),b'{}'
            raise AssertionError('unexpected recovery request')
        original=Path.read_bytes
        def read_bytes(path):
            if path==Path('/proc/sys/kernel/random/boot_id'):return boot+b'\n'
            if path==Path('/etc/machine-id'):return machine+b'\n'
            return original(path)
        original_stat=Path.stat
        def stat(path,*args,**kwargs):
            if case=='locked' and path==volume:return SimpleNamespace(st_mode=0)
            return original_stat(path,*args,**kwargs)
        provider={'ActiveState':'failed','Result':'exit-code'} if case=='locked' else {'ActiveState':'active'}
        patches=[patch.object(guest,'BASE',base),patch.object(guest,'STATE',state),patch.object(guest,'VOLUME',volume),
            patch.object(guest,'DEVICE',base/'mapper'),patch.object(guest,'joined',return_value=True),patch.object(Path,'stat',stat),
            patch.object(guest,'protected',side_effect=lambda path,maximum:original(path)),
            patch.object(Path,'read_bytes',read_bytes),patch.object(guest,'state_of',return_value=provider),
            patch.object(guest,'activation',return_value={'ActiveState':'inactive' if case=='locked' else 'active'}),
            patch.object(guest.os.path,'ismount',return_value=case!='locked'),
            patch.object(guest,'requestor',return_value=({},request)),patch.object(guest,'ready',return_value=True),
            patch.object(guest,'until',side_effect=lambda check,*args:guest.require(check(),'expected-ready')),
            patch.object(guest,'cleanup'),patch.object(guest,'atomic'),patch.object(guest,'emit'),
            patch.object(guest,'configuration'),patch.object(guest,'command')]
        values=[value.start() for value in patches]
        try:guest.check(case,plan)
        finally:
            for value in reversed(patches):value.stop()
        cleanup,atomic,emit,configuration,command=values[-5:]
        cleanup.assert_called_once();configuration.assert_not_called();command.assert_not_called()
        self.assertEqual(atomic.call_args[0][1]['clip_sha256'],guest.sha(clip))
        self.assertEqual(emit.call_args[0][0]['clip_sha256'],guest.sha(clip))

    def test_recovery_serves_exact_existing_clip_and_retains_denial_without_reenrollment(self):self.recovery()

    def test_locked_boot_accepts_actual_missing_key_exit_then_restores_only_its_held_key(self):
        self.recovery(case='locked')

    def test_unrelated_failure_timeout_success_signal_or_present_key_cannot_qualify_locked_boot(self):
        for change in (dict(failure_phase='crypt-module'),dict(failure_exit=None),dict(failure_exit=0),
                       dict(failure_exit=-9),dict(failure_exit=True),dict(key_present=True)):
            with self.subTest(change=change),self.assertRaises(ValueError):self.recovery(case='locked',**change)

    def test_changed_clip_denial_or_prior_generation_and_reaccepted_viewer_refuse(self):
        for change in ('clip_changed','viewer_allowed','denial_changed','prior_clip_changed'):
            with self.subTest(change=change),self.assertRaises(ValueError):self.recovery(**{change:True})


    def test_failed_provider_is_reported_before_reading_credentials_it_never_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            (base/'prepare-failure-provisioned.json').write_text(json.dumps(dict(phase='fixture-certificate',command_exit=1,locations=[])))
            original=Path.read_bytes
            def read(path):
                if path==Path('/proc/sys/kernel/random/boot_id'):return b'12345678-1234-1234-1234-123456789abc\n'
                if path==Path('/etc/machine-id'):return b'a'*32+b'\n'
                return original(path)
            with patch.object(guest,'BASE',base),patch.object(Path,'read_bytes',read),\
                 patch.object(guest,'protected',side_effect=lambda path,maximum:original(path)),\
                 patch.object(guest,'state_of',return_value=dict(ActiveState='failed',Result='exit-code')),\
                 patch.object(guest,'activation',return_value=dict(ActiveState='inactive')),\
                 patch.object(guest,'requestor',side_effect=FileNotFoundError('synthetic credential absent')) as requestor:
                with self.assertRaisesRegex(ValueError,'guest-preparation-refused'):guest.check('provisioned',dict(nonce='a'*32))
                requestor.assert_not_called()
                self.assertEqual(guest.stage,'fixture-certificate')

    def runtime_fixture(self,base):
        durable=base/'durable';runtime=base/'runtime'
        (durable/'bin').mkdir(parents=True);(runtime/'bin').mkdir(parents=True)
        files={}
        for name in ('control','nginx','sandbox'):
            path=durable/'bin'/name;path.write_bytes(('owned '+name).encode());files[str(path)]=guest.digest(path)
        source=base/'source.py';source.write_bytes(b'owned fixture source')
        return durable,runtime,dict(files=files),source

    def binding(self,*,readonly=True,same_images=True,copy_budget=True):
        with tempfile.TemporaryDirectory() as temporary:
            durable,runtime,plan,source=self.runtime_fixture(Path(temporary));mounted=False;calls=[]
            copy=guest.shutil.copyfile;digest=guest.digest
            def digest_file(path):
                return digest(source if str(path)=='/usr/src/nxrs/gateway/tests/systemd_recording_source.py' else path)
            def command(args,label):
                nonlocal mounted
                calls.append((args,label))
                if args[0:2]==['mount','--bind']:
                    for original in (durable/'bin').iterdir():
                        if same_images:os.link(original,runtime/'bin'/original.name)
                        else:copy(original,runtime/'bin'/original.name)
                    mounted=True
                elif args[0]=='umount':
                    for original in (runtime/'bin').iterdir():original.unlink()
                    mounted=False
            def copy_file(original,destination):
                if str(original)=='/usr/src/nxrs/gateway/tests/systemd_recording_source.py':original=source
                elif copy_budget and Path(original).parent==durable/'bin' and Path(original).name!='nginx':
                    raise OSError(errno.ENOSPC,'synthetic exhausted runtime-copy budget')
                return copy(original,destination)
            with patch.object(guest,'BASE',durable),patch.object(guest,'ROOT',runtime),\
                 patch.object(guest.os,'ST_NODEV',4,create=True),\
                 patch.object(guest,'command',side_effect=command),patch.object(guest.shutil,'copyfile',side_effect=copy_file),\
                 patch.object(guest.os.path,'ismount',side_effect=lambda path:mounted and Path(path)==runtime/'bin'),\
                 patch.object(guest.os,'statvfs',return_value=SimpleNamespace(f_flag=(guest.os.ST_RDONLY if readonly else 0)|guest.os.ST_NODEV|guest.os.ST_NOSUID)),\
                 patch.object(guest,'digest',side_effect=digest_file):
                review=guest.stage_runtime(plan)
                self.assertEqual(review,runtime/'proxy-review')
                self.assertEqual(list((runtime/'proxy-artifact').iterdir()),[])
                for name in ('control','nginx','sandbox'):
                    self.assertEqual((runtime/'bin'/name).stat().st_ino,(durable/'bin'/name).stat().st_ino)
                self.assertEqual(guest.digest(runtime/'source.py'),guest.digest(source))
                guest.retire_runtime()
            self.assertEqual([label for _,label in calls],['bind-runtime-images','protect-runtime-images','release-runtime-images'])
            self.assertEqual(calls[1][0],['mount','-o','remount,bind,ro,nodev,nosuid',runtime/'bin'])
            self.assertEqual(list((runtime/'bin').iterdir()),[])

    def test_verified_disk_binding_avoids_exhausted_bulk_copy_budget_and_joins(self):self.binding()

    def test_writable_or_copied_runtime_cannot_qualify_as_readonly_same_disk_binding(self):
        for change in (dict(readonly=False),dict(same_images=False)):
            with self.subTest(change=change),self.assertRaises(ValueError):self.binding(**change)

    def test_foreign_runtime_entry_refuses_before_binding_or_copying(self):
        with tempfile.TemporaryDirectory() as temporary:
            durable,runtime,plan,_=self.runtime_fixture(Path(temporary));(durable/'bin/foreign').write_bytes(b'unknown')
            with patch.object(guest,'BASE',durable),patch.object(guest,'ROOT',runtime),\
                 patch.object(guest,'command') as command,patch.object(guest.shutil,'copyfile') as copy:
                with self.assertRaisesRegex(ValueError,'exact-staged-runtime-inventory'):guest.stage_runtime(plan)
                command.assert_not_called();copy.assert_not_called()

    def test_busy_runtime_binding_is_not_reported_joined(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime=Path(temporary);(runtime/'bin').mkdir()
            with patch.object(guest,'ROOT',runtime),patch.object(guest.os.path,'ismount',return_value=True),\
                 patch.object(guest,'command') as command:
                with self.assertRaisesRegex(ValueError,'joined-runtime-bind'):guest.retire_runtime()
                command.assert_called_once_with(['umount',runtime/'bin'],'release-runtime-images')

    def proxy_store(self,*,stage_flags=6,protected_flags=7,same_disk=True,held_mount=False):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);durable=base/'disk';durable.mkdir();runtime=base/'run';runtime.mkdir()
            target=runtime/'proxy-artifact';target.mkdir();source=durable/'proxy-store-provisioned'
            mounted=False;flags=0;calls=[]
            original_samefile=Path.samefile
            def samefile(path,other):
                if path==target and other==source:return same_disk
                return original_samefile(path,other)
            def command(args,label):
                nonlocal mounted,flags
                calls.append((args,label))
                if args[:2]==['mount','--bind']:mounted=True
                elif label=='admit-proxy-store-execution':flags=stage_flags
                elif label=='protect-proxy-store':flags=protected_flags
                elif args[0]=='umount':mounted=held_mount
            with patch.object(guest,'BASE',durable),patch.object(guest,'ROOT',runtime),\
                 patch.object(guest.os,'ST_RDONLY',1),patch.object(guest.os,'ST_NOSUID',2),\
                 patch.object(guest.os,'ST_NODEV',4,create=True),patch.object(guest.os,'ST_NOEXEC',8,create=True),\
                 patch.object(guest,'command',side_effect=command),patch.object(Path,'samefile',samefile),\
                 patch.object(guest.os.path,'ismount',side_effect=lambda path:Path(path)==target and mounted),\
                 patch.object(guest.os,'statvfs',side_effect=lambda path:SimpleNamespace(f_flag=flags)):
                guest.stage_proxy_store('provisioned')
                self.assertTrue(source.is_dir());self.assertTrue(mounted)
                guest.protect_proxy_store()
                guest.retire_proxy_store()
            self.assertFalse(mounted);self.assertTrue(source.is_dir());self.assertEqual(list(target.iterdir()),[])
            self.assertEqual(calls,[(['mount','--bind',source,target],'bind-proxy-store'),
                (['mount','-o','remount,bind,rw,exec,nodev,nosuid',target],'admit-proxy-store-execution'),
                (['mount','-o','remount,bind,ro,exec,nodev,nosuid',target],'protect-proxy-store'),
                (['umount',target],'release-proxy-store')])

    def test_disk_proxy_store_admits_execution_then_requires_readonly_and_joins(self):self.proxy_store()

    def test_proxy_store_rejects_noexec_wrong_disk_missing_flags_and_busy_cleanup(self):
        for change in (dict(stage_flags=14),dict(stage_flags=7),dict(stage_flags=2),dict(stage_flags=4),
                       dict(protected_flags=15),dict(protected_flags=6),dict(protected_flags=3),
                       dict(protected_flags=5),dict(same_disk=False),dict(held_mount=True)):
            with self.subTest(change=change),self.assertRaises(ValueError):self.proxy_store(**change)

    def test_proxy_store_rejects_unknown_case_and_existing_source_before_native_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);(base/'proxy-store-provisioned').mkdir()
            with patch.object(guest,'BASE',base),patch.object(guest,'command') as command:
                for case in ('foreign','provisioned'):
                    with self.subTest(case=case),self.assertRaises(ValueError):guest.stage_proxy_store(case)
                command.assert_not_called()

    def test_failure_categories_are_fixed_bounded_and_never_include_exception_text(self):
        for code,suffix in ((errno.ENOSPC,'no-space'),(errno.EDQUOT,'quota'),(errno.EACCES,'permission'),
                            (errno.EPERM,'permission'),(errno.ENOENT,'missing'),(errno.EROFS,'read-only'),(errno.EIO,'io')):
            with self.subTest(code=code),patch.object(guest,'stage','stage-runtime-source'):
                self.assertEqual(guest.failure_phase(OSError(code,'synthetic secret')),'stage-runtime-source-'+suffix)
        with patch.object(guest,'stage','stage-runtime-source'):
            self.assertEqual(guest.failure_phase(RuntimeError('synthetic secret')),'stage-runtime-source')
        with patch.object(guest,'stage','invalid private phase'):
            self.assertEqual(guest.failure_phase(RuntimeError('synthetic secret')),'unclassified')

if __name__=='__main__':unittest.main()
