"""Actual OAEP bounds/binding/privacy controls, never target qualification."""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('sealing_controls',ROOT/'tools/device-identity/seal_diagnostics.py')
TOOLS=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(TOOLS)

class SealingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        cls.public=cls.key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve();(self.root/'logs').mkdir()
        self.name='logs/build-0.err';self.checkout='1'*40
    def decrypt(self,report,index=0,checkout=None):
        parts=report['files'][self.name]
        return self.key.decrypt(base64.b64decode(parts[index]),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=TOOLS.label(checkout or self.checkout,self.name,index,len(parts))))
    def test_real_sealing_only_retains_fixed_bounded_log_tails_without_plaintext(self):
        raw=b'prefix'*3000+b'private-credential-control';(self.root/self.name).write_bytes(raw)
        (self.root/'entropy-0.bin').write_bytes(b'excluded-seed-control')
        report=TOOLS.seal(self.root,self.public,self.checkout)
        self.assertEqual(set(report['files']),{self.name})
        self.assertNotIn('private-credential-control',json.dumps(report));self.assertNotIn('excluded-seed-control',json.dumps(report))
        self.assertEqual(b''.join(self.decrypt(report,i) for i in range(len(report['files'][self.name]))),raw[-8192:])
        self.assertFalse(report['qualified']);self.assertLess(len(json.dumps(report)),96*1024)
    def test_actual_oaep_refuses_tampering_wrong_checkout_and_reordered_blocks(self):
        (self.root/self.name).write_bytes(b'private-control'*100)
        report=TOOLS.seal(self.root,self.public,self.checkout)
        with self.assertRaises(ValueError):self.decrypt(report,checkout='2'*40)
        parts=report['files'][self.name];parts[0],parts[1]=parts[1],parts[0]
        with self.assertRaises(ValueError):self.decrypt(report)
        parts[0],parts[1]=parts[1],parts[0]
        raw=bytearray(base64.b64decode(parts[0]));raw[-1]^=1;parts[0]=base64.b64encode(raw).decode()
        with self.assertRaises(ValueError):self.decrypt(report)
    def test_symlink_and_private_key_inputs_are_refused(self):
        (self.root/'excluded').write_bytes(b'private-control');(self.root/self.name).symlink_to(self.root/'excluded')
        with self.assertRaises(OSError):TOOLS.seal(self.root,self.public,self.checkout)
        (self.root/self.name).unlink();(self.root/self.name).write_bytes(b'control')
        private=self.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        with self.assertRaises(ValueError):TOOLS.seal(self.root,private,self.checkout)
        (self.root/self.name).unlink();(self.root/'logs').rmdir();(self.root/'logs').symlink_to(self.root)
        with self.assertRaises(OSError):TOOLS.seal(self.root,self.public,self.checkout)
if __name__=='__main__':unittest.main()
