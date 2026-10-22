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
