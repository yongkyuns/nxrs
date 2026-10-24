"""Public fixture observations: fixed names and bounded counters, never raw logs."""
import json

PHASES = {
    'PASS: authenticated camera-scoped history over verified HTTPS': 'authenticated-history',
    'PASS: real RTSP authorization rejects wrong and cross-role recorder credentials': 'recorder-roles',
    'PASS: actual recorder source/finalizer argv and environments contain no credentials': 'credential-boundary',
    'PASS: motion pre/post roll, decodable H264, thumbnail and retention permissions': 'clip-playback',
    'PASS: orderly shutdown, observer restart and kept-clip persistence': 'worker-restart',
    'PASS: gateway restart preserves authenticated kept footage': 'controller-restart',
    'PASS: source loss/recovery, joined readers and playable interrupted motion': 'source-recovery',
    'PASS: explicit owner deletion and post-delete media isolation': 'owner-deletion',
}
REASONS = {
    'observer remains running': 'observer-exit',
    'fixture source remains live until explicit source-loss injection': 'fixture-publisher-exit',
    'restarted observer remains running': 'restarted-observer-exit',
    'restarted observer opens a new timing interval and decodes before source-loss injection': 'restarted-observer-progress',
    'confirmed movement produces an automatic clip': 'motion-clip',
    'clip includes bounded pre/post roll': 'clip-window',
    'saved H264 clip fully decodes': 'clip-decoding',
    'observer joins readers and encoder on shutdown': 'worker-shutdown',
    'real decoder samples resume after source recovery': 'source-recovery',
    'source loss is observed and recovery begins': 'source-retirement',
    'observer survives reader completion and source restart': 'observer-recovery-exit',
    'monotonic source recovery duration reaches authenticated health': 'recovery-timing',
    'interrupted recording remains authenticated': 'interrupted-auth',
    'interrupted recording fully decodes': 'interrupted-decoding',
    'interrupted movement preserves a completed playable partial clip': 'interrupted-clip',
}
STAGES = frozenset(['segment input ended','segment line refused','segment encoding refused',
    'segment fields refused','segment path refused','segment time refused',
    'segment interval nonincreasing','segment interval too short','segment interval too long',
    'decoder exited','reader exited','relay stopped','status reader stopped',
    'segment reader stopped','gray reader stopped','source retired; reconnecting',
    'invalid activity configuration','activity storage unavailable','activity storage busy',
    'activity storage full','activity not found','activity index needs attention',
    'activity process cleanup incomplete','encrypted activity storage unavailable'])
HEALTH = frozenset(['frames_observed','reconnects','clips_created','clips_dropped'])
PROGRESS = frozenset(['observer_run','recoveries','last_recovery_ms','max_recovery_ms','max_frame_gap_ms','frame_gaps_over_30s'])


def worker_stages(raw):
    result=[]
    for line in raw[-8192:].decode('utf-8',errors='replace').splitlines():
        prefix='nxrs-camera-activity: '
        value=line[len(prefix):] if line.startswith(prefix) else ''
        if value in STAGES: result.append(value)
    return result[-8:]


def recording_observations(value):
    if not isinstance(value,dict): value={}
    health=value.get('health');stages=value.get('worker_stages')
    result = dict(health={k:v for k,v in health.items() if k in HEALTH and type(v) is int and 0<=v<=2**63-1}
                if isinstance(health,dict) else {},
                worker_stages=[v for v in stages if isinstance(v,str) and v in STAGES][-8:]
                if isinstance(stages,list) else [])
    initial = value.get('initial_health')
    if isinstance(initial, dict):
        result['initial_health'] = {k:v for k,v in initial.items()
            if k in HEALTH and type(v) is int and 0 <= v <= 2**63-1}
    return result


def snapshot(health_path, log_paths):
    result=dict(health={},worker_stages=[])
    try:
        with health_path.open('rb') as source: raw=source.read(2049)
        if len(raw)<=2048: result['health']=json.loads(raw)
    except (OSError,ValueError,RecursionError): pass
    for path in log_paths:
        try:
            with path.open('rb') as source:
                source.seek(0,2);source.seek(max(0,source.tell()-8192))
                result['worker_stages'].extend(worker_stages(source.read(8192)))
        except OSError: pass
    return recording_observations(result)


def public_failure(raw):
    result=dict(scope='owned-activity-reference-failure',phase='unknown',reason='unknown',health={},worker_stages=[])
    for line in raw[-16384:].decode('utf-8',errors='replace').splitlines():
        if line in PHASES: result['phase']=PHASES[line]
        if line.startswith('AssertionError: '):
            result['reason']=REASONS.get(line[len('AssertionError: '):],'unknown')
        for prefix,kind in [('ACTIVITY_HEALTH ','health'),('ACTIVITY_PROGRESS ','progress'),('ACTIVITY_WORKER_STAGES ','worker_stages')]:
            if not line.startswith(prefix) or len(line)>2048: continue
            try: value=json.loads(line[len(prefix):])
            except (ValueError,RecursionError): continue
            if kind=='health' and isinstance(value,dict):
                result[kind]={k:v for k,v in value.items() if k in HEALTH and type(v) is int and 0<=v<=2**63-1}
            if kind=='progress' and isinstance(value,dict):
                result[kind]={k:v for k,v in value.items() if k in PROGRESS and type(v) is int and 0<=v<=2**63-1}
            if kind=='worker_stages' and isinstance(value,list):
                result[kind]=[v for v in value if isinstance(v,str) and v in STAGES][-8:]
    return result
