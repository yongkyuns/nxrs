"""Actual issuer/time bindings for the separate target TLS profile."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('tls_target_test',ROOT/'tools/device-identity/target_tls.py')
tls=importlib.util.module_from_spec(spec);spec.loader.exec_module(tls)
spec=importlib.util.spec_from_file_location('legacy_target_test',ROOT/'tools/device-identity/target_issuer.py')
legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
class FreshTargetFixtureTests(unittest.TestCase):
 def directory(self):
  temporary=tempfile.TemporaryDirectory(prefix='nxrs-target-tls-clock-');self.addCleanup(temporary.cleanup)
  return Path(temporary.name)/'authority'
 def test_actual_fresh_root_time_are_bound_without_weakening_legacy_clock(self):
  directory=self.directory();proof=tls.prepare(directory)
  self.assertEqual(tls.public_bindings(directory),proof)
  self.assertFalse(proof['qualified']);self.assertFalse(proof['trusted_time_acquisition_qualified'])
  self.assertFalse(proof['deployment_permitted'])
  with self.assertRaises(ValueError):legacy.public_bindings(directory)
  with self.assertRaises(FileExistsError):tls.prepare(directory)
 def test_time_pin_root_and_manifest_mutations_refuse(self):
  directory=self.directory();tls.prepare(directory)
  for name in ['time.bin','pin.bin','root.der','trust.json']:
   original=(directory/name).read_bytes();changed=bytearray(original);changed[-1]^=1
   (directory/name).write_bytes(changed)
   with self.assertRaises((ValueError,UnicodeError)):tls.public_bindings(directory)
   (directory/name).write_bytes(original)
  self.assertFalse(tls.public_bindings(directory)['physical_qualified'])
 def test_payload_waits_for_unique_newline_acks_with_bounded_wire_frames(self):
  events=[]
  def write(value):events.append(('write',value))
  def read(value):events.append(('read',value))
  self.assertEqual(tls.send_payload(bytes(range(17)),0,write,read),2)
  self.assertEqual(events,[('write',bytes(range(16)).hex().encode()+b'\n'),
   ('read',b'NXRS_TLS_PAYLOAD_ACK 1\n'),('write',b'10\n'),('read',b'NXRS_TLS_PAYLOAD_ACK 2\n')])
  self.assertTrue(all(len(value)<=33 for kind,value in events if kind=='write'))
  for value,counter in [(b'',0),(b'x'*4097,0),(b'x',100000)]:
   with self.assertRaises(ValueError):tls.send_payload(value,counter,write,read)
 def test_local_trust_failure_and_retry_do_not_count_as_server_refusal(self):
  import ssl
  for error in [ssl.SSLCertVerificationError('trust failure'),ssl.SSLWantReadError(),ssl.SSLWantWriteError(),OSError()]:
   self.assertFalse(tls.terminal_peer_refusal(error))
  self.assertTrue(tls.terminal_peer_refusal(ssl.SSLError('record layer failure')))
  self.assertTrue(tls.terminal_peer_refusal(ssl.SSLEOFError()))
 def test_actual_client_trust_file_imports_crl_and_csr_signature_refuses_mutation(self):
  import ssl
  from cryptography import x509
  from cryptography.hazmat.primitives import hashes,serialization
  from cryptography.hazmat.primitives.asymmetric import ec
  from cryptography.x509.oid import NameOID
  directory=self.directory();tls.prepare(directory)
  key=ec.generate_private_key(ec.SECP256R1())
  csr=x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'cam_a')])).sign(key,hashes.SHA256())
  raw=bytearray(csr.public_bytes(serialization.Encoding.DER));raw[-1]^=1
  bad=x509.load_der_x509_csr(bytes(raw)).public_bytes(serialization.Encoding.PEM)
  before=(directory/'issuer'/tls.issuer.STATE).read_bytes()
  with self.assertRaises(ValueError):tls.issue(directory,bad)
  self.assertEqual((directory/'issuer'/tls.issuer.STATE).read_bytes(),before)
  leaf,crl,client=tls.issue(directory,csr.public_bytes(serialization.Encoding.PEM))
  self.assertEqual(x509.load_der_x509_certificate(leaf).public_key().public_numbers(),key.public_key().public_numbers())
  self.assertGreater(len(crl),0);self.assertTrue(client.is_file())
  trust=(directory/'bundle/ca.pem').read_bytes()+(directory/'bundle/revocations.pem').read_bytes()
  tls.fresh(directory/'trust.pem',trust)
  context=ssl.create_default_context(cafile=str(directory/'trust.pem'))
  self.assertEqual(context.cert_store_stats()['crl'],1)
if __name__=='__main__':unittest.main()
