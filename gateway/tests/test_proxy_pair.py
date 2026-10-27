"""Fixture pair arguments, version identity and safe corruption admission."""
import hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import proxy_pair as pair

class ProxyPairTests(unittest.TestCase):
    def test_installer_receives_explicit_proof_review_and_expected_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);proof=b'{"scope":"synthetic-public-proof"}'
            raw=json.dumps(dict(qualification_sha256=hashlib.sha256(proof).hexdigest())).encode()
            (root/'qualification.json').write_bytes(proof);(root/'review.json').write_bytes(raw)
            args=pair.install_arguments(Path('/owned/installer'),root,Path('/owned/store'),'a'*64)
            self.assertEqual(args,['/owned/installer','--review-dir',str(root),'--review-sha256',hashlib.sha256(raw).hexdigest(),
                '--qualification-sha256',hashlib.sha256(proof).hexdigest(),'--destination','/owned/store','--expected-active','a'*64])
            with self.assertRaises(ValueError):pair.install_arguments(Path('/owned/installer'),root,Path('/owned/store'),'../other')
            (root/'qualification.json').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'same-fixture-pair-proof'):pair.install_arguments(Path('/owned/installer'),root,Path('/owned/store'))

    def test_selected_policy_must_match_the_single_digest_without_arbitrary_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);raw=b'{"approved":"synthetic"}';digest=hashlib.sha256(raw).hexdigest()
            version=root/'versions'/digest;version.mkdir(parents=True);(version/'approval.json').write_bytes(raw)
            selected=dict(schema=1,scope=pair.SCOPE,approval_sha256=digest);(root/'active.json').write_text(json.dumps(selected))
            self.assertEqual(pair.selected_version(root),version)
            for change in (dict(approval_sha256='../outside'),dict(schema=True),dict(program='/outside')):
                (root/'active.json').write_text(json.dumps(dict(selected,**change)))
                with self.assertRaises(ValueError):pair.selected_version(root)
            (root/'active.json').write_text(json.dumps(selected));(version/'approval.json').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'fixture-selected-policy'):pair.selected_version(root)

    def test_public_fixture_reads_refuse_aliases_and_oversized_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);file=root/'proof';file.write_bytes(b'public');link=root/'alias';link.symlink_to(file)
            self.assertEqual(pair.read(file,6),b'public')
            with self.assertRaises(OSError):pair.read(link,6)
            with self.assertRaises(ValueError):pair.read(file,5)
            os.link(file,root/'shared')
            with self.assertRaises(ValueError):pair.read(file,6)

    def test_deliberate_corruption_cannot_target_a_personal_or_production_store(self):
        with patch.object(pair.os,'geteuid',return_value=0),patch.object(Path,'mkdir') as mkdir:
            for destination in (Path('/usr/local/lib/nxrs-proxy'),Path('/run/foreign/proxy-artifact'),Path('/tmp/nxrs-sd-0123abcd/proxy-artifact')):
                with self.subTest(destination=destination),self.assertRaisesRegex(ValueError,'owned-run-policy-corruption-fixture'):
                    pair.select_invalid_policy(destination,Path('/unused'),b'{}')
            mkdir.assert_not_called()

if __name__=='__main__':unittest.main()
