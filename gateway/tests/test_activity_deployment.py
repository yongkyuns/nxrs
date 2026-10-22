import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value

deploy=module('activity_deployment',ROOT/'deploy/activity.py')
soak=module('activity_soak',ROOT/'tools/activity_soak.py')


class ActivityDeploymentTests(unittest.TestCase):
    def test_enrollment_is_preserved_and_recorder_identity_is_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); source=root/'old.json'
            original=dict(cameras=[dict(camera_id='camera',stream_path='path')],
                          viewers=[dict(viewer_id='owner',camera_ids=['camera'],token_sha256='original-viewer-hash')],
                          publishers=[dict(username='publisher',token_sha256='original-publisher-hash')])
            source.write_text(json.dumps(original)); source.chmod(0o600)
            output=deploy.generate(source,root/'new',camera='camera',owner='owner',worker=Path('/usr/bin/worker'),ffmpeg=Path('/usr/bin/ffmpeg'))
            control=json.loads((output/'control.json').read_text())
            self.assertEqual(json.loads(source.read_text()),original)
            self.assertEqual(control['publishers'],original['publishers'])
            self.assertEqual(control['viewers'][0]['token_sha256'],'original-viewer-hash')
            self.assertTrue(control['viewers'][0]['manage_recordings'])
            self.assertNotIn((output/'recorder.token').read_text().strip(),(output/'control.json').read_text())
            self.assertEqual(control['recorders'][0]['camera_id'],'camera')
            plist=plistlib.loads((output/'org.nxrs.home.activity.plist').read_bytes())
            self.assertFalse(plist['KeepAlive']['SuccessfulExit'])
            self.assertEqual(plist['ProgramArguments'],['/usr/bin/worker',str(output/'activity.json')])
            self.assertEqual(output.stat().st_mode&0o777,0o700)
            for path in output.iterdir(): self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):
                deploy.generate(source,output,camera='camera',owner='owner',worker=Path('/usr/bin/worker'),ffmpeg=Path('/usr/bin/ffmpeg'))

    def test_invalid_scope_and_public_rtsp_do_not_create_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); source=root/'old.json'
            source.write_text(json.dumps(dict(cameras=[],viewers=[]))); source.chmod(0o600)
            for camera,endpoint in [('../camera','127.0.0.1:8554'),('camera','192.0.2.1:8554')]:
                with self.assertRaises(ValueError):
                    deploy.generate(source,root/'new',camera=camera,owner='owner',worker=Path('/usr/bin/worker'),ffmpeg=Path('/usr/bin/ffmpeg'),rtsp=endpoint)
                self.assertFalse((root/'new').exists())


class SoakAcceptanceTests(unittest.TestCase):
    def test_short_or_interrupted_runs_cannot_become_a_72_hour_pass(self):
        progress=soak.Progress(72*3600,100)
        progress.samples=100000
        self.assertFalse(progress.report(100+71*3600,finished=True)['complete'])
        self.assertEqual(progress.report(100+71*3600,finished=True)['status'],'failed')
        progress.failures.append(dict(category='observation_interrupted'))
        self.assertEqual(progress.report(100+72*3600,finished=True)['status'],'failed')

    def test_stale_frames_and_resource_growth_are_retained_failures(self):
        progress=soak.Progress(72*3600,0)
        def page(counter,age=1):
            return dict(observed_at_ms=100000,health=dict(frames_observed=counter,last_frame_at_ms=100000-age))
        small=dict(activity=dict(pid=123,rss_bytes=10*1024*1024,threads=3))
        progress.sample(600,page(1),small)
        progress.sample(640,page(1,20000),small)
        large=dict(activity=dict(pid=123,rss_bytes=100*1024*1024,threads=10))
        progress.sample(670,page(2),large)
        categories={v['category'] for v in progress.failures}
        self.assertIn('camera_frames_not_recent',categories)
        self.assertIn('camera_progress_stalled',categories)
        self.assertIn('process_resource_growth',categories)
        progress.samples=100000
        self.assertEqual(progress.report(72*3600,finished=True)['status'],'failed')

    def test_only_complete_observation_with_enough_samples_can_pass(self):
        progress=soak.Progress(72*3600,0)
        self.assertFalse(progress.report(72*3600,finished=True)['complete'])
        progress.samples=8640
        self.assertEqual(progress.report(72*3600,finished=True)['status'],'passed')


if __name__=='__main__': unittest.main()
