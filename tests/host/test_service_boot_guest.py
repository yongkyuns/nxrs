"""Guest admission, actual target properties and durable recovery refusals."""
import hashlib,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'))
import service_boot_guest as guest

class ServiceBootGuestTests(unittest.TestCase):
    def test_personal_kernel_refuses_before_native_operations_mutation_or_poweroff(self):
        with patch.object(guest.sys,'platform','linux'),patch.object(guest.os,'getuid',return_value=0),\
             patch.object(Path,'read_text',return_value='ordinary personal kernel parameters'),\
             patch.object(guest,'command') as command,patch.object(guest.os,'execv') as poweroff,\
             patch.object(guest,'prepare') as prepare,patch.object(guest,'check') as check:
            with self.assertRaisesRegex(ValueError,'explicit-service-boot-case'):guest.main()
            for action in (command,poweroff,prepare,check):action.assert_not_called()

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
            (base/'prepare-failure-locked.json').write_bytes(guest.b.encoded(dict(phase=failure_phase,command_exit=failure_exit)))
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
            (base/'prepare-failure-provisioned.json').write_text(json.dumps(dict(phase='fixture-certificate',command_exit=1)))
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

if __name__=='__main__':unittest.main()
