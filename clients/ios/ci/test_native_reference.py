"""Portable fixture regressions; real Apple TLS controls run inside the probe."""
import importlib.util
from pathlib import Path
import ssl
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

path = Path(__file__).with_name('native_reference.py')
spec = importlib.util.spec_from_file_location('native_reference', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReferenceHarnessTests(unittest.TestCase):
    def test_cleanup_attempts_every_resource_despite_failure(self):
        server = Mock()
        server.shutdown.side_effect = RuntimeError('private details')
        child, log = Mock(), Mock()
        child.poll.return_value = None
        with self.assertRaisesRegex(AssertionError, '^reference cleanup failed: HTTPS fixture$'):
            module.cleanup_resources(server, [child], [log])
        server.server_close.assert_called_once()
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=8)
        log.close.assert_called_once()

    def test_cleanup_escalates_stuck_process(self):
        child, log = Mock(), Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('private', 8), 0]
        module.cleanup_resources(None, [child], [log])
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)
        log.close.assert_called_once()

    def test_cleanup_failure_does_not_skip_other_processes(self):
        broken, healthy, log = Mock(), Mock(), Mock()
        broken.poll.side_effect = RuntimeError('private')
        healthy.poll.return_value = None
        with self.assertRaisesRegex(AssertionError, 'child process'):
            module.cleanup_resources(None, [healthy, broken], [log])
        healthy.terminate.assert_called_once()
        log.close.assert_called_once()

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

    def test_command_failure_is_redacted_and_private(self):
        with patch.object(module.subprocess, 'run', return_value=Mock(returncode=1)) as run:
            with self.assertRaisesRegex(AssertionError, '^fixture failed \\(exit 1\\)$'):
                module.checked_command(['openssl', 'private'], 'fixture')
            self.assertEqual(run.call_args.kwargs['umask'], 0o077)
            self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_certificates_are_private_separate_and_parseable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module.create_certificates(root)
            self.assertNotEqual((root/'root.der').read_bytes(), (root/'wrong-root.der').read_bytes())
            self.assertNotEqual((root/'root.der').read_bytes(), (root/'cert.der').read_bytes())
            ssl.create_default_context(cafile=str(root/'root.pem'))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root/'cert.pem', root/'key.pem')
            for name in ['root.key', 'wrong-root.key', 'key.pem']:
                self.assertEqual(stat.S_IMODE((root/name).stat().st_mode), 0o600)
            self.assertIn('subjectAltName=DNS:localhost', (root/'leaf.cnf').read_text())
        self.assertFalse(root.exists())

    def test_harness_has_no_machine_trust_commands(self):
        source = path.read_text()
        for forbidden in ['sudo', 'security', 'add-trusted-cert', 'remove-trusted-cert', '/Library/Keychains']:
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
