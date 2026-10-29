import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from run_ios_reference import report_progress


class TimeoutDiagnosticsTests(unittest.TestCase):
    def report(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.log';path.write_bytes(data)
            output=io.StringIO()
            with contextlib.redirect_stdout(output): report_progress(path,'PRIVATE_TOKEN')
            return output.getvalue()

    def test_reports_structural_progress_and_redacts_fixture_token(self):
        output=self.report(b"Test Suite 'CameraSessionTests' started\n"
            b"Test Case 'testCamera' started\nCAMERA DIAGNOSTIC: PRIVATE_TOKEN\n"
            b"raw SDP and PRIVATE_TOKEN must not be printed\n")
        self.assertIn('"suite_seen": true',output)
        self.assertIn('"case_seen": true',output)
        self.assertIn('CAMERA DIAGNOSTIC: <redacted>',output)
        self.assertNotIn('PRIVATE_TOKEN',output)
        self.assertNotIn('raw SDP',output)

    def test_bounds_log_tail_line_count_and_unicode_output(self):
        output=self.report(b'x'*70000+b'\n'+('\n'.join(
            'CAMERA DIAGNOSTIC: '+str(n)+' '+('\u03bb'*1000) for n in range(20))).encode())
        self.assertIn('"tail_truncated": true',output)
        self.assertEqual(output.count('CAMERA DIAGNOSTIC:'),12)
        self.assertLess(len(output.encode()),8192)

    def test_missing_progress_is_reported_without_raw_log_content(self):
        output=self.report(b'private launcher output: Failed to launch\n')
        self.assertIn('"case_seen": false',output)
        self.assertIn('"launch_error_seen": true',output)
        self.assertNotIn('private launcher output',output)
