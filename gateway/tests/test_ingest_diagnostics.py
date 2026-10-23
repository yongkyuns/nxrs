import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import ingest_diagnostics as diagnostic

class FailureObservationsTests(unittest.TestCase):
    def test_private_text_unknown_categories_and_unbounded_logs_are_not_exported(self):
        secret='private-key-path-or-token'
        raw=(secret+'\nnxrs-camera-ingest: Authentication\n'
             'nxrs-camera-ingest: retired source/encoder (Encoder); retrying\n'
             'nxrs-camera-ingest: '+secret+'\n').encode()
        result=diagnostic.categories(raw)
        self.assertEqual(result,{'Authentication':1,'Encoder':1})
        self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(diagnostic.categories(b'x'*16384+b'\nnxrs-camera-ingest: Authentication\n'),{})
    def test_only_bounded_typed_counters_and_fixed_failure_names_are_exported(self):
        values=dict(active=0,peak=1,connections=2,disconnected=2,frames=0,busy=0,errors=2,
                    last_failure='Read',last_io='permission-denied',private='secret')
        result=diagnostic.source_counters(values)
        self.assertNotIn('private',result)
        for key,value in [('active',True),('frames',2**64),('last_failure','secret'),('last_io','/private/path')]:
            with self.subTest(key=key):self.assertIsNone(diagnostic.source_counters(dict(values,**{key:value})))
    def test_failure_observer_keeps_private_data_out_and_never_claims_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'ingest.log').write_text('SECRET\nnxrs-camera-ingest: Timeout\n')
            source=mock.Mock();source.command=mock.AsyncMock(side_effect=RuntimeError('PRIVATE'))
            adapter=mock.Mock();adapter.poll.return_value=2
            encoded=asyncio.run(diagnostic.observe(root,source,adapter));result=json.loads(encoded)
            self.assertFalse(result['qualified']);self.assertEqual(result['ingest_categories'],{'Timeout':1})
            self.assertEqual(result['adapter_exit'],2);self.assertIsNone(result['source'])
            self.assertNotIn('SECRET',encoded);self.assertNotIn('PRIVATE',encoded)
if __name__=='__main__':unittest.main()
