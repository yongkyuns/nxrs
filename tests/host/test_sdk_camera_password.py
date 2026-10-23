"""Actual issuer admission for the SDK fixture's random CA passphrase."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'))
import device_tls_camera as camera


class SdkIssuerPasswordTests(unittest.TestCase):
    def test_forbidden_random_bytes_are_encoded_before_actual_issuer_admission(self):
        # Deterministic negative control for the old raw-byte generation. This
        # password protects only a newly created disposable synthetic authority.
        raw=b'\0\n'*24
        with self.assertRaises(ValueError):camera.issuer.password_value(raw)
        with mock.patch.object(camera.secrets,'token_bytes',return_value=raw):
            password=camera.issuer_password()
        self.assertEqual(bytes.fromhex(password.decode('ascii')),raw)
        with tempfile.TemporaryDirectory(prefix='nxrs-sdk-password-') as directory:
            authority=Path(directory)/'issuer'
            pin=camera.issuer.initialize(authority,'site_test',password)
            self.assertEqual(len(pin),64)
            # Real encrypted key input is reopened with the generated password.
            bundle=Path(directory)/'public'
            camera.issuer.export(authority,pin,password,bundle)
            self.assertTrue((bundle/'bundle.json').is_file())


if __name__=='__main__':unittest.main()
