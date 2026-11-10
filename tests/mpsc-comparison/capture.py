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


def plan(passes, include_native_control=False):
    arms = [('nuttx', 'std'), ('zephyr', 'native')]
    if include_native_control:
        arms.insert(1, ('nuttx', 'native'))
    result = []
    for number in range(1, passes + 1):
        # Rotation gives each of three arms every position over three passes.
        offset = (number - 1) % len(arms)
        order = arms[offset:] + arms[:offset]
        for case, values in cases().items():
            result.extend(dict(pass_number=number, runtime=runtime, backend=backend,
                               case=case, **values) for runtime, backend in order)
    return result


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
                backend=receipt['backend'],
                wait_observer='queue-object' if receipt['backend'] == 'native' else 'semaphore-state-and-phase',
                **cases()[case])


def replay(directory, root):
    receipt = build.receipt(root)
    run = read(directory / 'run.json')
    metrics.require(run.get('schema') == 'mpsc-comparison-capture-v3' and run.get('failure') is None,
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
    run = dict(schema='mpsc-comparison-capture-v3', case=args.case, expected=exp,
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
    paths = {('nuttx', 'std'): args.nuttx_build}
    if getattr(args, 'nuttx_native_build', None) is not None:
        paths['nuttx', 'native'] = args.nuttx_native_build
    if getattr(args, 'zephyr_build', None) is not None:
        paths['zephyr', 'native'] = args.zephyr_build
    metrics.require(len(paths) > 1, 'comparison requires a native NuttX and/or Zephyr build')
    receipts = {arm: build.receipt(path) for arm, path in paths.items()}
    left = receipts['nuttx', 'std']
    for arm, record in receipts.items():
        metrics.require((record['runtime'], record['backend']) == arm, 'mismatched runtime/backend')
        build.validate_cohort(left, record)
    kernel_control = None
    if ('nuttx', 'native') in receipts:
        kernel_control = build.validate_nuttx_control(left, receipts['nuttx', 'native'])
    pairs = {}
    for directory in args.captures:
        run = read(directory / 'run.json')
        arm = run['expected']['runtime'], run['expected']['backend']
        metrics.require(arm in paths, 'capture from an unselected runtime/backend')
        summary = replay(directory, paths[arm])
        key = (summary['pass_number'], summary['case'])
        metrics.require(arm not in pairs.setdefault(key, {}), 'duplicate case/runtime/backend in pass')
        pairs[key][arm] = summary
    metrics.require(bool(pairs) and all(set(p) == set(paths) for p in pairs.values()), 'unpaired captures')
    result = []
    for (pass_number, case), pair in sorted(pairs.items()):
        arms = []
        for (runtime, backend), summary in pair.items():
            arms.append(dict(runtime=runtime, backend=backend, build_id=summary['build_id'],
                             messages_s=summary['delivered_per_nominal_second'],
                             delivery_p99_us=None if summary['delivery_ticks'] is None else
                             summary['delivery_ticks']['p99'] * 1e6 / summary['clock_hz'],
                             offered=summary['offered'], admitted=summary['admitted'],
                             delivered=summary['delivered'], rejected=summary['rejected'],
                             normal_loss_gate=summary['normal_loss_gate_passed'],
                             wait_observer=summary['wait_observer'],
                             cpu_ns_per_delivered=summary['cpu_ns_per_delivered']))
        result.append(dict(pass_number=pass_number, case=case, arms=arms))
    return dict(schema='mpsc-comparison-cohort-v3', board=left['board'],
                samples_pooled=False, physical_timing_qualified=False,
                matched_placement_qualified=False, product_qualified=False,
                footprint_scope='complete diagnostic images, not a channel-object or minimum firmware cost',
                nuttx_kernel_control=kernel_control, comparisons=result)
