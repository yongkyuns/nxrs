#!/usr/bin/env python3
"""Validate a bounded P4 transcript; return aggregates, never raw addresses."""
import json
import math
import re

PREFIX = 'EVENT_TRACE '
MASK = (1 << 32) - 1
FAULT = re.compile(r'EVENT_TRACE_FAILURE|PANIC|ASSERT|HardFault|Guru Meditation|SHA-256 comparison failed:', re.I)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def integer(value, low=0, high=MASK):
    require(type(value) is int and low <= value <= high, 'integer outside bounds')
    return value


def quantiles(values):
    values = sorted(values)
    require(bool(values), 'missing observations')
    return {key: values[max(0, math.ceil(len(values)*fraction)-1)]
            for key, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}


def checksum(total):
    result = 0
    mask = (1 << 64)-1
    for sequence in range(1, total+1):
        current = 0
        for word in (sequence, 0, ((sequence << 7) & MASK) ^ 0x9173ac5e,
                     ((~sequence) & MASK) ^ 0x13572468):
            current = ((((current << 11) | (current >> 53)) & mask) + word) & mask
        result = (result + current) & mask
    return result


def analyze(text, build, *, backend, policy, recording, samples, warmup):
    require(len(text.encode()) <= 2*1024*1024 and not FAULT.search(text), 'fault or oversized transcript')
    rows = []
    for line in text.splitlines():
        if line.startswith(PREFIX):
            row = json.loads(line[len(PREFIX):], object_pairs_hook=unique)
            require(isinstance(row, dict), 'record must be an object')
            rows.append(row)
    require(rows and rows[0].get('record') == 'metadata' and rows[-1].get('record') == 'result', 'incomplete framing')
    require(sum(r.get('record') == 'metadata' for r in rows) == 1 and
            sum(r.get('record') == 'result' for r in rows) == 1, 'duplicate framing')
    require(all(r.get('record') in {'metadata','placement','allocation','sample','event','result'} for r in rows), 'unknown record')
    meta, result = rows[0], rows[-1]
    require(backend in {'std','messaging','crossbeam'} and policy in {'equal','unequal'}, 'unknown case')
    integer(samples,1,128); integer(warmup,0,64)
    require(meta.get('schema') == 'event-path-trace-v1' and meta.get('build_id') == build['build_id'], 'wrong build/schema')
    require(meta.get('transport') == backend and meta.get('samples') == samples and meta.get('warmup') == warmup, 'wrong workload')
    require(meta.get('instrumented') is (build['mode']=='trace') and meta.get('recording') is recording and
            (not recording or meta['instrumented']), 'wrong instrumentation arm')
    require(meta.get('sender_priority') == (180 if policy=='equal' else 120) and
            meta.get('receiver_priority') == 180 and meta.get('policy') == 1, 'wrong FIFO schedule')
    sender = integer(meta.get('sender_tid'),1); receiver = integer(meta.get('receiver_tid'),1)
    require(sender != receiver, 'duplicate task identity')
    hz = integer(meta.get('clock_hz'),1,1_000_000_000)
    require(meta.get('counter_bits') == 32 and meta.get('counter_source') == 5, 'wrong native counter')
    window = integer(meta.get('window_ns'),1,10_000_000_000)
    require(window*hz < (1<<31)*1_000_000_000, 'ambiguous full trace window')
    require(meta.get('trace_capacity') == 4096 and meta.get('trace_bytes') == 65544, 'wrong finite storage layout')
    integer(meta.get('trace_address'),1); integer(meta.get('trace_region'),0,2)
    placements = [r for r in rows if r['record']=='placement']
    require(sorted(r.get('role') for r in placements) == ['receiver-stack','sender-stack'], 'missing/duplicate stack placement')
    for row in placements + [r for r in rows if r['record']=='allocation']:
        integer(row.get('address'),1); integer(row.get('bytes'),1); integer(row.get('region'),0,2)
        if row['record']=='allocation':
            require(row.get('scope')=='channel-constructor' and type(row.get('released_during_constructor')) is bool, 'invalid allocation scope')
            alignment = integer(row.get('alignment'),1)
            require(alignment & (alignment-1) == 0 and row['address'] % alignment == 0, 'invalid allocation alignment')
    allocations = [r for r in rows if r['record']=='allocation']
    require(0 < len(allocations) <= 64, 'missing/oversized constructor observations')
    observations = [r for r in rows if r['record']=='sample']
    require([r.get('sequence') for r in observations] == list(range(warmup+1,warmup+samples+1)), 'missing/duplicate/reordered sample')
    admission, delivery = [], []
    for row in observations:
        require(row.get('blocked_verified') is True, 'missing native blocked proof')
        for key in ('start','admission_return','receive_return'): integer(row.get(key))
        bound = integer(row.get('window_ns'),1,window)
        for key, values in [('admission_return', admission),('receive_return',delivery)]:
            delta = (row[key]-row['start']) & MASK
            require(0 < delta < 1<<31 and delta*1_000_000_000 <= bound*hz, 'counter outside monotonic bound')
            values.append(delta)
    for key in ('success','terminal_quiescence_verified','threads_joined','watchdog_joined','schedule_restored'):
        require(result.get(key) is True, 'missing completion proof: '+key)
    for key in ('overflow','physical_timing_qualified','priority_progress_qualified'):
        require(result.get(key) is False, 'invalid qualification/overflow: '+key)
    require(result.get('attempted') == samples+warmup == result.get('handled') and
            result.get('sample_count') == samples and result.get('checksum') == checksum(samples+warmup), 'payload/count mismatch')
    events = [r for r in rows if r['record']=='event']
    require(result.get('event_count') == len(events) <= 4096 and bool(events) is recording, 'event retention mismatch')
    stages = {key: [] for key in ['entry_to_ready','ready_to_receive','last_resume_to_receive']}
    irq_stack = []
    timeline = []
    elapsed = 0
    for index, row in enumerate(events):
        require(row.get('index') == index, 'missing/reordered event')
        tick = integer(row.get('ticks')); kind = integer(row.get('kind'),1,16)
        tid = integer(row.get('tid'),0); detail = integer(row.get('detail'))
        require(kind in {1,2,3,4,5,10,11,12,13,14,15,16}, 'unknown event')
        require(kind in {4,5} or tid in {sender,receiver}, 'unexpected participant')
        if index:
            delta = (tick-events[index-1]['ticks']) & MASK
            require(delta < 1<<31, 'reversed/ambiguous event clock')
            elapsed += delta
        require(elapsed*1_000_000_000 <= window*hz, 'trace outside wall window')
        timeline.append(elapsed)
        if kind==4: irq_stack.append(detail)
        if kind==5:
            require(irq_stack and irq_stack.pop()==detail, 'unbalanced IRQ trace')
    require(not irq_stack, 'truncated IRQ trace')
    require(sum(e['kind']>=10 for e in events) == (7*samples if recording else 0), 'extra application markers')
    ready_counts, resume_counts, irq_counts = [], [], []
    for sample in observations if recording else []:
        seq = sample['sequence']
        markers = {}
        for kind in (10,11,12,13,14,15,16):
            hits = [i for i,e in enumerate(events) if e['kind']==kind and e['detail']==seq]
            require(len(hits)==1, 'missing/duplicate application marker')
            i = hits[0]
            require(events[i]['tid'] == (receiver if kind in (12,13) else sender), 'wrong marker task')
            markers[kind] = i
        m = markers
        require(m[16]<m[12]<m[14]<m[10]<m[11]<m[15] and m[10]<m[13]<m[15], 'invalid causal marker order')
        require(any(e['kind']==2 and e['tid']==receiver and e['detail']==1 for e in events[m[12]+1:m[14]]), 'missing receive wait note')
        boundaries = {}
        for key,kind,previous in [('start',10,14),('admission_return',11,10),('receive_return',13,10)]:
            lag = (events[m[kind]]['ticks']-sample[key]) & MASK
            require(lag < 1<<31, 'marker precedes sample boundary')
            boundaries[key] = timeline[m[kind]]-lag
            require(timeline[m[previous]] <= boundaries[key] <= timeline[m[kind]], 'sample boundary outside its causal markers')
        require((max(timeline[m[11]],timeline[m[13]])-boundaries['start'])*1_000_000_000 <= sample['window_ns']*hz,
                'sample trace outside wall bound')
        ready = [i for i in range(m[10]+1,m[13]) if events[i]['kind']==1 and events[i]['tid']==receiver and events[i]['detail'] in (1,2)]
        resumes = [i for i in range(m[10]+1,m[13]) if events[i]['kind']==3 and events[i]['tid']==receiver]
        require(ready and resumes and ready[0]<resumes[-1], 'missing correlated ready/resume edges')
        stages['entry_to_ready'].append(timeline[ready[0]]-timeline[m[10]])
        stages['ready_to_receive'].append(timeline[m[13]]-timeline[ready[0]])
        stages['last_resume_to_receive'].append(timeline[m[13]]-timeline[resumes[-1]])
        ready_counts.append(len(ready)); resume_counts.append(len(resumes))
        irq_counts.append(sum(e['kind']==4 for e in events[m[10]:m[13]]))
    # Deliberate allowlist: no address, TID, console, host path or session fields.
    return {'schema':'event-path-trace-summary-v1','build_id':build['build_id'],
            'transport':backend,'policy':policy,'mode':build['mode'],'recording':recording,
            'samples':samples,'warmup':warmup,'clock_hz_configuration':hz,
            'admission_ticks':quantiles(admission),'delivery_ticks':quantiles(delivery),
            'correlated_ticks':{k:quantiles(v) for k,v in stages.items() if v},
            'ready_edges':ready_counts,'resume_edges':resume_counts,'irq_entries':irq_counts,
            'placement_regions':{r['role']:r['region'] for r in placements},
            'constructor_allocations':[{'bytes':r['bytes'],'alignment':r['alignment'],'region':r['region'],
                                        'released_during_constructor':r['released_during_constructor']} for r in allocations],
            'trace_region':meta['trace_region'],'trace_bytes':meta['trace_bytes'],
            'physical_timing_qualified':False,'priority_progress_qualified':False}
