"""Strict accounting and per-boot metrics; no pooled distributions or rankings."""
import hashlib,json,math,re
MAX_U64=(1<<64)-1

def require(ok,message):
    if not ok:raise ValueError(message)
def unique(items):
    result={}
    for k,v in items:
        require(k not in result,'duplicate JSON field');result[k]=v
    return result
def integer(x,low=0,high=0xffffffff):
    require(type(x) is int and low<=x<=high,'integer out of range');return x
def delta(end,start):
    value=(end-start)&0xffffffff;require(value<1<<31,'ambiguous/backwards interval');return value
def stats(values):
    values=sorted(values)
    return None if not values else dict(count=len(values),**{name:values[max(0,math.ceil(len(values)*q)-1)] for name,q in [('p50',.5),('p99',.99),('p99_9',.999),('max',1)]})
def records(raw):
    require(len(raw)<4*1024*1024,'oversized capture')
    text=raw.decode('utf-8');require(not re.search(r'MPSC_FAILURE|panic|Assertion failed|Guru Meditation|CPU halted|SHA-256 comparison failed',text,re.I),'firmware fault')
    require(text.count('MPSC_DONE')==1,'missing/duplicate completion')
    lines=[line[5:] for line in text.splitlines() if line.startswith('MPSC ')]
    require(len(lines)<2400 and all(len(s)<1024 for s in lines),'oversized records')
    return [json.loads(s,object_pairs_hook=unique) for s in lines]

def analyze(raw,expected=None):
    rows=records(raw)
    require(all(r.get('record') in {'metadata','sample','heap','participant','counter-pair','result','release','producer-final'} for r in rows),'unknown record')
    def group(kind):return [r for r in rows if r['record']==kind]
    require(len(group('metadata'))==len(group('result'))==1,'missing/duplicate metadata/result')
    m=group('metadata')[0];result=group('result')[0]
    require(m['schema']=='mpsc-comparison-v3' and m['runtime'] in {'host','nuttx','zephyr'},'scope')
    require(m['mode'] in {'sustained','stream','paced','burst','overload','backpressure'} and m['backend'] in {'std','native'},'case')
    require((m['runtime'],m['backend']) in {('host','std'),('nuttx','std'),('nuttx','native'),('zephyr','native')},'runtime/backend')
    require(m['native'] is (m['runtime']!='host'),'native label')
    require(m['clock_hz'] in (1000000,240000000) and m['warmup']==128,'clock/warmup')
    n=integer(m['producers'],1,4);require(n in (1,4),'producers')
    sustained=m['mode']=='sustained'
    count=integer(m['samples'],1,131072 if sustained else 2048);require(count%n==0,'per-producer work')
    require(m['payload_bytes'] in (16,64) and m['capacity'] in (8,64),'payload/capacity')
    prefix=m['capacity'] if m['mode']=='backpressure' else 0
    require(m['warm_blocked_senders']==n,'cold producer synchronization')
    require(m['prefix']==prefix and m['blocked_senders']==(n if prefix else 0),'backpressure proof')
    if expected:
        for k,v in expected.items():
            if k!='accounting':require(m[k]==v,'mismatched '+k)
    require(result['success'] is True and result['joined'] is True,'unjoined lifecycle')
    roles=group('participant');require(len(roles)==n+2 and {p['role'] for p in roles}==set(range(n+2)),'participant census')
    require(len({p['tid'] for p in roles})==n+2,'duplicate native task')
    for p in roles:
        priority={'nuttx':180,'zephyr':20,'host':0}[m['runtime']]
        require(p['start_priority']==p['end_priority']==priority,'participant priority')
        require(p['start_policy']==p['end_policy']==(1 if m['runtime']=='nuttx' else 0),'participant policy')
        if m['native']:require(p['internal']==1 and 0<p['stack_used']<=p['stack_bytes'],'stack placement/usage')
        integer(p['cpu_ns'],0,MAX_U64)
    observer='host-phase-only' if m['runtime']=='host' else 'queue-object' if m['backend']=='native' else 'semaphore-state-and-phase'
    require(m.get('wait_observer')==observer,'blocked-wait observer')
    accounting=all(p['cpu_ns']!=MAX_U64 for p in roles)
    require(accounting or all(p['cpu_ns']==MAX_U64 for p in roles),'partial CPU observations')
    if expected and 'accounting' in expected:require(accounting==expected['accounting'],'CPU image mismatch')
    wall=integer(m['wall_ns'],0,8_000_000_000)
    ticks=delta(m['end'],m['begin']);require(abs(ticks*1000000000/m['clock_hz']-wall)<=30_000_000,'native/precise window mismatch')
    if accounting:require(sum(p['cpu_ns'] for p in roles)<=wall+30_000_000,'runtime outside observation window')
    for key in ('begin','end','last_received','recovery_begin','joined_ticks','idle_ticks'):integer(m[key])
    require(m['sample_storage_bytes']==2240*28,'observation storage')
    samples=group('sample');total=128 if sustained else 128+count+prefix
    require(len(samples)==total and {s['id'] for s in samples}==set(range(total)),'missing/duplicate offered work')
    accepted=[];delivered=[]
    for s in samples:
        for k in ('id','planned','offered','returned','received','finished','order','flags'):integer(s[k])
        require(s['flags'] in (5,11),'lost/corrupt owned work')
        delta(s['returned'],s['offered']);delta(s['offered'],s['planned'])
        if s['flags']==11:
            require(delta(s['received'],s['offered'])<=delta(s['finished'],s['offered']),'stage order')
            accepted.append(s);delivered.append(s)
        elif s['id']<128 or s['id']>=128+count:require(False,'warmup/prefix rejection')
    require(result['admitted']==result['handled']==len(delivered)+(count if sustained else 0),'accounting mismatch')
    extra=(count*(2*128+count-1)//2) if sustained else 0
    require(result['checksum']==(sum(s['id'] for s in delivered)+extra)&0xffffffff,'checksum mismatch')
    require({s['order'] for s in delivered}==set(range(len(delivered))),'dispatch order gaps/duplicates')
    measured=sorted([s for s in samples if 128<=s['id']<128+count],key=lambda s:s['id'])
    handled=[s for s in measured if s['flags']==11]
    if m['mode'] in ('stream','backpressure'):require(len(handled)==count,'blocking admission rejected work')
    finals=group('producer-final')
    require((len(finals)==n and {r['producer'] for r in finals}==set(range(n)) and all(r['count']==count//n for r in finals)) if sustained else not finals,'sustained producer accounting')
    per_producer=[]
    for producer in range(n):
        offered=[s for s in measured if (s['id']-128)%n==producer]
        got=[s for s in handled if (s['id']-128)%n==producer]
        order=[s['order'] for s in got];require(order==sorted(order),'per-producer FIFO violated')
        per_producer.append(dict(producer=producer,offered=count//n if sustained else len(offered),admitted=count//n if sustained else len(got),rejected=0 if sustained else len(offered)-len(got),delivery_ticks=stats([delta(s['received'],s['offered']) for s in got])))
    require(sustained or handled,'no delivered work')
    if not sustained:require(m['last_received']==max(handled,key=lambda s:s['order'])['received'],'last-delivery boundary')
    delivered_count=count if sustained else len(handled)
    traffic_ticks=delta(m['last_received'],m['begin'])
    require(traffic_ticks>0,'empty traffic interval')
    heaps=group('heap');require(len(heaps)==3 and {h['stage'] for h in heaps}=={'before','live','after'},'heap snapshots')
    for h in heaps:
        for k in ('used','peak_since_boot','free','largest_free'):integer(h[k],0,16*1024*1024)
        require(h['used']<=h['peak_since_boot'] and h['largest_free']<=h['free'],'heap bounds')
    reads=group('counter-pair');require(len(reads)==64,'counter control')
    cpu=sum(p['cpu_ns'] for p in roles) if accounting else None
    output=dict(schema='mpsc-comparison-summary-v3',**{k:m[k] for k in ('clock_hz','build_id','runtime','backend','wait_observer','mode','producers','payload_bytes','capacity','samples','native','sample_storage_bytes')},offered=count,admitted=delivered_count,rejected=count-delivered_count,delivered=delivered_count,
        admitted_identity_sha256=hashlib.sha256(json.dumps(list(range(128,128+count)) if sustained else [s['id'] for s in handled]).encode()).hexdigest(),
        delivered_per_nominal_second=delivered_count*m['clock_hz']/traffic_ticks,traffic_ticks=traffic_ticks,protocol_ticks=ticks,
        delivery_ticks=stats([delta(s['received'],s['offered']) for s in handled]),release_to_receive_ticks=stats([delta(s['received'],s['planned']) for s in handled]),
        admission_ticks=stats([delta(s['returned'],s['offered']) for s in measured]),successful_admission_ticks=stats([delta(s['returned'],s['offered']) for s in handled]),
        rejected_admission_ticks=stats([delta(s['returned'],s['offered']) for s in measured if s['flags']==5]),
        release_lateness_ticks=stats([delta(s['offered'],s['planned']) for s in measured]),validation_ticks=stats([delta(s['finished'],s['received']) for s in handled]),
        experimental_threshold_us=None if sustained else {str(us):sum(delta(s['received'],s['planned'])>m['clock_hz']*us/1000000 for s in handled) for us in (100,1000,10000)},product_deadline_us=None,
        blocked_senders=m['blocked_senders'],backpressure_recovery_ticks=delta(m['last_received'],m['recovery_begin']) if prefix else None,
        joined_ticks=m['joined_ticks'],accounting_available=accounting,total_participant_cpu_ns=cpu,cpu_ns_per_offer=cpu/count if cpu is not None else None,cpu_ns_per_delivered=cpu/delivered_count if cpu is not None else None,
        idle_ticks=m['idle_ticks'],idle_owner_ns=None if m['idle_owner_ns']==MAX_U64 else m['idle_owner_ns'],
        participants=[{k:(None if k=='cpu_ns' and v==MAX_U64 else v) for k,v in p.items() if k not in {'record','tid'}} for p in roles],
        heap=[{k:v for k,v in h.items() if k!='record'} for h in heaps],per_producer=per_producer,counter_read_pair_ticks=stats([integer(s['ticks'],0,1<<31) for s in reads]),
        normal_loss_gate_passed=m['mode']=='overload' or delivered_count==count,latency_sample_count=len(handled),
        samples_pooled=False,production_qualified=False,physical_timing_qualified=False)
    releases=group('release')
    b={'paced':1,'burst':16,'overload':64}.get(m['mode'],0)
    groups=0 if b==0 else count if b==1 else math.ceil(count/(b*n))
    require(m['source_groups']==groups and len(releases)==groups and {r['index'] for r in releases}==set(range(groups)),'missing/extra source release')
    releases=sorted(releases,key=lambda r:r['index'])
    for r in releases:
        integer(r['start']);integer(r['end']);delta(r['end'],r['start'])
        if m['native']:require(delta(r['end'],m['begin'])<=ticks,'release outside measurement')
    for r in measured:
        if groups:
            index=r['id']-128;g=index if b==1 else index//(b*n)
            require(r['planned']==releases[g]['start'],'arrival not bound to source release')
    output['source_groups']=groups
    output['source_callback_wall_ticks']=stats([delta(r['end'],r['start']) for r in releases])
    output['source_period_drift_ticks']=stats([delta(r['start'],releases[0]['start'])-i*(m['clock_hz']//100) for i,r in enumerate(releases)]) if releases else None
    return output
