#!/usr/bin/env python3
"""Describe latency by iteration order; never infer a queue slot or root cause."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import analyze


def ticks_summary(values):
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    return {
        'count': len(values), 'min_ticks': ordered[0],
        'p50_ticks': ordered[(len(values) + 1) // 2 - 1],
        'p99_ticks': ordered[(99 * len(values) + 99) // 100 - 1],
        'max_ticks': ordered[-1], 'distinct_ticks': len(set(values)),
        'most_common': [{'ticks': value, 'count': count}
                        for value, count in sorted(Counter(values).items(),
                                                   key=lambda item: (-item[1], item[0]))[:8]],
        'percentile_method': 'nearest-rank',
    }


def report(text):
    # Reuse all existing payload, schedule, clock-wrap and source-mode gates.
    checked = analyze.analyze(text)
    analyze.require(checked['mode'] == 'latency', 'alternation requires latency samples')
    metadata, samples, result = analyze.records(text)
    warmup, capacity = metadata['warmup'], metadata['capacity']
    send = [(s['send_return_ticks'] - s['send_start_ticks']) % (1 << 32) for s in samples]
    receive = [(s['receive_return_ticks'] - s['send_start_ticks']) % (1 << 32) for s in samples]

    def groups(offset, modulus):
        return [dict(remainder=r,
                     send_call=ticks_summary([v for i, v in enumerate(send)
                                              if (i + offset) % modulus == r]),
                     send_to_receive_return=ticks_summary([v for i, v in enumerate(receive)
                                                           if (i + offset) % modulus == r]))
                for r in range(modulus)]

    return {
        'schema': 'std-channel-alternation-v1',
        'run_id': metadata['run_id'], 'build_id': metadata['build_id'],
        'sample_count': len(samples), 'warmup': warmup, 'capacity': capacity,
        'sampling': checked.get('sampling'),
        'retained_index_mod_2': groups(0, 2),
        'transfer_index_mod_2': groups(warmup, 2),
        'transfer_index_mod_capacity': groups(warmup, capacity),
        'transfer_index_definition': 'zero-based warmup + retained sample index',
        'internal_channel_slot_observed': False,
        'last_sample': {'index': len(samples) - 1, 'send_ticks': send[-1],
                        'receive_ticks': receive[-1]},
        'terminal_quiescence_verified': checked['terminal_quiescence_verified'],
        'root_cause_established': False, 'physical_timing_qualified': False,
        'overhead_subtracted': False, 'samples_excluded': 0,
        'scope': ('Descriptive counter-tick distributions from one validated transcript. '
                  'Transfer index is not an observed internal queue slot. No causal, '
                  'IRQ-phase, calibrated-time, CPU-cost or representative-load claim.'),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('transcript', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.transcript.read_bytes()
    result = report(raw.decode('utf-8'))
    result['transcript_sha256'] = hashlib.sha256(raw).hexdigest()
    # Do not overwrite original evidence or a prior report.
    with args.output.open('x') as handle:
        json.dump(result, handle, indent=2)
        handle.write('\n')


if __name__ == '__main__':
    main()
