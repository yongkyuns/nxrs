"""Actual produced bundle, including coherent sidecar tamper refusal."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import verify_media_evidence as evidence


@unittest.skipUnless(os.environ.get('NXRS_TEST_MEDIA_EVIDENCE'),'requires actual reproduced/audited executable')
class ActualMediaEvidenceTests(unittest.TestCase):
    def test_actual_bundle_and_coherently_rehashed_tampering(self):
        source=Path(os.environ['NXRS_TEST_MEDIA_EVIDENCE'])
        self.assertEqual(evidence.verify(source,qualification=True)['components'],87)
        with tempfile.TemporaryDirectory() as directory:
            binary=Path(directory)/source.name
            for suffix in ['', '.cdx.json','.advisories.json','.provenance.json']:
                shutil.copyfile(str(source)+suffix,str(binary)+suffix)
            original={suffix:Path(str(binary)+suffix).read_bytes() for suffix in ['.cdx.json','.advisories.json','.provenance.json']}
            def tamper(suffix,edit):
                for ending,raw in original.items():Path(str(binary)+ending).write_bytes(raw)
                value=json.loads(original[suffix]);edit(value)
                raw=json.dumps(value).encode();Path(str(binary)+suffix).write_bytes(raw)
                provenance=json.loads(original['.provenance.json'])
                for product in provenance['predicate']['runDetails']['byproducts']:
                    if product['name']==binary.name+suffix:product['digest']['sha256']=evidence.digest(raw)
                Path(str(binary)+'.provenance.json').write_text(json.dumps(provenance))
                with self.assertRaises((evidence.Failure,KeyError,ValueError,TypeError)):
                    evidence.verify(binary,qualification=True)
            def omit(value):
                removed=value['components'].pop()
                value['dependencies'][0]['dependsOn'].remove(removed['bom-ref'])
            tamper('.cdx.json',omit)
            tamper('.advisories.json',lambda v:v.update(embedded_asset_sha256='0'*64))
            tamper('.advisories.json',lambda v:v.update(physical_qualified=True))
            tamper('.advisories.json',lambda v:v['qualification_controls'].update(affected_advisory_ids=[]))
            tamper('.advisories.json',lambda v:v['main_module'].update(reviewed_advisories={}))
            for ending,raw in original.items():Path(str(binary)+ending).write_bytes(raw)
            binary.write_bytes(b'replaced executable')
            provenance=json.loads(original['.provenance.json'])
            provenance['subject'][0]['digest']['sha256']=evidence.digest(binary.read_bytes())
            Path(str(binary)+'.provenance.json').write_text(json.dumps(provenance))
            with self.assertRaises(evidence.Failure):evidence.verify(binary,qualification=True)

if __name__=='__main__':unittest.main()
