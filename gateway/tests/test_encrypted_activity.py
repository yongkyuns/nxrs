import importlib.util
import json
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
                                      worker=Path('/usr/bin/worker'),ffmpeg=Path('/usr/bin/ffmpeg'))
            before={v.name:v.read_bytes() for v in original.iterdir()}
            output=encrypted.generate(original,root/'overlay','01234567-89ab-cdef-0123-456789abcdef')
            control=json.loads((output/'config.json').read_text());worker=json.loads((output/'activity.json').read_text())
            previous=json.loads(before['control.json'])
            for key in ['viewers','publishers','recorders']:self.assertEqual(control[key],previous[key])
            self.assertEqual(control['activity'],worker['storage'])
            self.assertEqual(control['activity']['directory'],'/srv/nxrs/activity')
            self.assertEqual({v.name:v.read_bytes() for v in original.iterdir()},before)
            self.assertFalse((output/'recorder.token').exists())
            crypttab=(output/'crypttab.fragment').read_text()
            self.assertIn('/run/nxrs-unlock/manual-only.key',crypttab)
            self.assertIn('keyfile-timeout=1s',crypttab)
            self.assertNotIn(' none ',crypttab)
            self.assertEqual(json.loads((output/'deployment.json').read_text())['backup_encryption_implemented'],False)
            for name in ['nxrs-control.service.d/encrypted-storage.conf','nxrs-activity.service']:
                unit=(output/name).read_text();self.assertIn('--require-encrypted-storage',unit)
                self.assertIn('BindsTo=',unit);self.assertIn('srv-nxrs.mount',unit);self.assertIn('ReadWritePaths=/srv/nxrs',unit)
                if name=='nxrs-activity.service':
                    self.assertIn('User=nxrs-control',unit);self.assertIn('Restart=no',unit)
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

    def test_bad_uuid_and_fifo_configuration_fail_before_output_creation(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'bundle';source.mkdir(mode=0o700)
            with self.assertRaises(ValueError):encrypted.generate(source,root/'out','bad\nExecStart=other')
            self.assertFalse((root/'out').exists())
            os.mkfifo(source/'control.json',0o600)
            with self.assertRaises(ValueError):encrypted.generate(source,root/'out','01234567-89ab-cdef-0123-456789abcdef')
            self.assertFalse((root/'out').exists())

if __name__=='__main__':unittest.main()
