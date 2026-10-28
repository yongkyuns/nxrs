"""Mutate a real built public bundle; no fabricated successful evidence fixture."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from backup_engine import Failure,digest
from verify_backup_evidence import verify


@unittest.skipUnless(os.environ.get('NXRS_TEST_BACKUP_EVIDENCE'),'actual native bundle is exercised explicitly in qualification')
class ActualNativeBundleTests(unittest.TestCase):
    def test_real_artifact_and_evidence_tampering_is_rejected(self):
        source=Path(os.environ['NXRS_TEST_BACKUP_EVIDENCE'])
        verify(source,qualification=True)
        with tempfile.TemporaryDirectory(prefix='nxrs-public-bundle-controls-') as directory:
            binary=Path(directory)/source.name
            suffixes=['','.cdx.json','.provenance.json','.advisories.json']
            originals={suffix:Path(str(source)+suffix).read_bytes() for suffix in suffixes}
            def reset():
                for suffix,data in originals.items():Path(str(binary)+suffix).write_bytes(data)
            def write(suffix,value):
                Path(str(binary)+suffix).write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')
            def rebind(provenance,suffix):
                for descriptor in provenance['predicate']['runDetails']['byproducts']:
                    if descriptor['name']==binary.name+suffix:
                        descriptor['digest']['sha256']=digest(Path(str(binary)+suffix).read_bytes())
            for category in ['artifact','inventory','advisory','recipe','materials','qualification','module-content']:
                reset();provenance=json.loads(originals['.provenance.json'])
                if category=='artifact':binary.write_bytes(originals['']+b'altered')
                elif category in ['inventory','module-content']:
                    bom=json.loads(originals['.cdx.json'])
                    if category=='inventory':bom['components'][0]['hashes'][0]['content']='0'*64
                    else:bom['components'][2]['properties'][0]['value']='h1:unreviewed-content'
                    write('.cdx.json',bom);rebind(provenance,'.cdx.json');write('.provenance.json',provenance)
                elif category in ['advisory','qualification']:
                    summary=json.loads(originals['.advisories.json'])
                    if category=='advisory':summary['scans'][0]['imported_or_called']=1
                    else:summary['qualification_controls']=False
                    write('.advisories.json',summary);rebind(provenance,'.advisories.json');write('.provenance.json',provenance)
                elif category=='recipe':
                    provenance['predicate']['buildDefinition']['externalParameters']['recipe_sha256']='0'*64
                    write('.provenance.json',provenance)
                elif category=='materials':
                    provenance['predicate']['buildDefinition']['resolvedDependencies']=[]
                    write('.provenance.json',provenance)
                with self.subTest(category=category),self.assertRaises((Failure,ValueError,KeyError,TypeError)):
                    verify(binary,qualification=True)

if __name__=='__main__':unittest.main()
