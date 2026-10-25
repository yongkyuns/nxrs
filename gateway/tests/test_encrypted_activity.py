import importlib.util
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
import activity
import encrypted_activity as encrypted

class EncryptedDeploymentTests(unittest.TestCase):
    def test_overlay_preserves_identity_and_strict_denial_without_custody_side_effects(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'old.json'
            old=dict(cameras=[dict(camera_id='camera',stream_path='one')],viewers=[dict(viewer_id='owner',
                     camera_ids=['camera'],token_sha256='viewer-digest')],publishers=[dict(username='pub',token_sha256='publisher-digest')])
            source.write_text(json.dumps(old));source.chmod(0o600)
            original=activity.generate(source,root/'activity',camera='camera',owner='owner',
                                      worker=Path('/usr/bin/worker'),ffmpeg=Path('/usr/lib/selected-media/bin/ffmpeg'))
            before={v.name:v.read_bytes() for v in original.iterdir()}
            output=encrypted.generate(original,root/'overlay','01234567-89ab-cdef-0123-456789abcdef')
            control=json.loads((output/'config.json').read_text());worker=json.loads((output/'activity.json').read_text())
            previous=json.loads(before['control.json'])
            for key in ['viewers','publishers','recorders']:self.assertEqual(control[key],previous[key])
            self.assertEqual(control['activity'],worker['storage'])
            self.assertEqual(control['activity']['directory'],'/srv/nxrs/activity')
            self.assertEqual(worker['ffmpeg'],'/usr/lib/selected-media/bin/ffmpeg')
            self.assertEqual(worker['reader'],'/usr/local/libexec/nxrs-rtsp-reader')
            self.assertEqual(worker['media_sandbox'],'/usr/local/libexec/nxrs-media-sandbox')
            self.assertEqual({v.name:v.read_bytes() for v in original.iterdir()},before)
            self.assertFalse((output/'recorder.token').exists())
            crypttab=(output/'crypttab.fragment').read_text()
            self.assertIn('/run/nxrs-unlock/manual-only.key',crypttab)
            self.assertIn('keyfile-timeout=1s',crypttab)
            self.assertNotIn(' none ',crypttab)
            self.assertEqual(json.loads((output/'deployment.json').read_text())['backup_configured'],False)
            for name in ['nxrs-control.service.d/encrypted-storage.conf','nxrs-activity.service']:
                unit=(output/name).read_text();self.assertIn('--require-encrypted-storage',unit)
                self.assertIn('BindsTo=',unit);self.assertIn('srv-nxrs.mount',unit);self.assertIn('ReadWritePaths=/srv/nxrs',unit)
                if name=='nxrs-activity.service':
                    self.assertIn('User=nxrs-control',unit);self.assertIn('Restart=no',unit)
                    self.assertIn('ExecStartPre=/usr/local/libexec/nxrs-media-sandbox --check',unit)
                    self.assertIn('--require-media-isolation',unit)
            control_unit=(output/'nxrs-control.service.d/encrypted-storage.conf').read_text()
            self.assertIn('--security /run/nxrs-control/security.json',control_unit)
            self.assertIn('ExecStart=\nExecStart=',control_unit)
            self.assertFalse((output/'nxrs-monitor.target').exists())
            self.assertFalse((output/'nxrs-ingest.service').exists())
            self.assertFalse((output/'nxrs-control.service').exists())
            self.assertNotIn('luksFormat',''.join(v.read_text() for v in output.rglob('*') if v.is_file()))
            with self.assertRaises(FileExistsError):encrypted.generate(original,output,'01234567-89ab-cdef-0123-456789abcdef')

    def test_kernel_fixture_refuses_a_personal_host_before_allocation_or_commands(self):
        spec=importlib.util.spec_from_file_location('encrypted_reference',ROOT/'tests/encrypted_activity_reference.py')
        reference=importlib.util.module_from_spec(spec);spec.loader.exec_module(reference)
        with mock.patch.object(reference.sys,'platform','darwin'), \
             mock.patch.object(reference.tempfile,'mkdtemp') as allocate, \
             mock.patch.object(reference.subprocess,'run') as execute:
            with self.assertRaisesRegex(AssertionError,'disposable hosted Linux root required'):
                reference.run(Path('/unused-worker'),Path('/unused-control'),Path('/unused-media'))
            allocate.assert_not_called();execute.assert_not_called()

    def test_unqualified_shared_library_refuses_before_volume_allocation(self):
        spec=importlib.util.spec_from_file_location('encrypted_regex_control',ROOT/'tests/encrypted_activity_reference.py')
        reference=importlib.util.module_from_spec(spec);spec.loader.exec_module(reference)
        sys.path.insert(0,str(ROOT/'tools'))
        import system_regex_evidence
        with mock.patch.object(reference,'hosted'), \
             mock.patch.object(reference.subprocess,'check_output',return_value='a'*40), \
             mock.patch.object(system_regex_evidence,'verify',side_effect=ValueError('owned qualification refusal')), \
             mock.patch.object(reference.tempfile,'mkdtemp') as allocate, \
             mock.patch.object(reference.subprocess,'run') as execute:
            with self.assertRaisesRegex(ValueError,'owned qualification refusal'):
                reference.run(Path('/unused-worker'),Path('/unused-control'),Path('/unused-media'))
            allocate.assert_not_called();execute.assert_not_called()

    def test_bad_uuid_and_fifo_configuration_fail_before_output_creation(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'bundle';source.mkdir(mode=0o700)
            with self.assertRaises(ValueError):encrypted.generate(source,root/'out','bad\nExecStart=other')
            self.assertFalse((root/'out').exists())
            os.mkfifo(source/'control.json',0o600)
            with self.assertRaises(ValueError):encrypted.generate(source,root/'out','01234567-89ab-cdef-0123-456789abcdef')
            self.assertFalse((root/'out').exists())

class CompositionReceiptTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('encrypted_reference',ROOT/'tests/encrypted_activity_reference.py')
        self.reference=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.reference)
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.repository=self.root/'repository';self.repository.mkdir()
        subprocess.run(['git','init','-q',str(self.repository)],check=True)
        (self.repository/'source').write_text('source')
        subprocess.run(['git','add','source'],cwd=self.repository,check=True)
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        '-c','commit.gpgsign=false','commit','-qm','fixture'],cwd=self.repository,check=True)
        self.head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.repository,text=True).strip()
        self.report=dict(qualified=True,authenticated_camera_ingest_qualified=True,
                         authenticated_backup_restore_qualified=True,sdk_camera_host_qualified=True,
                         inputs={'source':'a'*64},executables={'encoder':'b'*64},sdk_qualification_sha256='c'*64,
                         owned_volume_identity_sha256='d'*64,
                         system_regex_runtime=dict(qualification_sha256='e'*64,library_sha256='f'*64),
                         media_runtime=dict(qualified=True,checkout=dict(commit=self.head,clean=True)))
        self.report['recording']=dict(sdk_endpoint_used=True,camera_tls_front_end_test_only=False,
                                     sdk_media=dict(media_runtime=self.report['media_runtime']))
        from activity_reference import ROTATION_CHECKS
        self.report['recorder_key_replacement']=dict(schema=1,scope='host-recorder-key-replacement',qualified=True,
            new_decoded_frames=12,encrypted_storage_required=True,physical_qualified=False,deployment_permitted=False,
            **dict.fromkeys(ROTATION_CHECKS,True))

    def test_first_case_keeps_its_own_binding_when_next_case_fails(self):
        path=self.root/'security-integration-qualification-first.json'
        self.reference.write_report(self.report,path,qualification_case='first',repository=self.repository)
        before=path.read_bytes()
        with self.assertRaises(FileNotFoundError):
            self.reference.require_three_cases(self.root/'security-integration-qualification.json',repository=self.repository)
        value=json.loads(path.read_bytes())
        self.assertEqual(value['repository_head'],self.head)
        self.assertIs(value['repository_has_changes'],False)
        self.assertEqual(value['qualification_case'],'first')
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse((self.root/'security-integration-qualification-second.json').exists())
        self.assertFalse((self.root/'security-integration-qualification.json').exists())
        self.assertNotIn('repository_head',self.report)
        with self.assertRaises(FileExistsError):
            self.reference.write_report(self.report,path,qualification_case='first',repository=self.repository)
        self.assertEqual(path.read_bytes(),before)

    def write_cases(self):
        for index,case in enumerate(('first','second','third'),1):
            self.reference.write_report({**self.report,'owned_volume_identity_sha256':str(index)*64},
                self.root/('security-integration-qualification-'+case+'.json'),
                qualification_case=case,repository=self.repository)

    def test_three_bound_distinct_cases_publish_exact_third_receipt(self):
        self.write_cases();destination=self.root/'security-integration-qualification.json'
        self.reference.require_three_cases(destination,repository=self.repository)
        self.assertEqual(destination.read_bytes(),(self.root/'security-integration-qualification-third.json').read_bytes())
        with self.assertRaises(FileExistsError):
            self.reference.require_three_cases(destination,repository=self.repository)

    def test_two_cases_mixed_cohort_duplicate_volume_or_missing_binding_never_publish(self):
        self.write_cases();destination=self.root/'security-integration-qualification.json'
        path=self.root/'security-integration-qualification-third.json';original=path.read_bytes()
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.reference.require_three_cases(destination,repository=self.repository)
        self.assertFalse(destination.exists())
        for field,value,reason in [('executables',{'encoder':'0'*64},'same sources'),
                                  ('owned_volume_identity_sha256','1'*64,'fresh distinct'),
                                  ('repository_head','0'*40,'same bound case'),
                                  ('qualification_case','first','same named')]:
            changed=json.loads(original);changed[field]=value;path.write_text(json.dumps(changed))
            with self.subTest(field=field),self.assertRaisesRegex(AssertionError,reason):
                self.reference.require_three_cases(destination,repository=self.repository)
            self.assertFalse(destination.exists())
        changed=json.loads(original);del changed['repository_head'];path.write_text(json.dumps(changed))
        with self.assertRaises(KeyError):
            self.reference.require_three_cases(destination,repository=self.repository)
        self.assertFalse(destination.exists())

    def test_dirty_or_different_native_checkout_cannot_publish_bound_case(self):
        path=self.root/'first.json'
        (self.repository/'source').write_text('changed')
        with self.assertRaisesRegex(AssertionError,'clean qualification checkout required'):
            self.reference.write_report(self.report,path,qualification_case='first',repository=self.repository)
        self.assertFalse(path.exists())
        (self.repository/'source').write_text('source')
        self.report['media_runtime']['checkout']['commit']='0'*40
        with self.assertRaisesRegex(AssertionError,'same current native checkout required'):
            self.reference.write_report(self.report,path,qualification_case='first',repository=self.repository)
        self.assertFalse(path.exists())

    def test_incomplete_or_unnamed_case_cannot_publish_receipt(self):
        path=self.root/'first.json'
        for case in ('unknown','',True):
            with self.subTest(case=case),self.assertRaisesRegex(AssertionError,'named composition case required'):
                self.reference.write_report(self.report,path,qualification_case=case,repository=self.repository)
            self.assertFalse(path.exists())
        for field in ('qualified','authenticated_camera_ingest_qualified',
                      'authenticated_backup_restore_qualified','sdk_camera_host_qualified'):
            report={**self.report,field:False}
            with self.subTest(field=field),self.assertRaisesRegex(AssertionError,'complete SDK composition required'):
                self.reference.write_report(report,path,qualification_case='first',repository=self.repository)
            self.assertFalse(path.exists())

    def test_missing_or_incomplete_recorder_replacement_cannot_publish_a_case(self):
        import copy
        path=self.root/'first.json'
        value=copy.deepcopy(self.report);del value['recorder_key_replacement']
        with self.assertRaises(KeyError):
            self.reference.write_report(value,path,qualification_case='first',repository=self.repository)
        self.assertFalse(path.exists())
        for field in ('old_key_rejected','owned_cleanup_joined','encrypted_storage_required'):
            value=copy.deepcopy(self.report);value['recorder_key_replacement'][field]=False
            with self.subTest(field=field),self.assertRaises(AssertionError):
                self.reference.write_report(value,path,qualification_case='first',repository=self.repository)
            self.assertFalse(path.exists())

    def test_oversized_receipt_never_creates_output(self):
        path=self.root/'first.json'
        report={**self.report,'oversized':'x'*65536}
        with self.assertRaisesRegex(AssertionError,'bounded complete composition evidence'):
            self.reference.write_report(report,path,qualification_case='first',repository=self.repository)
        self.assertFalse(path.exists())

if __name__=='__main__':unittest.main()
