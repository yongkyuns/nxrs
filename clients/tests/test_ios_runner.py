"""Bounded iOS runner diagnostics and actual owned-process timeout cleanup."""
import contextlib
import io
import json
from pathlib import Path
import sys
import subprocess
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

import run_ios_reference as runner


class IosRunnerTests(unittest.TestCase):
    def test_https_admission_projects_only_fixed_domains_and_codes(self):
        raw=(b'/private/secret/CameraSessionTests.swift:99: error: Fixture HTTPS admission failed: NSURLErrorDomain -1202 PRIVATE RESPONSE\n'
             b'Fixture HTTPS admission failed: NxrsMonitorCore.MonitorError 9 PRIVATE TOKEN\n')
        result=runner.diagnostics(raw,'failed')
        self.assertEqual(result['https_admission'],[dict(domain='NSURLErrorDomain',code=-1202),
            dict(domain='NxrsMonitorCore.MonitorError',code=9)])
        encoded=json.dumps(result)
        for secret in ('private/secret','PRIVATE RESPONSE','PRIVATE TOKEN'):
            self.assertNotIn(secret,encoded)

    def test_https_admission_ignores_unknown_domains_codes_and_long_numbers(self):
        raw=(b'Fixture HTTPS admission failed: private-domain 9 PRIVATE TOKEN\n'
             b'Fixture HTTPS admission failed: NSURLErrorDomain -99999 PRIVATE TOKEN\n'
             b'Fixture HTTPS admission failed: NSURLErrorDomain -120200000 PRIVATE TOKEN\n'
             b'Fixture HTTPS admission failed: NxrsMonitorCore.MonitorError 32 PRIVATE TOKEN\n')
        result=runner.diagnostics(raw,'failed')
        self.assertEqual(result['https_admission'],[])
        self.assertNotIn('PRIVATE TOKEN',json.dumps(result))

    def test_https_admission_retains_at_most_two_unique_fixed_pairs(self):
        rows=[b'Fixture HTTPS admission failed: NSURLErrorDomain -1202\n',
              b'Fixture HTTPS admission failed: NxrsMonitorCore.MonitorError 9\n',
              b'Fixture HTTPS admission failed: NSURLErrorDomain -1001\n']
        result=runner.diagnostics(b''.join(row*100 for row in rows),'refused')
        self.assertEqual(result['https_admission'],[dict(domain='NSURLErrorDomain',code=-1202),
            dict(domain='NxrsMonitorCore.MonitorError',code=9)])
        self.assertLessEqual(len(json.dumps(result)),8192)

    def test_diagnostics_project_known_cases_and_source_coordinates_without_private_text(self):
        raw=(b"Test Case '-[NxrsMonitorSessionTests.CameraSessionTests testManualPlaybackPreferenceStartsOnlyExplicitlyResumedCameras]' started.\n"
             b"/private/secret/CameraSessionTests.swift:76: error: credential private-token and response body\n"
             b"CAMERA DIAGNOSTIC: arbitrary-private-data\n"
             b"Test Case '-[CameraSessionTests privateToken]' failed.\n")
        result=runner.diagnostics(raw,'refused');encoded=json.dumps(result)
        self.assertEqual(result['tests'],[dict(case='testManualPlaybackPreferenceStartsOnlyExplicitlyResumedCameras',event='started')])
        self.assertEqual(result['locations'],[dict(source='clients/ios/NxrsMonitorSessionTests/CameraSessionTests.swift',line=76)])
        for secret in ('private-token','private/secret','arbitrary-private-data','response body','privateToken'):
            self.assertNotIn(secret,encoded)

    def test_diagnostics_are_bounded_and_refuse_unknown_execution_categories(self):
        with self.assertRaises(ValueError):runner.diagnostics(b'', 'private-message')
        with self.assertRaises(ValueError):runner.diagnostics(b'x'*(1024*1024+1),'failed')
        case="Test Case '-[CameraSessionTests testManualPlaybackPreferenceStartsOnlyExplicitlyResumedCameras]' started.\n"
        locations=''.join(f'/private/CameraSessionTests.swift:{n}: error: private\n' for n in range(1,100))
        result=runner.diagnostics((case*100+locations).encode(),'failed')
        self.assertEqual(len(result['tests']),1);self.assertEqual(len(result['locations']),8)

    def test_only_an_exact_unique_success_marker_and_zero_exit_can_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for output,status in ((runner.SUCCESS,0),(runner.SUCCESS,1),(runner.SUCCESS+'\n'+runner.SUCCESS,0),('MULTICAMERA IOS PASS: private',0),('',0)):
                with self.subTest(status=status,output_kind=len(output)),patch.object(runner.process,'execute',return_value=(status,b'')) as execute:
                    (root/'ios-session-tests.log').write_text(output+'\n')
                    with contextlib.redirect_stdout(io.StringIO()):
                        if output==runner.SUCCESS and status==0:self.assertEqual(runner.run_tests(['test'],root),runner.SUCCESS)
                        else:
                            with self.assertRaises(AssertionError):runner.run_tests(['test'],root)
                    self.assertEqual(execute.call_args.args[4],180)

    def test_timeout_keeps_fixed_progress_and_stops_a_descendant_that_ignores_term(self):
        with tempfile.TemporaryDirectory(prefix='nxrs-ios-runner-test-') as directory:
            root=Path(directory);marker=root/'survived';ready=root/'ready'
            child=('import pathlib,signal,time,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                   'pathlib.Path(sys.argv[2]).touch(); time.sleep(2); pathlib.Path(sys.argv[1]).touch()')
            parent=('import subprocess,sys,time; '
                    'print("Test Case \'-[CameraSessionTests testManualPlaybackPreferenceStartsOnlyExplicitlyResumedCameras]\' started.",flush=True); '
                    'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2],sys.argv[3]]); time.sleep(60)')
            output=io.StringIO()
            with contextlib.redirect_stdout(output),self.assertRaisesRegex(AssertionError,'execution refused'):
                runner.run_tests([sys.executable,'-c',parent,child,str(marker),str(ready)],root,seconds=1)
            self.assertTrue(ready.exists(),'descendant did not reach its TERM-ignore barrier')
            result=json.loads(output.getvalue().split('IOS QUALIFICATION DIAGNOSTICS: ',1)[1])
            self.assertEqual(result['execution'],'refused')
            self.assertEqual(result['tests'],[dict(case='testManualPlaybackPreferenceStartsOnlyExplicitlyResumedCameras',event='started')])
            time.sleep(2.1)
            self.assertFalse(marker.exists(),'descendant outlived the fixture timeout')

    def test_term_cancellation_retires_the_separate_native_process_group(self):
        with tempfile.TemporaryDirectory(prefix='nxrs-ios-cancel-test-') as directory:
            root=Path(directory);marker=root/'survived';ready=root/'ready'
            child=('import pathlib,signal,time,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                   'pathlib.Path(sys.argv[2]).touch(); time.sleep(2); pathlib.Path(sys.argv[1]).touch()')
            native=('import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2],sys.argv[3]]); time.sleep(60)')
            parent=('import pathlib,sys; sys.path.insert(0,sys.argv[1]); import run_ios_reference as r; '
                    'r.run_tests([sys.executable,"-c",sys.argv[3],sys.argv[4],sys.argv[5],sys.argv[6]],pathlib.Path(sys.argv[2]),seconds=60)')
            owner=subprocess.Popen([sys.executable,'-c',parent,str(Path(runner.__file__).parent),str(root),native,child,str(marker),str(ready)],
                stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                deadline=time.monotonic()+3
                while not ready.exists() and owner.poll() is None and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(ready.exists(),'native descendant did not reach its cancellation barrier')
                owner.terminate();output,_=owner.communicate(timeout=5)
                self.assertEqual(owner.returncode,1)
                self.assertIn(b'IOS QUALIFICATION DIAGNOSTICS:',output)
            finally:
                if owner.poll() is None:
                    # Even a regressed TERM handler must not leak the control's
                    # native group. INT raises through the qualified owner.
                    owner.send_signal(signal.SIGINT)
                    try:owner.wait(timeout=5)
                    except subprocess.TimeoutExpired:owner.kill()
                owner.wait(timeout=5)
                if owner.stdout:owner.stdout.close()
            time.sleep(2.1)
            self.assertFalse(marker.exists(),'native descendant escaped runner cancellation')


if __name__=='__main__':unittest.main()
