"""Bounded public failure observations; never return log text or private paths."""
import asyncio
from collections import Counter
import json
from pathlib import Path
import re

CATEGORIES = frozenset(('Configuration','Authentication','Io','Protocol','Frame','Stale',
                        'Timeout','Encoder','Cleanup','Stopped'))
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


def source_counters(value):
    if not isinstance(value,dict): return None
    if not all(type(value.get(key)) is int and 0 <= value[key] <= 2**63-1 for key in SOURCE_KEYS): return None
    if value.get('last_failure') not in FAILURES or value.get('last_io') not in IO_KINDS: return None
    return {key:value[key] for key in (*SOURCE_KEYS,'last_failure','last_io')}


async def observe(root, source, adapter):
    result = dict(schema=1,scope='bounded-sdk-media-failure-observations-only',qualified=False,
                  ingest_categories={},source=None,adapter_exit=None)
    try:
        with (Path(root)/'ingest.log').open('rb') as stream: result['ingest_categories'] = categories(stream.read(16384))
    except OSError: pass
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
