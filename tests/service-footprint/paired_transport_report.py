"""Validate alternating C/Rust scale workloads sharing one firmware image."""
import argparse
import json
from pathlib import Path
import re
import statistics

from evidence import image_record, load_measurement, marker_rows, summary, validate_scale
from transport_pipeline_report import validate_artifacts


def validate_pair_policy(build, ack_bytes):
    features = set(build['features'])
    required = {'mq-backend', 'paired-scale-control', 'synchronized-scale',
                'native-scale-worker', 'shared-mq-code', 'borrowed-mq-io', 'ffi-scale-entry'}
    if not required <= features or build['app_opt_level'] != '2' or not build['std_source']['selected_source_matches']:
        raise ValueError('paired Rust provider policy mismatch')
    # Parse by key, not positional assumptions about future make arguments.
    definitions = next(x.split('=', 1)[1] for x in build['make_command'] if x.startswith('NXRS_TARGET_C_FLAGS='))
    flags = set(definitions.split())
    if not {'-DNXRS_CQ_DEVICE', '-DNXRS_CQ_SYNCHRONIZED', '-DNXRS_CQ_EMBED_CONTROL', '-DNXRS_CQ_SPEED'} <= flags:
        raise ValueError('paired C provider policy mismatch')
    packet = ack_bytes == 28
    if ('packet-service' in features) != packet or ('wire-only' in features) == packet:
        raise ValueError('paired Rust payload policy mismatch')
    for flag, expected in (('-DNXRS_CQ_PACKET_SERVICE', packet), ('-DNXRS_PAYLOAD_C_SPEED', packet),
                           ('-DNXRS_CQ_WIRE_ONLY', not packet)):
        if (flag in flags) != expected: raise ValueError('paired C payload policy mismatch')


def analyze_pairs(text, runs, ack_bytes):
    if '_FAIL' in text or 'MEASUREMENT_FAILED' in text: raise ValueError('paired failure marker')
    cases = {lang: validate_scale(text, lang, 'wire', runs*4, require_wake=False) for lang in ('c', 'rust')}
    completions = marker_rows(text, 'CQ_PAIR_PASS', runs)
    if any(row != dict(batches=4, messages=5760, threads=20, queues=60) for row in completions):
        raise ValueError('paired completion topology mismatch')
    pairs, batch, first, pending, values, completed = [], None, None, [], {}, 0
    for line in text.replace('\r', '').splitlines():
        if line.startswith('CQ_PAIR_BEGIN '):
            if pending: raise ValueError('missing paired provider')
            fields = dict(re.findall(r'(\w+)=(\d+)', line))
            batch, first = int(fields['batch']), int(fields['first'])
            if batch != len(pairs) - completed * 4 or not 0 <= batch < 4 or first != batch % 2:
                raise ValueError('batch sequence or language order mismatch')
            pending = ['c', 'rust'] if first == 0 else ['rust', 'c']
            values = {}
        elif line.startswith('CQ_TRANSPORT_PASS '):
            fields = dict(re.findall(r'(\w+)=([^\s]+)', line))
            if (not pending or fields.get('language') != pending.pop(0)
                    or fields.get('mode') != 'large' or int(fields.get('event_bytes', 0)) != 248
                    or int(fields.get('ack_bytes', 0)) != ack_bytes):
                raise ValueError('paired transport order or layout mismatch')
            cycles = int(fields.get('cycles', 0))
            if not 0 < cycles < 3840000000: raise ValueError('invalid paired cycle clock')
            values[fields['language']] = cycles
            if not pending: pairs.append(values)
        elif line.startswith('CQ_PAIR_PASS '):
            if pending or len(pairs) != (completed + 1) * 4:
                raise ValueError('early or misplaced paired completion')
            completed += 1
    if pending or len(pairs) != runs*4 or completed != runs: raise ValueError('incomplete pair set')
    command_samples = [pairs[i:i+4] for i in range(0, len(pairs), 4)]
    return dict(runs=runs, pairs_per_command=4, pairs=pairs, protocol=cases,
                c_us=summary([statistics.median(p['c']/240 for p in command) for command in command_samples]),
                rust_us=summary([statistics.median(p['rust']/240 for p in command) for command in command_samples]),
                paired_rust_over_c=summary([statistics.median(p['rust']/p['c'] for p in command) for command in command_samples]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=10)
    parser.add_argument('--ack-bytes', type=int, choices=(16,28), required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1: parser.error('runs must be positive')
    if args.out.exists(): parser.error('fresh output required')
    directory = args.directory.resolve()
    row = image_record('xtensa-esp32s3-elf-', directory, 'rust')
    row['build'] = json.loads((directory / 'relink-provenance.json').read_text())
    validate_artifacts(directory, row['build'])
    validate_pair_policy(row['build'], args.ack_bytes)
    measurement = directory / f'measure-{args.runs}'
    row['board'] = load_measurement(measurement, directory / 'rust.merged.bin', args.runs)
    if any(row['board']['repeat_retained']): raise ValueError('heap retained across repeated paired commands')
    row['paired'] = analyze_pairs((measurement / 'serial.log').read_text(), args.runs, args.ack_bytes)
    args.out.write_text(json.dumps(row, indent=2) + '\n')

if __name__ == '__main__': main()
