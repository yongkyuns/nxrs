"""No real trust or media processes: test the harness's bounded cleanup behavior."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

path = Path(__file__).with_name('native_reference.py')
spec = importlib.util.spec_from_file_location('native_reference', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReferenceHarnessTests(unittest.TestCase):
    def test_timeout_does_not_skip_certificate_deletion(self):
        outcomes = [subprocess.TimeoutExpired('redacted', 60), subprocess.TimeoutExpired('redacted', 60),
                    Mock(returncode=0)]
        with patch.object(module.subprocess, 'run', side_effect=outcomes) as run:
            with self.assertRaisesRegex(AssertionError, 'trust removal'):
                module.remove_test_trust(Path('/test/cert'), 'FINGERPRINT')
            self.assertEqual(run.call_count, 3)
            self.assertIn('delete-certificate', run.call_args.args[0])

    def test_cleanup_retries_and_reports_success(self):
        with patch.object(module.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('redacted', 60),
                                                              Mock(returncode=0), Mock(returncode=0)]) as run:
            module.remove_test_trust(Path('/test/cert'), 'FINGERPRINT')
            self.assertEqual(run.call_count, 3)

    def test_publisher_requires_output_frames_not_just_a_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            progress = Path(temporary)/'progress'
            progress.write_text('frame=5\nprogress=continue\n')
            module.wait_for_publisher(Mock(poll=lambda: None), progress)
            with self.assertRaisesRegex(AssertionError, 'exited'):
                module.wait_for_publisher(Mock(poll=lambda: 1), progress)

    def test_harness_refuses_a_personal_machine(self):
        with patch.dict(module.os.environ, {'GITHUB_ACTIONS': '', 'RUNNER_ENVIRONMENT': ''}):
            with self.assertRaisesRegex(AssertionError, 'ephemeral GitHub-hosted'):
                module.main(Path('/gateway'), Path('/media'), Path('/probe'))


if __name__ == '__main__':
    unittest.main()
