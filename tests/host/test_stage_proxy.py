"""Review preparation is private, exact and never itself release approval."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('stage_proxy',ROOT/'gateway/tools/stage_proxy.py')
stage=importlib.util.module_from_spec(spec);spec.loader.exec_module(stage)


class StageProxyTests(unittest.TestCase):
    def proof(self):
        return dict(binary_sha256=hashlib.sha256(b'fixture proxy').hexdigest(),qualification_sha256=hashlib.sha256(b'{}').hexdigest(),
            sources={},libraries={name:'a'*64 for name in stage.LIBRARIES.values()})

    def git(self,args,**kwargs):
        return subprocess.CompletedProcess(args,0,stdout=(b'1'*40 if args[-1]=='HEAD' else b''))

    def test_preparation_copies_exact_artifacts_private_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d,patch.object(stage.os,'geteuid',return_value=1000), \
                patch.object(stage.subprocess,'run',side_effect=self.git),patch.object(stage.candidate,'source_inputs',return_value={}), \
                patch.object(stage.evidence,'verify',return_value=self.proof()):
            root=Path(d);runtime=root/'runtime';(runtime/'bin').mkdir(parents=True)
            (runtime/'bin/nginx').write_bytes(b'fixture proxy');(runtime/'qualification.json').write_bytes(b'{}')
            bundle=root/'new';result=stage.prepare(bundle,runtime)
            self.assertTrue(result['operator_approval_required']);self.assertFalse(result['deployment_permitted'])
            self.assertEqual((bundle/'nginx').read_bytes(),b'fixture proxy')
            review=json.loads((bundle/'review.json').read_bytes());self.assertEqual(set(review['libraries']),set(stage.LIBRARIES))
            self.assertEqual(review['qualification_sha256'],self.proof()['qualification_sha256'])
            self.assertEqual(result['review_sha256'],stage.evidence.digest(bundle/'review.json'))
            self.assertEqual(bundle.stat().st_mode&0o777,0o700)
            self.assertEqual((bundle/'review.json').stat().st_mode&0o777,0o600)
            self.assertEqual((bundle/'nginx').stat().st_mode&0o777,0o700)
            with self.assertRaises(FileExistsError):stage.prepare(bundle,runtime)

    def test_root_and_dirty_checkout_refuse_before_creating_a_bundle(self):
        with tempfile.TemporaryDirectory() as d,patch.object(stage.subprocess,'run') as command:
            out=Path(d)/'absent'
            with patch.object(stage.os,'geteuid',return_value=0),self.assertRaisesRegex(ValueError,'stage-as-unprivileged'):
                stage.prepare(out)
            command.assert_not_called();self.assertFalse(out.exists())
            command.return_value=subprocess.CompletedProcess([],0,stdout=b' M changed')
            with patch.object(stage.os,'geteuid',return_value=1000),self.assertRaisesRegex(ValueError,'clean-proxy-review'):
                stage.prepare(out)
            self.assertFalse(out.exists())

    def test_changed_artifact_receipt_or_extra_library_never_emits_a_review(self):
        for kind in ['binary','receipt','library']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d,patch.object(stage.os,'geteuid',return_value=1000), \
                    patch.object(stage.subprocess,'run',side_effect=self.git),patch.object(stage.candidate,'source_inputs',return_value={}):
                root=Path(d);runtime=root/'runtime';(runtime/'bin').mkdir(parents=True)
                (runtime/'bin/nginx').write_bytes(b'changed' if kind=='binary' else b'fixture proxy')
                (runtime/'qualification.json').write_bytes(b'changed' if kind=='receipt' else b'{}')
                proof=self.proof()
                if kind=='library':proof['libraries']['/usr/lib/extra.so']='a'*64
                with patch.object(stage.evidence,'verify',return_value=proof),self.assertRaises(ValueError):stage.prepare(root/'out',runtime)
                self.assertFalse((root/'out/review.json').exists())

    def test_review_epoch_bounds_match_the_canonical_dates(self):
        from datetime import datetime
        review=json.loads((ROOT/'security/gateway-proxy-dependencies.json').read_bytes())['advisory_review']
        for name in ['reviewed_at','valid_until']:
            self.assertEqual(review[name+'_unix'],int(datetime.fromisoformat(review[name]).timestamp()))


if __name__=='__main__':unittest.main()
