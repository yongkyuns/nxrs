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
    def test_only_fixed_stage_category_pairs_are_exported(self):
        raw=(b'nxrs-camera-ingest: stage-failure (CameraFrames: Timeout)\n'
             b'nxrs-camera-ingest: stage-failure (CameraSetup: Io)\n'
             b'nxrs-camera-ingest: stage-failure (PRIVATE: Timeout)\n'
             b'nxrs-camera-ingest: stage-failure (EncodedRelay: PRIVATE)\n'
             b'nxrs-camera-ingest: stage-failure (EncodedRelay: Io) SECRET\n')
        self.assertEqual(diagnostic.stage_failures(raw),{'CameraFrames:Timeout':1,'CameraSetup:Io':1})
        self.assertEqual(diagnostic.stage_failures(b'x'*16384+b'\n'+raw),{})
    def test_only_bounded_typed_counters_and_fixed_failure_names_are_exported(self):
        values=dict(active=0,peak=1,connections=2,disconnected=2,frames=0,busy=0,errors=2,
                    last_failure='Read',last_io='permission-denied',private='secret')
        result=diagnostic.source_counters(values)
        self.assertNotIn('private',result)
        for key,value in [('active',True),('frames',2**64),('last_failure','secret'),('last_io','/private/path')]:
            with self.subTest(key=key):self.assertIsNone(diagnostic.source_counters(dict(values,**{key:value})))
    def test_failure_observer_keeps_private_data_out_and_never_claims_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'ingest.log').write_text('SECRET\nnxrs-camera-ingest: Timeout\nnxrs-camera-ingest: stage-failure (CameraSetup: Timeout)\n')
            (root/'ingest-revocation.log').write_text('nxrs-camera-ingest: stage-failure (CameraFrames: Io)\n')
            source=mock.Mock();source.command=mock.AsyncMock(side_effect=RuntimeError('PRIVATE'))
            adapter=mock.Mock();adapter.poll.return_value=2
            encoded=asyncio.run(diagnostic.observe(root,source,adapter));result=json.loads(encoded)
            self.assertFalse(result['qualified']);self.assertEqual(result['ingest_categories'],{'Timeout':1})
            self.assertEqual(result['ingest_stages'],{'CameraSetup:Timeout':1,'CameraFrames:Io':1})
            self.assertEqual(result['adapter_exit'],2);self.assertIsNone(result['source'])
            self.assertNotIn('SECRET',encoded);self.assertNotIn('PRIVATE',encoded)
    def test_optional_sdk_controls_keep_only_fixed_results_and_bounded_counts(self):
        base=dict(active=0,peak=1,connections=3,disconnected=3,frames=8,busy=0,errors=1,
                  last_failure='Stream',last_io='none',captured=12,control_generation=2,
                  control_applied=1,control_refused=3,control_refreshes=1,control_result='applied',private='SECRET')
        result=diagnostic.source_counters(base)
        self.assertEqual(result['control_refused'],3);self.assertNotIn('SECRET',json.dumps(result))
        for key,value in [('control_generation',True),('control_applied',2**64),('control_result','SECRET')]:
            result=diagnostic.source_counters(dict(base,**{key:value}))
            self.assertFalse(any(name.startswith('control_') for name in result))
            self.assertNotIn('SECRET',json.dumps(result))

class ForwardedFailureObservationsTests(unittest.TestCase):
    def test_observed_control_media_and_viewer_states_are_fixed_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'control.log').write_text('SECRET\nnxrs-gateway: media management health deadline expired\n')
            source=mock.Mock();source.command=mock.AsyncMock(return_value=dict(active=1,peak=1,connections=2,
                disconnected=1,frames=80,busy=0,errors=0,last_failure='None',last_io='none',private='SECRET'))
            control=mock.Mock();control.poll.return_value=1
            media=mock.Mock();media.poll.return_value=None
            adapter=mock.Mock();adapter.poll.return_value=None
            reader=mock.Mock();reader.count=0
            result=json.loads(asyncio.run(diagnostic.observe(root,source,adapter,control=control,media=media,readers=[reader])))
            self.assertEqual(result['processes'],dict(control=dict(observed=True,running=False,exit=1),
                media=dict(observed=True,running=True),adapter=dict(observed=True,running=True)))
            self.assertEqual(result['viewer_frames'],[0]);self.assertEqual(result['source']['frames'],80)
            self.assertEqual(result['control_reasons'],['management-deadline'])
            self.assertNotIn('SECRET',json.dumps(result));self.assertFalse(result['qualified'])

    def test_projection_drops_unknown_or_malformed_process_counter_and_source_values(self):
        value=dict(schema=1,scope='bounded-sdk-media-failure-observations-only',qualified=False,
            ingest_categories={'Io':2,'SECRET':9,'Timeout':True},ingest_stages={'CameraFrames:Stale':3,'SECRET':8},
            source=dict(active=0,peak=1,connections=1,disconnected=1,frames=0,busy=0,errors=0,
                last_failure=['SECRET'],last_io='none'),adapter_exit=True,viewer_frames=[0,3,True,-1,2**64,'SECRET'],
            control_reasons=['credential-window','SECRET'],processes={
                'control':dict(observed=True,running=False,exit=1,token='SECRET'),
                'media':dict(observed=True,running=True,argv='SECRET'),
                'adapter':dict(observed=True,running=False,exit=True),'SECRET':dict(observed=False)})
        result=diagnostic.project_observations(value)
        self.assertEqual(result['ingest_categories'],{'Io':2});self.assertEqual(result['ingest_stages'],{'CameraFrames:Stale':3})
        self.assertIsNone(result['source']);self.assertIsNone(result['adapter_exit'])
        self.assertEqual(result['viewer_frames'],[0,3]);self.assertEqual(result['control_reasons'],['credential-window'])
        self.assertEqual(set(result['processes']),{'control','media'})
        self.assertNotIn('SECRET',json.dumps(result))
        for change in [dict(schema=True),dict(qualified=True),dict(scope='SECRET')]:
            self.assertIsNone(diagnostic.project_observations(dict(value,**change)))
        for bad in [None,[],True,'SECRET']:self.assertIsNone(diagnostic.project_observations(bad))


class ViewerFailureObservationsTests(unittest.TestCase):
    def test_receiver_counters_distinguish_packets_from_decoded_progress_without_identifiers(self):
        from types import SimpleNamespace
        reader=SimpleNamespace(count=0,failure='none',task=None,peer=SimpleNamespace(connectionState='connected'))
        reader.peer.getStats=mock.AsyncMock(return_value={
            'PRIVATE-SSRC':SimpleNamespace(type='inbound-rtp',kind='video',packetsReceived=10,address='PRIVATE'),
            'PRIVATE-TRANSPORT':SimpleNamespace(type='transport',packetsReceived=999)})
        value=asyncio.run(diagnostic.viewer_state(reader))
        self.assertEqual(value,dict(frames=0,failure='none',consumer='not-started',connection='connected',
            rtp_observed=True,rtp_streams=1,rtp_packets_received=10))
        self.assertNotIn('PRIVATE',json.dumps(value))
        reader.peer.getStats=mock.AsyncMock(side_effect=RuntimeError('PRIVATE'))
        self.assertFalse(asyncio.run(diagnostic.viewer_state(reader))['rtp_observed'])

    def test_viewer_projection_rejects_private_strings_and_invalid_counts(self):
        value=dict(frames=True,rtp_streams=-1,rtp_packets_received=2**64,consumer='PRIVATE',failure='PRIVATE',
            connection='PRIVATE',rtp_observed='PRIVATE',address='PRIVATE')
        self.assertEqual(diagnostic.project_viewer(value),{})
        self.assertIsNone(diagnostic.project_viewer('PRIVATE'))

if __name__=='__main__':unittest.main()
