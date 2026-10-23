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
if __name__=='__main__':unittest.main()
