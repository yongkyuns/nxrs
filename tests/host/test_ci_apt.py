"""Exercise CI package setup without sudo, network access or host mutation."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / '.github/scripts/install-ubuntu-packages.sh'


@unittest.skipIf(os.name == 'nt', 'Ubuntu setup uses bash')
class AptSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.trace = self.directory / 'calls.jsonl'
        stub = '''import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ['APT_TEST_TRACE'], 'a') as trace:
    trace.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if os.environ.get('APT_TEST_FAIL') == name:
    raise SystemExit(42)
if os.environ.get('APT_TEST_FAIL') == 'source' and sys.argv[1:2] == ['test']:
    raise SystemExit(43)
if name == 'timeout':
    os.execvp(sys.argv[3], sys.argv[3:])
'''
        for name in ('sudo', 'timeout'):
            path = self.directory / name
            path.write_text(f'#!{sys.executable}\n' + stub)
            path.chmod(0o755)

    def run_setup(self, *packages, fail=None):
        env = {**os.environ, 'PATH': str(self.directory) + os.pathsep + os.environ['PATH'],
               'APT_TEST_TRACE': str(self.trace), 'APT_TEST_FAIL': fail or ''}
        result = subprocess.run(['bash', str(SCRIPT), *packages], env=env,
                                text=True, capture_output=True, timeout=5)
        calls = [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []
        return result, calls

    def test_packages_mirror_and_both_network_and_wall_deadlines(self):
        packages = ['gcc-arm-none-eabi', 'qemu-system-arm', 'ffmpeg']
        result, calls = self.run_setup(*packages)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in calls], ['sudo', 'sudo', 'timeout', 'sudo', 'timeout', 'sudo'])
        self.assertEqual(calls[1], ['sudo', 'test', '-s', '/etc/apt/sources.list.d/ubuntu.sources'])
        self.assertEqual(calls[2][1:3], ['--kill-after=15s', '2m'])
        self.assertEqual(calls[4][1:3], ['--kill-after=15s', '5m'])
        self.assertEqual(calls[0][-1], '/etc/apt/apt-mirrors.txt')
        source_parts = []
        for command in (calls[3], calls[5]):
            for option in ('Acquire::Retries=2', 'Acquire::http::Timeout=30', 'Acquire::https::Timeout=30',
                           'Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu.sources'):
                self.assertIn(option, command)
            source_parts.append(next(value.split('=', 1)[1] for value in command
                                     if value.startswith('Dir::Etc::sourceparts=')))
            self.assertNotIn('--allow-unauthenticated', command)
            self.assertFalse(any('AllowInsecure' in value or 'AllowUnauthenticated' in value for value in command))
        self.assertEqual(source_parts[0], source_parts[1])
        self.assertFalse(Path(source_parts[0]).exists(), 'temporary source directory must be removed')
        self.assertEqual(calls[3][-1], 'update')
        self.assertEqual(calls[5][-7:], ['install', '-y', '--no-install-recommends', '--', *packages])
        # Use the actual substitution on a temporary mirror list, preserving
        # unrelated entries and both supported Ubuntu runner suite choices.
        primary = 'https://archive.ubuntu.com/ubuntu'
        mirrors = (
            'http://azure.archive.ubuntu.com/ubuntu\n'
            'https://azure.archive.ubuntu.com/ubuntu\n'
            'http://us-east-2.ec2.archive.ubuntu.com/ubuntu\n'
            'https://eu-west-1.ec2.archive.ubuntu.com/ubuntu/ priority:2\n'
        )
        untouched = (
            'http://security.ubuntu.com/ubuntu\n'
            f'{primary}\n'
            'http://azure.archive.ubuntu.com/ubuntu-ports\n'
            'https://example.invalid/ubuntu\n'
        )
        changed = subprocess.run(['sed', '-E', calls[0][3]], input=mirrors + untouched,
                                 text=True, capture_output=True, check=True)
        self.assertEqual(changed.stdout, (primary + '\n') * 3 + primary + '/ priority:2\n' + untouched)

    def test_no_packages_never_mutates_runner(self):
        result, calls = self.run_setup()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_mirror_failure_stops_before_apt(self):
        result, calls = self.run_setup('ffmpeg', fail='sudo')
        self.assertEqual(result.returncode, 42)
        self.assertEqual(len(calls), 1)

    def test_update_timeout_does_not_start_installation(self):
        result, calls = self.run_setup('ffmpeg', fail='timeout')
        self.assertEqual(result.returncode, 42)
        self.assertEqual(len(calls), 3)

    def test_missing_ubuntu_source_stops_before_apt(self):
        result, calls = self.run_setup('ffmpeg', fail='source')
        self.assertEqual(result.returncode, 43)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
