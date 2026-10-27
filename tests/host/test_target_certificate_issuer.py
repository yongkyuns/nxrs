"""Actual issuer/fixture/protocol controls; never claimed as target execution."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('target_issuer_controls',ROOT/'tools/device-identity/target_issuer.py')
TOOLS=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(TOOLS)
def request():
    key=ec.generate_private_key(ec.SECP256R1())
    return (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME,'cam_A')])).sign(key,hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM))
class TargetIssuerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.directory=Path(self.temporary.name)/'authority';self.fixture=TOOLS.prepare(self.directory)
    def test_actual_target_requests_receive_matching_wrong_key_and_short_signed_controls(self):
        requests=[request(),request()]
        for label,raw in zip(('A','B'),requests):
            values=TOOLS.issue_response(self.directory,label,raw)
            self.assertEqual(len(values),9)
            keys=[x509.load_der_x509_certificate(value).public_key().public_bytes(
                serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo) for value in values[:3]]
            self.assertEqual(keys[0],TOOLS.csr_key(raw));self.assertNotEqual(keys[1],keys[0]);self.assertEqual(keys[2],keys[0])
        report=TOOLS.verify_execution(self.directory,requests)
        self.assertEqual(report['independently_verified_csrs'],2);self.assertEqual(len(set(report['public_key_sha256'])),2)
        self.assertEqual(report['independently_verified_crls'],12)
        self.assertEqual([group['clear']['generation'] for group in report['crl_controls']],[4,10])
        self.assertEqual([group['revoked']['revoked_count'] for group in report['crl_controls']],[1,2])
        self.assertTrue(report['repeated_key_refused'] and report['bad_signature_refused']);self.assertFalse(report['deployment_permitted'])
    def test_protocol_uses_bounded_frames_and_real_issuer_responses(self):
        requests=[request(),request()];sent=[]
        def read(marker):
            label='A' if b'READY A' in marker else 'B';raw=requests[0 if label=='A' else 1].decode()
            return f'NXRS_IDENTITY_CSR_BEGIN {label}\n'+raw+f'NXRS_IDENTITY_CSR_END {label}\nNXRS_IDENTITY_CERTIFICATE_READY {label}\n'
        TOOLS.Interaction(self.directory)('identity_sdk',sent.append,read)
        self.assertEqual(sent[0],b'identity_sdk\n');self.assertTrue(all(len(frame)<=128 for frame in sent))
        lines=iter(b''.join(sent[1:]).splitlines());decoded=[]
        for header in lines:
            self.assertTrue(header.startswith(b'NXRS_CERT_DER '));length=int(header.split()[1]);data=bytearray()
            while len(data)<length:data.extend(bytes.fromhex(next(lines).decode()))
            self.assertEqual(len(data),length);decoded.append(bytes(data))
        self.assertEqual(len(decoded),18)
        for offset in (0,9):
            for raw in decoded[offset:offset+3]: x509.load_der_x509_certificate(raw)
            for raw in decoded[offset+3:offset+9]: x509.load_der_x509_crl(raw)
        TOOLS.verify_execution(self.directory,requests)
    def test_crl_mutations_and_signed_export_replacement_fail_independent_verification(self):
        requests=[request(),request()]
        for label,raw in zip(('A','B'),requests): TOOLS.issue_response(self.directory,label,raw)
        TOOLS.verify_execution(self.directory,requests)
        for name in TOOLS.CRL_NAMES:
            path=self.directory/('crl-B-'+name+'.der');original=path.read_bytes()
            changed=bytearray(original);changed[-1]^=1;path.write_bytes(changed)
            with self.assertRaises(ValueError): TOOLS.verify_execution(self.directory,requests)
            path.write_bytes(original)
        path=self.directory/'crl-B-clear.der';original=path.read_bytes()
        path.write_bytes((self.directory/'crl-B-newer.der').read_bytes())
        with self.assertRaises(ValueError): TOOLS.verify_execution(self.directory,requests)
        path.write_bytes(original);TOOLS.verify_execution(self.directory,requests)
    def test_public_pin_time_mutation_and_symlinks_cannot_change_precompiled_trust(self):
        for name in ('pin.bin','time.bin'):
            path=self.directory/name;original=path.read_bytes();changed=bytearray(original);changed[0]^=1;path.write_bytes(changed)
            with self.assertRaises(ValueError):TOOLS.public_bindings(self.directory)
            path.write_bytes(original)
        root=self.directory/'root.der';other=self.directory/'other.der';root.rename(other);root.symlink_to(other)
        with self.assertRaises(OSError):TOOLS.public_bindings(self.directory)
        root.unlink();other.rename(root);self.assertEqual(TOOLS.public_bindings(self.directory),self.fixture)
    def test_replayed_invalid_or_duplicate_protocol_requests_cannot_issue_more_credentials(self):
        raw=request();TOOLS.issue_response(self.directory,'A',raw)
        state=self.directory/'issuer'/TOOLS.issuer.STATE;before=state.read_bytes()
        with self.assertRaises(ValueError):TOOLS.issue_response(self.directory,'A',request())
        self.assertEqual(state.read_bytes(),before)
        with self.assertRaises(ValueError):TOOLS.issue_response(self.directory,'B',raw)
        self.assertEqual(state.read_bytes(),before)
        with self.assertRaises(ValueError):TOOLS.issue_response(self.directory,'C',request())
        self.assertEqual(state.read_bytes(),before)
        with self.assertRaises(ValueError):TOOLS.issue_response(self.directory,'B',b'not a CSR')
        self.assertEqual(state.read_bytes(),before)
if __name__=='__main__':unittest.main()
