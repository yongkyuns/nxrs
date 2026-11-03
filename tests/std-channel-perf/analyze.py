#!/usr/bin/env python3
"""Validate std-channel evidence; never turn a reproduced stall into qualification."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

PREFIX = 'STD_CHANNEL_PERF '
SCHEMA = 'std-channel-perf-v1'
MODES = {'latency': 0, 'witness-reserved': 1, 'witness-published': 2}
FAILURE = re.compile(r'STD_CHANNEL_PERF FAIL|panicked at|Assertion failed|HardFault|arm_hardfault:|qemu: fatal:|Lockup:')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(row, key, lower=0, upper=None):
    value = row.get(key)
    require(type(value) is int and value >= lower and (upper is None or value <= upper),
            f'invalid {key}')
    return value


def boolean(row, key):
    value = row.get(key)
    require(type(value) is bool, f'invalid {key}')
    return value


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_keys)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'duplicate JSON field: {key}')
        result[key] = value
    return result


def records(text):
    require(not FAILURE.search(text), 'failure or kernel fault in transcript')
    rows = []
    for line in text.replace('\r', '').splitlines():
        if line.startswith(PREFIX):
            row = json.loads(line[len(PREFIX):], object_pairs_hook=unique_keys)
            require(isinstance(row, dict) and row.get('schema') == SCHEMA, 'unknown record schema')
            require(row.get('record') not in {'failure', 'fatal'}, f"target failed: {row.get('reason', 'unspecified')}")
            rows.append(row)
        elif PREFIX in line:
            raise ValueError('truncated or interleaved result record')
    require(len(rows) >= 2, 'incomplete transcript')
    require(rows[0].get('record') == 'metadata' and rows[-1].get('record') == 'result',
            'expected one metadata record followed by samples and a final result')
    require(all(r.get('record') == 'sample' for r in rows[1:-1]), 'mixed runs or duplicate summary')
    run_id = rows[0].get('run_id')
    require(isinstance(run_id, str) and bool(run_id), 'missing run identity')
    require(all(r.get('run_id') == run_id for r in rows), 'mixed run identities')
    return rows[0], rows[1:-1], rows[-1]


def distribution(values, frequency):
    """Nearest-rank percentiles of individual samples, with no overhead subtraction."""
    ordered = sorted(values)
    require(bool(ordered), 'no samples')
    def ns(ticks):
        return (ticks * 1_000_000_000 + frequency - 1) // frequency
    def percentile(percent):
        return ordered[math.ceil(len(ordered) * percent / 100) - 1]
    return {
        'count': len(ordered), 'min_ticks': ordered[0],
        'p50_ticks': percentile(50), 'p95_ticks': percentile(95),
        'p99_ticks': percentile(99), 'max_ticks': ordered[-1],
        'min_ns': ns(ordered[0]), 'p50_ns': ns(percentile(50)),
        'p95_ns': ns(percentile(95)), 'p99_ns': ns(percentile(99)),
        'max_ns': ns(ordered[-1]), 'percentile_method': 'nearest-rank',
    }


def analyze(text):
    metadata, samples, result = records(text)
    mode = metadata.get('mode')
    require(mode in MODES, 'unknown mode')
    require(integer(metadata, 'hook_stage', 0, 2) == MODES[mode], 'instrumentation/mode mismatch')
    require(metadata.get('runtime') in {'nuttx', 'linux-functional'}, 'unknown target runtime')
    require(integer(metadata, 'capacity', 1) == 8, 'unexpected capacity')
    require(integer(metadata, 'payload_bytes', 1) == 16, 'unexpected payload size')
    require(metadata.get('witness_api') == ('recv' if mode == 'latency' else 'try_recv'),
            'wrong std API under test')
    require(metadata.get('blocked_observation_scope') == 'WAIT_SEM-inside-recv', 'unknown blocked-wait proof')
    request = metadata.get('clock_source_request')
    require(request in {'baseline', 'native-counter'}, 'unknown clock request')
    sources = {'native-counter': {'xtensa-ccount', 'arm-dwt-cyccnt'},
               'baseline': {'nuttx-arch-perf', 'nuttx-arch-timer', 'nuttx-system-ticks', 'nuttx-monotonic', 'linux-monotonic'}}
    require(metadata.get('clock_source') in sources[request], 'counter source differs from request')
    native = metadata['runtime'] == 'nuttx'
    require(boolean(metadata, 'single_core_tcb_witness') == native, 'wrong scheduler witness capability')
    require(boolean(metadata, 'physical_measurement') is False, 'firmware cannot self-certify physical execution')
    build_id = metadata.get('build_id')
    require(isinstance(build_id, str) and (re.fullmatch('[0-9a-f]{64}', build_id)
            or (not native and build_id == 'unrecorded-host')), 'missing firmware build identity')
    for role in ['sender', 'receiver', 'observer', 'watchdog']:
        integer(metadata, role + '_policy', 0, 2)
        integer(metadata, role + '_priority', 0, 255)
    if native:
        expected = {'sender': 120, 'receiver': 180, 'observer': 120 if mode == 'latency' else 220,
                    'watchdog': 240}
        for role, priority in expected.items():
            require(metadata[role + '_policy'] == 1 and metadata[role + '_priority'] == priority,
                    f'unverified FIFO schedule for {role}')
    require(boolean(result, 'success'), 'fixture failed')
    require(boolean(result, 'threads_joined'), 'workers did not join')
    require(not boolean(result, 'deadline_hit'), 'fixture watchdog expired')
    summary = {
        'schema': 'std-channel-analysis-v1', 'run_id': metadata['run_id'], 'mode': mode,
        'fixture_valid': True, 'runtime_kind': 'unassigned', 'metadata': metadata, 'result': result,
        'physical_timing_qualified': False, 'hard_realtime_bound': False,
        'priority_progress_qualified': False,
    }
    if mode == 'latency':
        frequency = integer(metadata, 'clock_hz', 1)
        bits = integer(metadata, 'counter_bits', 1, 64)
        require(bits == 32, 'unexpected effective counter width')
        modulus = 1 << bits
        count = integer(metadata, 'samples', 1, 4096)
        warmup = integer(metadata, 'warmup', 0, 1024)
        integer(metadata, 'native_counter_bits', bits, 64)
        resolution = integer(metadata, 'monotonic_resolution_ns', 1)
        probe_ns = integer(metadata, 'counter_probe_ns', 20_000_000)
        probe_ticks = integer(metadata, 'counter_probe_ticks', 0, modulus - 1)
        require(metadata.get('counter_probe_kind') == 'active-monotonic-plausibility', 'unknown counter sanity probe')
        counter_validated = boolean(metadata, 'counter_validated')
        expected = probe_ns * frequency // 1_000_000_000
        tolerance = expected // 2 + resolution * frequency // 1_000_000_000
        plausible = metadata['clock_source'] not in {'nuttx-system-ticks', 'nuttx-monotonic'} and probe_ticks > 0 and abs(probe_ticks - expected) <= tolerance
        require(counter_validated == plausible, 'counter sanity claim disagrees with raw probe')
        require(len(samples) == count == integer(result, 'count', 1), 'missing or extra samples')
        send, receive = [], []
        verified = 0
        for index, sample in enumerate(samples):
            require(integer(sample, 'index') == index, 'sample sequence gap or duplicate')
            require(boolean(sample, 'sequence_valid'), 'payload/order mismatch')
            verified += int(boolean(sample, 'blocked_verified'))
            start = integer(sample, 'send_start_ticks', 0, modulus - 1)
            end = integer(sample, 'send_return_ticks', 0, modulus - 1)
            received = integer(sample, 'receive_return_ticks', 0, modulus - 1)
            send_delta, receive_delta = (end - start) % modulus, (received - start) % modulus
            require(max(send_delta, receive_delta) < modulus // 2, 'ambiguous counter wrap or reversed timestamp')
            elapsed_upper = integer(sample, 'elapsed_upper_ns', 1)
            require(elapsed_upper * frequency < (modulus // 2) * 1_000_000_000,
                    'wall-time bound permits ambiguous counter wrap')
            require(max(send_delta, receive_delta) * 1_000_000_000 <= elapsed_upper * frequency,
                    'counter delta exceeds independent wall-time upper bound')
            send.append(send_delta)
            receive.append(receive_delta)
        require(verified == integer(result, 'blocked_verified_count', 0, count), 'blocked count disagrees with samples')
        require(metadata['runtime'] != 'nuttx' or verified == count, 'NuttX receiver was not independently observed blocked')
        high_resolution = metadata.get('clock_source') in {
            'nuttx-arch-perf', 'nuttx-arch-timer', 'xtensa-ccount', 'arm-dwt-cyccnt'}
        counter_resolved = boolean(result, 'counter_resolved')
        require(counter_resolved == (counter_validated and min(send) > 0 and min(receive) > 0),
                'counter resolution summary disagrees with samples/probe')
        summary.update(
            outcome='blocked-receiver-measurement' if verified == count else 'host-functional-only',
            receiver_blocked_verified=verified == count,
            timing_resolution_sufficient=high_resolution and counter_resolved and min(receive) >= 10 and min(send) >= 1,
            warmup_completed=warmup > 0,
            send_call=distribution(send, frequency),
            send_to_receive_return=distribution(receive, frequency),
            timing_boundary='sender timestamp immediately before send to receiver timestamp immediately after recv returns',
            timing_includes='counter accessor, channel publication, wake, scheduling, and receiver return; send may be preempted by receiver',
            timing_excludes='pre-send blocked-state observation, setup, warmup, serialization and UART output',
            pure_context_switch_latency=False,
        )
    else:
        require(not samples, 'witness must not contain timing samples')
        require(metadata['runtime'] == 'nuttx', 'host execution cannot qualify fixed-priority witness')
        require(integer(result, 'hook_hit_count', 1) == 1, 'witness hook did not fire exactly once')
        require(boolean(result, 'consumer_entered'), 'independent wake did not enter consumer')
        require(boolean(result, 'delivered'), 'message not delivered after witness')
        require(integer(metadata, 'samples') == integer(metadata, 'warmup') == 0, 'witness is not a latency workload')
        for key in ['initial_gate_waits_verified', 'consumer_woken_from_hook', 'observation_deadline_reached']:
            require(boolean(result, key), f'unverified interleaving: {key}')
        hook_returned = boolean(result, 'hook_returned_before_recovery')
        consumer = boolean(result, 'consumer_done_before_recovery')
        producer = boolean(result, 'producer_done_before_recovery')
        recovery = boolean(result, 'recovery_applied')
        observation = integer(result, 'observation_ns', 50_000_000)
        hook = integer(result, 'hook_timestamp_ns')
        entered = integer(result, 'consumer_enter_timestamp_ns')
        observed = integer(result, 'observer_timestamp_ns')
        require(hook <= entered <= observed and observed - entered == observation,
                'inconsistent independent-wake/deadline timestamps')
        if mode == 'witness-reserved':
            require(not consumer and not producer and recovery and not hook_returned,
                    'reserved-slot stall not established')
            require(result.get('consumer_state_at_deadline') == result.get('producer_state_at_deadline') == 'ready',
                    'workers were not runnable at the observation deadline')
            require(integer(result, 'consumer_priority_at_deadline') == metadata['receiver_priority'] and
                    integer(result, 'producer_priority_at_deadline') == metadata['sender_priority'],
                    'priorities changed before the observation')
            require(boolean(result, 'reservation_stall_observed'), 'stall summary disagrees with observations')
            summary['outcome'] = 'reservation-stall-observed'
            summary['progress_contract_satisfied'] = False
        else:
            require(consumer and producer and not recovery and hook_returned, 'published-slot control failed')
            require(not boolean(result, 'reservation_stall_observed'), 'control mislabeled as reservation stall')
            summary['outcome'] = 'published-slot-control-completed'
            summary['progress_contract_satisfied'] = True
        summary['scope'] = 'one forced interleaving in the actual instrumented std; no general channel progress or performance proof'
    return summary


def checked_file(directory, relative):
    require(isinstance(relative, str) and bool(relative), 'missing artifact path')
    root = Path(directory).resolve()
    path = (root / relative).resolve()
    require(path.is_relative_to(root) and path.is_file(), 'artifact escapes build directory or is missing')
    return path


def build_receipt(directory, mode):
    """Validate the files used for capture; metadata alone cannot identify a firmware."""
    receipt = read_json(Path(directory) / 'build.json')
    require(receipt.get('schema') == 'std-channel-build-v1', 'unknown build receipt')
    require(receipt.get('status') == 'complete', 'incomplete build receipt')
    require(receipt.get('mode') == mode and receipt.get('hook_stage') == MODES[mode], 'wrong firmware mode')
    for file_key, hash_key in [('image_file', 'image_sha256'), ('elf_file', 'elf_sha256'),
                              ('config_file', 'config_sha256')]:
        path = checked_file(directory, receipt.get(file_key))
        require(sha256(path) == receipt.get(hash_key), f'{file_key} digest mismatch')
    for key in ['std_array_sha256', 'std_array_original_sha256']:
        require(isinstance(receipt.get(key), str) and re.fullmatch('[0-9a-f]{64}', receipt[key]), f'missing {key}')
    same_source = receipt['std_array_sha256'] == receipt['std_array_original_sha256']
    require(same_source == (mode == 'latency'), 'private std instrumentation disagrees with image purpose')
    require(receipt.get('compiler_version') and receipt.get('kernel_revision'), 'missing compiler/kernel identity')
    require(isinstance(receipt.get('build_id'), str) and re.fullmatch('[0-9a-f]{64}', receipt['build_id']),
            'missing build identity')
    return receipt


def qualify(summary, receipt, session, *, fresh_boot_observed=False):
    """Physical identity/clock calibration are operator evidence, never inferred from QEMU."""
    result = dict(summary)
    require(receipt['mode'] == summary['mode'], 'build/run mode mismatch')
    require(receipt.get('build_id') == summary['metadata']['build_id'], 'running firmware build identity mismatch')
    require(receipt.get('clock_request') == summary['metadata']['clock_source_request'],
            'running firmware clock request mismatch')
    runtime = session.get('runtime_kind')
    require(runtime in {'qemu', 'physical', 'host'}, 'unknown capture runtime')
    require(session.get('image_sha256') == receipt['image_sha256'], 'session image differs from built image')
    require(session.get('schema') == 'std-channel-session-v1', 'unknown session receipt')
    require(runtime != 'host' or summary['metadata']['runtime'] == 'linux-functional', 'host/session mismatch')
    require(runtime == 'host' or summary['metadata']['runtime'] == 'nuttx', 'firmware/session mismatch')
    result.update(runtime_kind=runtime, build=receipt, session=session,
                  fresh_boot_observed=bool(fresh_boot_observed), physical_timing_qualified=False)
    if runtime == 'physical':
        for key in ['measurement_session', 'board', 'board_serial', 'clock_verification',
                    'cache_state', 'memory_placement', 'power_management', 'external_load',
                    'flash_verification']:
            value = session.get(key)
            require(isinstance(value, str) and bool(value.strip()) and
                    value.strip().lower() not in {'unknown', 'todo', 'unmeasured'} and
                    not value.strip().lower().startswith(('replace', 'todo', '<')),
                    f'missing physical {key}')
        require(integer(session, 'active_cores', 1) == 1, 'witness requires one active CPU')
        integer(session, 'cpu_clock_hz', 1)
        if summary['metadata']['clock_source'] in {'xtensa-ccount', 'arm-dwt-cyccnt'}:
            require(session['cpu_clock_hz'] == summary['metadata']['clock_hz'],
                    'session CPU frequency differs from hardware counter frequency')
        require(fresh_boot_observed, 'physical capture must observe a fresh boot banner')
        result['hardware_identity_origin'] = 'operator-supplied session and flashing/calibration evidence'
        if summary['mode'] == 'latency':
            result['physical_timing_qualified'] = bool(summary['receiver_blocked_verified'] and
                summary['timing_resolution_sufficient'] and summary['warmup_completed'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('transcript', type=Path)
    parser.add_argument('--expect-outcome', choices=['blocked-receiver-measurement', 'host-functional-only',
                        'reservation-stall-observed', 'published-slot-control-completed'])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = analyze(args.transcript.read_text())
    if args.expect_outcome:
        require(result['outcome'] == args.expect_outcome, 'unexpected fixture outcome')
    encoded = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
