"""Bounded public failure observations; never return log text or private paths."""
import asyncio
from collections import Counter
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
    if value.get('last_failure') not in FAILURES or value.get('last_io') not in IO_KINDS: return None
    result={key:value[key] for key in (*SOURCE_KEYS,'last_failure','last_io')}
    controls=('captured','control_generation','control_applied','control_refused','control_refreshes')
    if all(type(value.get(key)) is int and 0<=value[key]<=2**63-1 for key in controls) \
            and value.get('control_result') in ('ready','applied','refused','refreshed','fatal'):
        result.update({key:value[key] for key in (*controls,'control_result')})
    return result


async def observe(root, source, adapter):
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
    encoded=json.dumps(result,sort_keys=True,separators=(',',':'))
    assert len(encoded)<=4096
    return encoded
