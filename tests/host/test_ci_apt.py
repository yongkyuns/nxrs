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
        self.assertEqual([call[0] for call in calls], ['sudo', 'timeout', 'sudo', 'timeout', 'sudo'])
        self.assertEqual(calls[1][1:3], ['--kill-after=15s', '2m'])
        self.assertEqual(calls[3][1:3], ['--kill-after=15s', '5m'])
        self.assertEqual(calls[0][-1], '/etc/apt/apt-mirrors.txt')
        for command in (calls[2], calls[4]):
            for option in ('Acquire::Retries=2', 'Acquire::http::Timeout=30', 'Acquire::https::Timeout=30'):
                self.assertIn(option, command)
        self.assertEqual(calls[2][-1], 'update')
        self.assertEqual(calls[4][-7:], ['install', '-y', '--no-install-recommends', '--', *packages])
        # Use the actual substitution on a temporary mirror list, preserving
        # unrelated entries and both supported Ubuntu runner suite choices.
        mirrors = 'http://azure.archive.ubuntu.com/ubuntu\nhttps://azure.archive.ubuntu.com/ubuntu\nhttps://example.invalid/ubuntu\n'
        changed = subprocess.run(['sed', '-E', calls[0][3]], input=mirrors,
                                 text=True, capture_output=True, check=True)
        self.assertEqual(changed.stdout, 'https://archive.ubuntu.com/ubuntu\n' * 2 + 'https://example.invalid/ubuntu\n')

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
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
