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



"""No package installation: refusal, signed-source and timeout failure controls."""
import importlib.util
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('ci_apt',ROOT/'tools/ci-apt.py')
ci=importlib.util.module_from_spec(spec);spec.loader.exec_module(ci)


class CiAptTests(unittest.TestCase):
    def test_personal_host_refused_before_files_or_commands(self):
        with patch.dict(os.environ,{},clear=True),patch.object(ci.tempfile,'TemporaryDirectory') as directory,patch.object(ci.subprocess,'run') as command:
            with self.assertRaises(ValueError): ci.install(['ffmpeg'])
            directory.assert_not_called();command.assert_not_called()

    def test_wrong_os_or_missing_keyring_is_rejected(self):
        env={'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted'}
        with patch.dict(os.environ,env,clear=True),patch.object(ci.sys,'platform','linux'),patch.object(Path,'read_text',return_value='ID=debian\nVERSION_ID="12"\n'):
            with self.assertRaises(ValueError): ci.hosted()
        with patch.dict(os.environ,env,clear=True),patch.object(ci.sys,'platform','linux'),patch.object(Path,'read_text',return_value='ID=ubuntu\nVERSION_ID="24.04"\n'),patch.object(Path,'is_file',return_value=False):
            with self.assertRaises(ValueError): ci.hosted()

    def test_invalid_package_cannot_become_an_option_or_shell_fragment(self):
        for packages in [[],['-y'],['x;id'],['$(id)'],['x\ny'],['x/y']]:
            with patch.object(ci,'hosted'),patch.object(ci.subprocess,'run') as command:
                with self.assertRaises(ValueError): ci.install(packages)
                command.assert_not_called()

    def test_local_https_sources_preserve_signature_checks_and_bound_commands(self):
        calls=[]
        def command(args,**kwargs):
            sources=next(a.split('=',1)[1] for a in args if a.startswith('Dir::Etc::sourcelist='))
            self.assertEqual(Path(sources).read_text(),ci.SOURCE)
            self.assertIn('Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg',ci.SOURCE)
            self.assertNotIn('http://',ci.SOURCE)
            self.assertNotIn('trusted=yes',ci.SOURCE)
            self.assertIn('APT::Update::Error-Mode=any',args)
            self.assertIn('Acquire::https::Timeout=30',args)
            self.assertEqual(kwargs['check'],True)
            calls.append((args,kwargs['timeout']))
        with patch.object(ci,'hosted'),patch.object(ci.subprocess,'run',side_effect=command): ci.install(['ffmpeg'])
        self.assertEqual(len(calls),2)
        self.assertEqual([x[1] for x in calls],[240,360])
        self.assertEqual(calls[0][0][-1],'update')
        self.assertEqual(calls[1][0][-1],'ffmpeg')

    def test_failed_update_or_timeout_never_proceeds_to_install(self):
        for failure in [subprocess.CalledProcessError(100,['apt-get']),subprocess.TimeoutExpired(['apt-get'],240)]:
            with patch.object(ci,'hosted'),patch.object(ci.subprocess,'run',side_effect=failure) as command:
                with self.assertRaises(subprocess.SubprocessError): ci.install(['ffmpeg'])
                self.assertEqual(command.call_count,1)


if __name__ == '__main__':
    unittest.main()
