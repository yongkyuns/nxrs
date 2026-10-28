"""Refusal, bounded progress and ownership controls for SDK API observations."""
import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
import detection_observer as observer


def value(publication=1,sequence=1,session='a'*32):
    source=dict(api_version='1',publication_id=publication,emitted_ms=1000,presence='person',localization=False,
        latest=dict(frame_sequence=sequence,captured_ms=900,inference_us=1,person_score_permille=900,objects=[],truncated=False),
        event_count=1,events=[dict(id=1,at_ms=900,kind='person_entered')])
    return dict(api_version='1',camera_id='camera',source_session_id=session,revision=publication,source_online=True,
        presence='person',stream_elapsed_ms=0,observation_age_ms=100,valid_for_ms=1500,frame_synchronized=False,source=source,event_gap=False)


def event(v):
    session=v['source_session_id'] or 'none';count=v['source']['event_count'] if v['source'] else 0
    return (b'event: detection\nid: '+(session+':'+str(count)).encode()+b'\ndata: '+json.dumps(v).encode()+b'\n\n').splitlines(keepends=True)


class Response:
    status=200
    headers={'Content-Type':'text/event-stream; charset=utf-8'}
    def __init__(self,lines):self.lines=list(lines);self.content=self;self.closed=False
    async def __aenter__(self):return self
    async def __aexit__(self,*args):self.closed=True
    async def readline(self):
        await asyncio.sleep(0)
        return self.lines.pop(0) if self.lines else b''


class Http:
    def __init__(self,response):self.response=response;self.args=None
    def get(self,url,**kwargs):self.args=(url,kwargs);return self.response


class SnapshotTests(unittest.TestCase):
    def test_exact_fixture_snapshot_and_registered_empty_source(self):
        original=value();raw=json.dumps(original).encode()
        self.assertEqual(observer.snapshot(raw,'camera','a'*32+':1'),original)
        for session in [None,'a'*32]:
            empty=dict(original,source=None,source_session_id=session,presence='unavailable',source_online=False,observation_age_ms=None)
            self.assertEqual(observer.snapshot(json.dumps(empty).encode(),'camera',(session or 'none')+':0'),empty)

    def test_wrong_camera_namespace_age_version_identity_and_duplicate_keys_refuse(self):
        original=value()
        cases=[dict(original,camera_id='other'),dict(original,api_version='2'),dict(original,source_session_id='private'),
            dict(original,frame_synchronized=True),dict(original,source_online=False),dict(original,observation_age_ms=5000),
            dict(original,valid_for_ms=0),dict(original,revision=True),dict(original,source=None)]
        for field,replacement in [('frame_sequence',0),('person_score_permille',0),('captured_ms',1001),('objects',[{}]),('truncated',True)]:
            changed=copy.deepcopy(original);changed['source']['latest'][field]=replacement;cases.append(changed)
        for changed in cases:
            with self.subTest(keys=list(changed)),self.assertRaises(AssertionError):
                observer.snapshot(json.dumps(changed).encode(),'camera','a'*32+':1')
        for raw in [b' '*8193,b'{"camera_id":"camera","camera_id":"camera"}',b'{"x":NaN}']:
            with self.assertRaises(AssertionError):observer.snapshot(raw,'camera','a'*32+':1')
        with self.assertRaises(AssertionError):observer.snapshot(json.dumps(original).encode(),'camera','b'*32+':1')

    def test_evidence_requires_all_three_distinct_progressing_sources_and_both_expiries(self):
        confirmed=[dict(presence='person',publications=2,session=c*32,response_closed=True) for c in 'abc']
        unavailable=[dict(presence='unavailable',response_closed=True) for _ in range(2)]
        proof=observer.evidence(confirmed,unavailable);observer.validate(proof)
        for change in [dict(confirmed_publications=True),dict(source_session_sha256=['a'*64]*3),dict(source_stall_expired=False),
                       dict(peer_revocation_expired=False),dict(frame_synchronized=True),dict(media_frame_alignment_qualified=True),
                       dict(response_owners_joined=False),dict(physical_qualified=True),dict(deployment_permitted=True)]:
            with self.assertRaises(AssertionError):observer.validate(dict(proof,**change))
        for a,b in [(confirmed[:2],unavailable),(confirmed,unavailable[:1]),([confirmed[0]]*3,unavailable)]:
            with self.assertRaises(AssertionError):observer.evidence(a,b)


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def observe(self,response,**kwargs):
        http=Http(response)
        result=await observer.observe(http,'http://127.0.0.1/detections','fixture-token','camera','person',**kwargs)
        self.assertTrue(response.closed)
        self.assertEqual(http.args[1],dict(headers={'Authorization':'Bearer fixture-token'},timeout=None,read_bufsize=4096))
        return result

    async def test_two_advancing_observations_and_changed_source_are_required(self):
        response=Response(event(value())+event(value())+event(value(2,2)))
        result=await self.observe(response);self.assertEqual(result['publications'],2)
        self.assertEqual(result['messages'],3)
        response=Response(event(value())+event(value(2,2))+event(value(1,1,'b'*32))+event(value(2,2,'b'*32)))
        result=await self.observe(response,different_from='a'*32);self.assertEqual(result['session'],'b'*32)

    async def test_replay_stale_heartbeat_eof_oversize_and_duplicate_sse_fields_close_response(self):
        sources=[event(value())+event(value()),event(value(2,2))+event(value(1,1)),event(value())+event(value(2,1)),
            [b'x'*8193+b'\n'],[b'event: detection\n',b'event: detection\n']]
        for lines in sources:
            response=Response(lines)
            with self.subTest(lines=len(lines)),self.assertRaises(AssertionError):await self.observe(response)
            self.assertTrue(response.closed)

    async def test_unavailable_is_observed_and_cancellation_joins_response(self):
        empty=dict(value(),presence='unavailable',source=None,source_session_id=None,source_online=False,observation_age_ms=None)
        response=Response(event(empty));result=await observer.observe(Http(response),'http://127.0.0.1/detections','fixture-token','camera','unavailable')
        self.assertEqual(result['presence'],'unavailable');self.assertTrue(response.closed)
        entered=asyncio.Event()
        class Held(Response):
            async def readline(self):entered.set();await asyncio.Event().wait()
        held=Held([]);task=asyncio.create_task(self.observe(held));await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(held.closed)
