"""Local, capture-only sessions; flashing and backup restoration stay explicit."""
import hashlib
import json
import os
from pathlib import Path
import time

import build
import metrics

# Eight workload shapes, each with one and four producers. Keep passes separate.
SHAPES = [('sustained', 16, 8), ('stream', 16, 8), ('paced', 16, 8),
          ('burst', 16, 64), ('overload', 16, 8), ('backpressure', 16, 8),
          ('stream', 64, 8), ('stream', 16, 64)]


def cases():
    return {f'{mode}-p{n}-b{payload}-q{capacity}': dict(
        mode=mode, producers=n, payload_bytes=payload, capacity=capacity,
        samples=131072 if mode == 'sustained' else 256 if mode == 'paced' else 2048)
        for mode, payload, capacity in SHAPES for n in (1, 4)}


def read(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=metrics.unique)


def session(value, receipt, root):
    metrics.require(value.get('schema') == 'mpsc-comparison-session-v1'
                    and all(value.get(k) is True for k in
                            ('fresh_backup_verified', 'flash_verified', 'fresh_boot'))
                    and value.get('image_sha256') == build.digest(root / receipt['image_file']),
                    'session requires verified backup/image and a fresh-boot attestation')
    metrics.integer(value.get('pass'), 1, 100)


def expected(receipt, case):
    return dict(build_id=receipt['build_id'], runtime=receipt['runtime'], native=True,
                accounting=False, clock_hz=receipt['board_contract']['clock-hz'],
                backend='std' if receipt['runtime'] == 'nuttx' else 'native', **cases()[case])


def replay(directory, root):
    receipt = build.receipt(root)
    run = read(directory / 'run.json')
    metrics.require(run.get('schema') == 'mpsc-comparison-capture-v2' and run.get('failure') is None,
                    'failed/incomplete capture')
    raw = (directory / 'console.log').read_bytes()
    attestation = (directory / 'session.json').read_bytes()
    metrics.require(hashlib.sha256(raw).hexdigest() == run['console_sha256'] and
                    hashlib.sha256(attestation).hexdigest() == run['session_sha256'], 'evidence changed')
    value = json.loads(attestation, object_pairs_hook=metrics.unique)
    session(value, receipt, root)
    metrics.require(run['expected'] == expected(receipt, run['case']) and b'MPSC_READY' in raw,
                    'case/build/readiness mismatch')
    summary = metrics.analyze(raw, run['expected'])
    return dict(board=receipt['board'], case=run['case'], pass_number=value['pass'], **summary)


def capture(args):
    import serial  # Only physical capture needs pyserial.
    receipt = build.receipt(args.build_directory)
    attestation = args.session.read_bytes()
    session(json.loads(attestation, object_pairs_hook=metrics.unique), receipt, args.build_directory)
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    (args.output / 'session.json').write_bytes(attestation)
    exp = expected(receipt, args.case)
    run = dict(schema='mpsc-comparison-capture-v2', case=args.case, expected=exp,
               failure='incomplete', fresh_boot_attested=True,
               session_sha256=hashlib.sha256(attestation).hexdigest())
    raw = bytearray()
    try:
        print('Reset into the verified image, then open capture within its 120-second command window.', flush=True)
        with serial.Serial(args.port, 115200, timeout=0.1, write_timeout=2) as console:
            deadline = time.monotonic() + 180
            sent = False
            completed = None
            while time.monotonic() < deadline:
                raw.extend(console.read(4096))
                metrics.require(len(raw) < 4 * 1024 * 1024, 'oversized capture')
                metrics.require(not any(word in raw.lower() for word in
                                        (b'mpsc_failure', b'panic', b'assertion failed', b'cpu halted')), 'firmware fault')
                if not sent and b'MPSC_READY' in raw:
                    words = [exp[k] for k in ('mode', 'backend', 'producers', 'payload_bytes', 'capacity', 'samples')]
                    command = (' '.join(map(str, words)) + '\n').encode()
                    metrics.require(console.write(command) == len(command), 'short console command')
                    sent = True
                if b'MPSC_DONE' in raw:
                    completed = completed or time.monotonic()
                    if time.monotonic() - completed >= 1:
                        break
            metrics.require(sent and completed is not None, 'capture timeout')
        metrics.analyze(raw, exp)
        run['failure'] = None
    except BaseException as error:
        run['failure'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        (args.output / 'console.log').write_bytes(raw)
        run['console_sha256'] = hashlib.sha256(raw).hexdigest()
        (args.output / 'run.json').write_text(json.dumps(run, indent=2) + '\n')
        for path in args.output.iterdir():
            path.chmod(0o600)
    summary = replay(args.output, args.build_directory)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (args.output / 'summary.json').chmod(0o600)
    return summary


def compare(args):
    left, right = (build.receipt(path) for path in (args.nuttx_build, args.zephyr_build))
    metrics.require(left['runtime'] == 'nuttx' and right['runtime'] == 'zephyr'
                    and left['board'] == right['board']
                    and left['board_contract'] == right['board_contract']
                    and left['source_sha256'] == right['source_sha256']
                    and left['rust_compiler'] == right['rust_compiler'], 'unmatched build cohort')
    pairs = {}
    for directory in args.captures:
        run = read(directory / 'run.json')
        runtime = run['expected']['runtime']
        summary = replay(directory, args.nuttx_build if runtime == 'nuttx' else args.zephyr_build)
        key = (summary['pass_number'], summary['case'])
        metrics.require(runtime not in pairs.setdefault(key, {}), 'duplicate case/runtime in pass')
        pairs[key][runtime] = summary
    metrics.require(bool(pairs) and all(set(p) == {'nuttx', 'zephyr'} for p in pairs.values()), 'unpaired captures')
    result = []
    for (pass_number, case), pair in sorted(pairs.items()):
        n, z = pair['nuttx'], pair['zephyr']
        result.append(dict(pass_number=pass_number, case=case,
                           nuttx_messages_s=n['delivered_per_nominal_second'],
                           zephyr_messages_s=z['delivered_per_nominal_second'],
                           nuttx_delivery_p99_us=None if n['delivery_ticks'] is None else n['delivery_ticks']['p99'] * 1e6 / n['clock_hz'],
                           zephyr_delivery_p99_us=None if z['delivery_ticks'] is None else z['delivery_ticks']['p99'] * 1e6 / z['clock_hz'],
                           nuttx_normal_loss_gate=n['normal_loss_gate_passed'], zephyr_normal_loss_gate=z['normal_loss_gate_passed']))
    return dict(board=left['board'], samples_pooled=False, physical_timing_qualified=False, comparisons=result)
