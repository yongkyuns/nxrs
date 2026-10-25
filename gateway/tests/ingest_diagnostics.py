"""Bounded public failure observations; never return log text or private paths."""
import asyncio
from collections import Counter
from itertools import islice
import json
from pathlib import Path
import re

CATEGORIES = frozenset(('Configuration','Authentication','Io','Protocol','Frame','Stale',
                        'Timeout','Encoder','Cleanup','Stopped'))
STAGES = frozenset(('CameraSetup','CameraFrames','EncodedRelay','EncoderProgress','PublisherProgress'))
SOURCE_KEYS = ('active','peak','connections','disconnected','frames','busy','errors')
FAILURES = frozenset(('None','Clock','RequestDeadline','ResponseDeadline','IdleDeadline',
                     'InvalidRequest','InvalidFrame','UnexpectedInput','Configure','Read','Write','Stream'))
IO_KINDS = frozenset(('none','permission-denied','out-of-memory','timed-out','invalid-data','eof','other'))
MAX_OBSERVATION_BYTES = 8192
CONTROL_REASONS = {
    'nxrs-gateway: media supervision stopped unexpectedly':'supervision-stopped',
    'nxrs-gateway: media management health deadline expired':'management-deadline',
    'nxrs-gateway: credential activation window unavailable':'credential-window',
    'nxrs-gateway: finite credential activation window required for strict enforcement':'missing-credential-window',
    'nxrs-gateway: gateway server stopped unexpectedly':'server-stopped',
}



def categories(raw):
    counts = Counter()
    for line in raw[:16384].decode('ascii',errors='replace').splitlines():
        match = re.fullmatch(r'nxrs-camera-ingest: (?:retired source/encoder \(([A-Za-z]+)\); retrying|([A-Za-z]+))',line)
        if match:
            value = match[1] or match[2]
            if value in CATEGORIES: counts[value] += 1
    return dict(sorted(counts.items()))


def stage_failures(raw):
    counts = Counter()
    for line in raw[:16384].decode('ascii',errors='replace').splitlines():
        match = re.fullmatch(r'nxrs-camera-ingest: stage-failure \(([A-Za-z]+): ([A-Za-z]+)\)',line)
        if match and match[1] in STAGES and match[2] in CATEGORIES:
            counts[match[1]+':'+match[2]] += 1
    return dict(sorted(counts.items()))


def source_counters(value):
    if not isinstance(value,dict): return None
    if not all(type(value.get(key)) is int and 0 <= value[key] <= 2**63-1 for key in SOURCE_KEYS): return None
    if not isinstance(value.get('last_failure'),str) or value['last_failure'] not in FAILURES \
            or not isinstance(value.get('last_io'),str) or value['last_io'] not in IO_KINDS: return None
    result={key:value[key] for key in (*SOURCE_KEYS,'last_failure','last_io')}
    controls=('captured','control_generation','control_applied','control_refused','control_refreshes')
    if all(type(value.get(key)) is int and 0<=value[key]<=2**63-1 for key in controls) \
            and value.get('control_result') in ('ready','applied','refused','refreshed','fatal'):
        result.update({key:value[key] for key in (*controls,'control_result')})
    return result


VIEWER_STATES = frozenset(('not-started','consuming','retired','cancelled'))
VIEWER_FAILURES = frozenset(('none','ended','dimensions','invariant','receive-error'))
CONNECTION_STATES = frozenset(('new','connecting','connected','disconnected','failed','closed'))


def project_viewer(value):
    if not isinstance(value,dict):return None
    result={}
    for key in ['frames','rtp_streams','rtp_packets_received']:
        if type(value.get(key)) is int and 0<=value[key]<=2**63-1:result[key]=value[key]
    for key,allowed in [('consumer',VIEWER_STATES),('failure',VIEWER_FAILURES),('connection',CONNECTION_STATES)]:
        if isinstance(value.get(key),str) and value[key] in allowed:result[key]=value[key]
    if type(value.get('rtp_observed')) is bool:result['rtp_observed']=value['rtp_observed']
    return result


async def viewer_state(reader):
    result=dict(frames=getattr(reader,'count',None),failure=getattr(reader,'failure','none'),rtp_observed=False)
    task=getattr(reader,'task',None)
    result['consumer']='not-started' if task is None else 'cancelled' if task.cancelled() else 'retired' if task.done() else 'consuming'
    try:
        result['connection']=reader.peer.connectionState
        report=await asyncio.wait_for(reader.peer.getStats(),.25)
        rows=list(islice(report.values(),65))
        if len(rows)<=64:
            video=[v for v in rows if getattr(v,'type',None)=='inbound-rtp' and getattr(v,'kind',None)=='video']
            counts=[getattr(v,'packetsReceived',None) for v in video]
            if all(type(n) is int and 0<=n<=2**63-1 for n in counts) and sum(counts)<=2**63-1:
                result.update(rtp_observed=True,rtp_streams=len(video),rtp_packets_received=sum(counts))
    except Exception:pass
    return project_viewer(result)


def process_state(process):
    if process is None:return dict(observed=False)
    try:status=process.poll()
    except Exception:return dict(observed=False)
    if status is None:return dict(observed=True,running=True)
    if type(status) is int and -255<=status<=255:return dict(observed=True,running=False,exit=status)
    return dict(observed=False)


def project_observations(value):
    """Revalidate child output before it crosses the composed report boundary."""
    if not isinstance(value,dict) or type(value.get('schema')) is not int or value['schema']!=1 \
            or value.get('scope')!='bounded-sdk-media-failure-observations-only' or value.get('qualified') is not False:
        return None
    def counts(raw,allowed):
        return {k:v for k,v in raw.items() if isinstance(k,str) and k in allowed
            and type(v) is int and 0<=v<=2**63-1} if isinstance(raw,dict) else {}
    result=dict(schema=1,scope='bounded-sdk-media-failure-observations-only',qualified=False,
        ingest_categories=counts(value.get('ingest_categories'),CATEGORIES),
        ingest_stages=counts(value.get('ingest_stages'),{s+':'+c for s in STAGES for c in CATEGORIES}),
        source=source_counters(value.get('source')),adapter_exit=None)
    status=value.get('adapter_exit')
    if type(status) is int and -255<=status<=255:result['adapter_exit']=status
    reasons=value.get('control_reasons')
    if isinstance(reasons,list):result['control_reasons']=[r for r in reasons
        if isinstance(r,str) and r in CONTROL_REASONS.values()][-8:]
    frames=value.get('viewer_frames')
    if isinstance(frames,list):result['viewer_frames']=[v for v in frames[:8] if type(v) is int and 0<=v<=2**63-1]
    viewers=value.get('viewers')
    if isinstance(viewers,list):result['viewers']=[v for v in (project_viewer(v) for v in viewers[:8]) if v is not None]
    processes=value.get('processes')
    if isinstance(processes,dict):
        result['processes']={}
        for role in ['control','media','adapter']:
            item=processes.get(role)
            if not isinstance(item,dict) or type(item.get('observed')) is not bool:continue
            if item['observed'] is False:result['processes'][role]=dict(observed=False)
            elif item.get('running') is True:result['processes'][role]=dict(observed=True,running=True)
            elif item.get('running') is False and type(item.get('exit')) is int and -255<=item['exit']<=255:
                result['processes'][role]=dict(observed=True,running=False,exit=item['exit'])
    return result


async def observe(root, source, adapter, *, control=None, media=None, readers=()):
    result = dict(schema=1,scope='bounded-sdk-media-failure-observations-only',qualified=False,
                  ingest_categories={},ingest_stages={},source=None,adapter_exit=None)
    counts=Counter();stages=Counter()
    for name in ('ingest.log','ingest-revocation.log'):
        try:
            with (Path(root)/name).open('rb') as stream:raw=stream.read(16384)
            counts.update(categories(raw));stages.update(stage_failures(raw))
        except OSError:pass
    result['ingest_categories']=dict(sorted(counts.items()))
    result['ingest_stages']=dict(sorted(stages.items()))
    if source is not None:
        try: result['source'] = source_counters(await asyncio.wait_for(source.command('status'),2))
        except Exception: pass
    if adapter is not None:
        try:
            status = adapter.poll()
            if status is None or (type(status) is int and -255 <= status <= 255): result['adapter_exit'] = status
        except Exception: pass
    result['processes']={name:process_state(process) for name,process in
        [('control',control),('media',media),('adapter',adapter)]}
    result['viewer_frames']=[reader.count for reader in readers[:8]
        if type(getattr(reader,'count',None)) is int and 0<=reader.count<=2**63-1]
    result['viewers']=[await viewer_state(reader) for reader in readers[:8]]
    result['control_reasons']=[]
    try:
        with (Path(root)/'control.log').open('rb') as stream:
            stream.seek(0,2);stream.seek(max(0,stream.tell()-8192));raw=stream.read(8192)
        result['control_reasons']=[CONTROL_REASONS[line] for line in raw.decode('ascii',errors='replace').splitlines()
            if line in CONTROL_REASONS][-8:]
    except OSError:pass
    result=project_observations(result)
    encoded=json.dumps(result,sort_keys=True,separators=(',',':'))
    assert len(encoded)<=MAX_OBSERVATION_BYTES
    return encoded
