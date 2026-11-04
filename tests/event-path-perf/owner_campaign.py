#!/usr/bin/env python3
"""Run the same executable's two selected-owner strategies, retaining all outcomes.

Host measurements are workload observations, not NuttX latency, WCET, scheduler
switch counts, or production progress qualification. This tool never flashes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('p3_p2_analysis', HERE / 'analyze.py')
p2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p2)
PREFIX = 'OWNER_PATH_PERF '
CASES = tuple(f'owner-{strategy}-{workload}' for workload in (
    'backlog', 'burst-1', 'burst-4', 'periodic-1', 'periodic-4')
    for strategy in ('select', 'ready'))
require, integer, boolean = p2.require, p2.integer, p2.boolean


def analyze(text):
    require(not p2.faulted(text), 'native or fixture failure')
    rows = []
    for line in text.splitlines():
        require(line.startswith(PREFIX), 'unexpected owner output')
        row = json.loads(line[len(PREFIX):], object_pairs_hook=p2.unique_keys,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        require(isinstance(row, dict) and row.get('schema') == 'owner-path-v1', 'owner schema')
        for key, value in row.items():
            if key.endswith('_qualified'):
                require(value is False, 'unsupported qualification')
        rows.append(row)
    require(len(rows) >= 4 and rows[0].get('record') == 'experiment'
            and rows[-1].get('record') == 'result', 'incomplete owner output')
    meta, result = rows[0], rows[-1]
    require(meta.get('case') in CASES, 'unknown owner case')
    require(meta.get('runtime') in ('host-functional-only', 'nuttx-unqualified'), 'owner runtime')
    require(isinstance(meta.get('build_id'), str) and meta['build_id'], 'owner build identity')
    require(all(type(meta.get(k)) is int and meta[k] == v for k, v in
                [('capacity', 8), ('stop_capacity', 1), ('important_burst', 8),
                 ('payload_bytes', 16), ('worker_stack', 16384), ('counter_bits', 32)]),
            'owner policy or layout mismatch')
    count = integer(meta, 'samples', 1, 256)
    hz = integer(meta, 'clock_hz', 1, 10**12)
    native = meta['runtime'] == 'nuttx-unqualified'
    backlog = meta['case'].endswith('backlog')
    providers = 1 if backlog else int(meta['case'].rsplit('-', 1)[1])
    per_provider = count * 16 if backlog else count
    total = per_provider * providers
    actual = rows[1:-1]
    attempts = []
    schedules = []
    for provider in range(providers):
        require(actual and actual[0].get('record') == 'provider', 'provider record missing')
        schedule = actual.pop(0)
        require(integer(schedule, 'provider') == provider, 'provider order')
        integer(schedule, 'tid', 1, (1 << 31) - 1)
        policy = integer(schedule, 'policy', 0, 2)
        priority = integer(schedule, 'priority', 0, 255)
        require(not native or (policy, priority) == (1, 180), 'unequal native provider schedule')
        schedules.append(schedule)
        for sequence in range(1, per_provider + 1):
            require(actual and actual[0].get('record') == 'attempt', 'attempt missing')
            row = actual.pop(0)
            require(integer(row, 'provider') == provider and integer(row, 'sequence') == sequence,
                    'attempt identity gap or reorder')
            require(integer(row, 'checksum', 0, p2.U64) == p2.payload_checksum('bytes16', sequence, provider),
                    'owner payload checksum')
            attempts.append(row)
    require(not actual, 'extra owner records')
    admitted = handled = discarded = 0
    admission_ticks, delivery_ticks, handler_ticks = [], [], []
    for row in attempts:
        accepted = boolean(row, 'accepted')
        cancelled = boolean(row, 'discarded')
        require(not cancelled or accepted, 'rejected value was discarded')
        begin = integer(row, 'start_ticks', 0, p2.U32)
        end = integer(row, 'admission_return_ticks', 0, p2.U32)
        upper = integer(row, 'elapsed_upper_ns', 1, p2.U64)
        require(upper * hz < p2.HALF * 10**9, 'ambiguous owner counter wrap')
        delta = (end - begin) & p2.U32
        deltas = [delta]
        admission_ticks.append(delta)
        admitted += accepted
        discarded += cancelled
        if accepted and not cancelled:
            first = integer(row, 'handler_start_ticks', 0, p2.U32)
            last = integer(row, 'handler_end_ticks', 0, p2.U32)
            first_delta = (first - begin) & p2.U32
            last_delta = (last - begin) & p2.U32
            require(first_delta <= last_delta, 'handler timestamps reversed')
            deltas.extend([first_delta, last_delta])
            delivery_ticks.append(first_delta)
            handler_ticks.append(last_delta - first_delta)
            handled += 1
        else:
            require(row.get('handler_start_ticks') is None and row.get('handler_end_ticks') is None,
                    'unhandled event claims a handler')
        require(all(v < p2.HALF and v * 10**9 <= upper * hz for v in deltas), 'invalid owner interval')
    for key, expected in [('attempted', total), ('admitted', admitted),
                          ('rejected', total-admitted), ('handled', handled), ('discarded', discarded)]:
        require(integer(result, key, 0, total) == expected, 'owner conservation mismatch: ' + key)
    require(admitted == handled + discarded, 'owner lost work')
    require(boolean(result, 'success') and boolean(result, 'threads_joined'), 'owner did not finish')
    for key in ('physical_timing_qualified', 'priority_progress_qualified'):
        require(boolean(result, key) is False and boolean(meta, key) is False, 'owner qualification promoted')
    require(result.get('actual_switch_count') is None and result.get('cpu_time_ns') is None,
            'unmeasured execution cost promoted')
    select_calls = integer(result, 'select_calls', 0, p2.U32)
    ready_hits = integer(result, 'ready_hits', 0, p2.U32)
    integer(result, 'deadline_turns', 0, p2.U32)
    integer(result, 'owner_tid', 1, (1 << 31)-1)
    require(not native or (integer(result, 'owner_policy') == 1 and
                          integer(result, 'owner_priority') == 180), 'unequal native owner schedule')
    elapsed = integer(result, 'elapsed_ns', 1, p2.U64)
    start = integer(result, 'stop_request_ticks', 0, p2.U32)
    stop = integer(result, 'stop_observed_ticks', 0, p2.U32)
    stop_delta = (stop - start) & p2.U32
    require(stop_delta < p2.HALF and stop_delta * 10**9 <= elapsed * hz, 'invalid stop interval')
    ready = '-ready-' in meta['case']
    if backlog:
        require(admitted == handled == total and not discarded, 'backlog did not handle every event')
        require(select_calls == (0 if ready else total) and ready_hits == (total if ready else 0),
                'backlog dispatch counters disagree with strategy')
        integer(result, 'drain_ticks', 1, p2.U64)
    else:
        require(result.get('drain_ticks') is None, 'concurrent case claims isolated drain time')
    return {'schema': 'owner-path-analysis-v1', 'metadata': meta, 'result': result,
            'providers': schedules, 'structural_valid': True, 'physical_timing_qualified': False,
            'priority_progress_qualified': False, 'raw_ticks': {
                'admission_call': p2.raw_distribution(admission_ticks),
                'admission_to_handler_start': p2.raw_distribution(delivery_ticks),
                'handler_work': p2.raw_distribution(handler_ticks),
                'stop_observation': stop_delta},
            'scope': 'Cold-inclusive finite owner workload. Selector calls are not kernel wakeups or switches. '
                     'Dropped work is explicit. No physical or general fixed-priority qualification.'}


def run(binary, output, samples, repeats):
    require(not output.exists(), 'output already exists; do not overwrite evidence')
    require(type(samples) is int and 1 <= samples <= 256 and 1 <= repeats <= 10, 'campaign bounds')
    binary = binary.resolve(strict=True)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    output.mkdir(parents=True)
    results = []
    for repeat in range(repeats):
        # Reverse order in alternate rounds. Concurrent admission counts may
        # differ; only deterministic backlog is a like-for-like dispatch test.
        for case in CASES if repeat % 2 == 0 else tuple(reversed(CASES)):
            folder = output / f'{repeat:02}-{case}'
            folder.mkdir()
            completed = subprocess.run([str(binary), case, str(samples), '0', 'bytes16'],
                                       capture_output=True, timeout=30)
            (folder / 'console.log').write_bytes(completed.stdout)
            (folder / 'stderr.log').write_bytes(completed.stderr)
            require(completed.returncode == 0, f'owner execution failed: {case}')
            require(len(completed.stdout) <= 4*1024*1024 and not completed.stderr, 'unexpected owner output size/stderr')
            summary = analyze(completed.stdout.decode('utf-8'))
            require(summary['metadata']['runtime'] == 'host-functional-only', 'host runner received non-host result')
            summary['executable_sha256'] = digest
            (folder / 'analysis.json').write_text(json.dumps(summary, indent=2) + '\n')
            results.append({'case': case, 'repeat': repeat, 'result': summary['result'], 'raw_ticks': summary['raw_ticks']})
            print(case, 'handled', summary['result']['handled'], 'rejected', summary['result']['rejected'],
                  'select calls', summary['result']['select_calls'], flush=True)
    require(hashlib.sha256(binary.read_bytes()).hexdigest() == digest, 'executable changed during campaign')
    report = {'schema': 'owner-campaign-v1', 'executable_sha256': digest, 'samples': samples,
              'repeats': repeats, 'results': results, 'physical_timing_qualified': False,
              'priority_progress_qualified': False}
    (output / 'campaign.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=128)
    parser.add_argument('--repeats', type=int, default=2)
    args = parser.parse_args()
    run(args.binary, args.output, args.samples, args.repeats)
