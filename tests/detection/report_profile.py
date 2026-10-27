#!/usr/bin/env python3
"""Summarize validated detector stage timings without private device metadata."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

STAGES = ('decode_us', 'resize_us', 'kernel_us', 'postprocess_us', 'total_us')


def stats(values):
    ordered = sorted(values)
    return {'samples': len(ordered), 'min_us': ordered[0],
            'mean_us': statistics.fmean(ordered), 'p50_us': statistics.median(ordered),
            'p95_us': ordered[math.ceil(len(ordered) * .95) - 1], 'max_us': ordered[-1]}


def summarize(record, reference=None):
    native = 'rows' in record
    if (native and record.get('passed') is not True) or (not native and record.get('result') != 'PASS'):
        raise ValueError('qualification did not pass')
    rows = record['rows'] if native else record.get('detection_stage_samples', [])
    if not rows:
        raise ValueError('no stage measurements')
    profiles = []
    for index, row in enumerate(rows):
        timing = row.get('stages_us', {}) if native else row
        if set(timing) != set(STAGES) or any(type(timing[k]) is not int or timing[k] < 0 for k in STAGES):
            raise ValueError('invalid stage layout or duration')
        if not timing['kernel_us'] or not 0 < sum(timing[k] for k in STAGES[:-1]) <= timing['total_us']:
            raise ValueError('inconsistent stage clock totals')
        if native and (row['index'] != index or timing['total_us'] > row['inference_us']):
            raise ValueError('invalid native sequence or outer clock')
        profiles.append(timing)
    output = {'samples': len(profiles), 'clock': 'monotonic wall time; includes preemption',
              'timing_scope': 'JPEG validation/decode; Q8 resize/quantization; compute lease/tensor copies/SDK; confidence/DFL/NMS',
              'stages': {key: stats([row[key] for row in profiles]) for key in STAGES}}
    if native:
        output['full_hal_call'] = stats([row['inference_us'] for row in rows])
        output['dataset_sha256'] = record['dataset_sha256']
    if reference is not None:
        if not native:
            raise ValueError('reference scores require a native corpus')
        expected = [list(map(int, line.split())) for line in reference.splitlines()]
        if len(expected) != len(rows) or any(len(row) != 5 or row[0] != i for i, row in enumerate(expected)):
            raise ValueError('reference score sequence differs')
        differences = sum(row['score'] != expected[i][2] for i, row in enumerate(rows))
        output['reference_score_differences'] = differences
        if differences:
            raise ValueError('optimization changed native scores')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--reference-scores', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    source = args.report.read_bytes()
    reference = args.reference_scores.read_text() if args.reference_scores else None
    output = summarize(json.loads(source), reference)
    output['source_report_sha256'] = hashlib.sha256(source).hexdigest()
    if reference is not None:
        output['reference_scores_sha256'] = hashlib.sha256(reference.encode()).hexdigest()
    with args.out.open('x') as file:
        file.write(json.dumps(output, indent=2) + '\n')


if __name__ == '__main__':
    main()
