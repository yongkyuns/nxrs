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
    'confirmed movement produces an automatic clip': 'motion-clip',
    'clip includes bounded pre/post roll': 'clip-window',
    'saved H264 clip fully decodes': 'clip-decoding',
    'observer joins readers and encoder on shutdown': 'worker-shutdown',
    'real decoder samples resume after source recovery': 'source-recovery',
    'interrupted movement preserves a completed playable partial clip': 'interrupted-clip',
}
STAGES = frozenset(['decoder exited','reader exited','relay stopped','status reader stopped',
    'segment reader stopped','gray reader stopped','source retired; reconnecting'])
HEALTH = frozenset(['frames_observed','reconnects','clips_created','clips_dropped'])


def worker_stages(raw):
    result=[]
    for line in raw[-8192:].decode('utf-8',errors='replace').splitlines():
        prefix='nxrs-camera-activity: '
        value=line[len(prefix):] if line.startswith(prefix) else ''
        if value in STAGES: result.append(value)
    return result[-8:]


def public_failure(raw):
    result=dict(scope='owned-activity-reference-failure',phase='unknown',reason='unknown',health={},worker_stages=[])
    for line in raw[-16384:].decode('utf-8',errors='replace').splitlines():
        if line in PHASES: result['phase']=PHASES[line]
        if line.startswith('AssertionError: '):
            result['reason']=REASONS.get(line[len('AssertionError: '):],'unknown')
        for prefix,kind in [('ACTIVITY_HEALTH ','health'),('ACTIVITY_WORKER_STAGES ','worker_stages')]:
            if not line.startswith(prefix) or len(line)>2048: continue
            try: value=json.loads(line[len(prefix):])
            except (ValueError,RecursionError): continue
            if kind=='health' and isinstance(value,dict):
                result[kind]={k:v for k,v in value.items() if k in HEALTH and type(v) is int and 0<=v<=2**63-1}
            if kind=='worker_stages' and isinstance(value,list):
                result[kind]=[v for v in value if isinstance(v,str) and v in STAGES][-8:]
    return result
