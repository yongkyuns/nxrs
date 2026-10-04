#!/usr/bin/env python3
"""Validate fresh three-platform evidence without copying the NuttX/Zephyr report."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from measure import HERE, load, marker_rows, validate
import run_matrix
import build

legacy = load('embassy_existing_report', HERE.parent / 'zephyr-comparison' / 'report.py')
# Its matrix validator is unchanged; teach it the additional case names only.
legacy.matrix_runner.CASES.update(run_matrix.EMBASSY)


def embassy_rows(case, matrices, directory, provenance):
    rows = []
    mode = provenance['mode']
    for matrix in matrices:
        if case not in matrix['cases']:
            continue
        for block in range(matrix['blocks']):
            output = matrix['directory'] / f'{case}-block-{block}'
            record = json.loads((output / 'measurement.json').read_text())
            count = matrix['runs_per_block']
            if (record.get('failure') is not None or record.get('flashed') is not True or
                    record.get('requested_runs') != count or record.get('completed_runs') != count or
                    len(record.get('runs', [])) != count or record.get('mode') != mode or
                    record.get('command') != ('baseline' if mode == 'baseline' else 'large') or
                    record.get('image_sha256') != provenance['artifacts']['embassy.bin']):
                raise ValueError('incomplete, wrong-image or wrong-command Embassy measurement')
            text = (output / 'serial.log').read_text(errors='replace').replace('embassy> ', '').replace('\r', '')
            if 'FAIL' in text:
                raise ValueError('failure in Embassy transcript')
            markers = [('baseline', 'EMBASSY_BASELINE_PASS')] if mode == 'baseline' else [
                ('resources', 'EMBASSY_RESOURCES'), ('transport', 'CQ_TRANSPORT_PASS'),
                ('scale', 'CQ_EMBASSY_SCALE_PASS')]
            parsed = {key: marker_rows(text, marker, count) for key, marker in markers}
            exits = marker_rows(text, 'EMBASSY_COMMAND_EXIT', count)
            for index, saved in enumerate(record['runs']):
                lines = [marker + ' ' + ' '.join(f'{k}={v}' for k, v in parsed[key][index].items())
                         for key, marker in markers]
                lines.append('EMBASSY_COMMAND_EXIT ' + ' '.join(f'{k}={v}' for k, v in exits[index].items()))
                checked = validate(('\n'.join(lines) + '\n').encode(), mode)
                if checked != saved:
                    raise ValueError('Embassy JSON/raw transcript mismatch')
                rows.append({'matrix': matrix['directory'].name, 'matrix_block': block,
                             'block': matrix['global_block_start'] + block, 'run': index, **checked})
    if not rows:
        raise ValueError('no Embassy runs')
    return rows


def embassy_case(case, matrices, artifacts, readelf, nm):
    directory = run_matrix.case_directory(artifacts, case)
    run_matrix.frozen_image(directory, case)
    provenance = json.loads((directory / 'build-provenance.json').read_text())
    if provenance['mode'] != run_matrix.EMBASSY[case][3]:
        raise ValueError('Embassy build mode does not match case')
    if provenance.get('cooperative_yield') != (case == 'embassy-packet-yield-2'):
        raise ValueError('Embassy scheduling control does not match case')
    if provenance['source_sha256'] != build.source_inventory():
        raise ValueError('Embassy firmware sources changed after build')
    elf = directory / 'embassy.elf'
    sections = build.parse_sections(subprocess.check_output([readelf, '-W', '-S', str(elf)], text=True))
    build.assert_stack_section(sections)
    accounting = build.section_accounting(sections)
    if accounting != provenance['section_accounting']:
        raise ValueError('Embassy section accounting changed')
    header = build.image_header(directory / 'embassy.bin')
    if header != provenance['image_header']:
        raise ValueError('Embassy image header changed')
    rows = embassy_rows(case, matrices, directory, provenance)
    symbols = subprocess.check_output([nm, '-S', '--size-sort', '-C', str(elf)], text=True)
    storage = []
    for line in symbols.splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) == 4 and fields[2].lower() in ('b', 'd') and '::workload::' in fields[3]:
            storage.append({'symbol': fields[3], 'bytes': int(fields[1], 16)})
    out = {'os': 'embassy', 'language': 'rust-no_std', 'mode': provenance['mode'],
           'identity': {'toolchain': provenance['tool_versions'], 'profile': provenance['profile'],
                        'cooperative_yield': provenance['cooperative_yield'],
                        'configuration': provenance['configuration'], 'sources_sha256': provenance['source_sha256'],
                        'artifacts_sha256': provenance['artifacts']},
           'completed_runs': len(rows), 'runs': rows, 'sections': sections,
           'image_bytes': (directory / 'embassy.bin').stat().st_size,
           'resource_ledger': {**accounting, 'whole_ram_footprint_bytes': accounting['resident_ram_bytes'],
                               'peak_heap_bytes': 0, 'shared_stack_bytes': 8192,
                               'workload_static_symbols': storage,
                               'task_pool_storage_bytes': sum(s['bytes'] for s in storage if '::POOL' in s['symbol'])}}
    if provenance['mode'] != 'baseline':
        cycles = [row['transport']['cycles'] for row in rows]
        out['timing'] = {'primary_cycles': legacy.summary(cycles),
                         'primary_elapsed_us': legacy.summary([n / 240 for n in cycles]),
                         'latency_rx_p99_us': legacy.summary([r['scale']['rx_p99_us'] for r in rows])}
        resources = rows[0]['resources']
        if any(r['resources'] != resources for r in rows):
            raise ValueError('Embassy resources changed between runs')
        out['resource_ledger']['channel_storage_bytes'] = resources['channel_storage']
        out['resource_ledger']['queue_buffer_bytes'] = resources['queue_buffers']
        out['resource_ledger']['channel_metadata_bytes'] = resources['channel_storage'] - resources['queue_buffers']
        if not out['resource_ledger']['task_pool_storage_bytes']:
            raise ValueError('missing task-pool symbol sizes')
    return out


def build_report(artifacts, directories, readelf, nm, nuttx_compiler=None):
    matrices, cases = legacy._matrix_records(directories)
    start = 0
    for matrix in matrices:
        matrix['global_block_start'] = start
        start += matrix['blocks']
    version = ((nuttx_compiler, subprocess.check_output([nuttx_compiler, '--version'], text=True).splitlines()[0])
               if nuttx_compiler else None)
    reports = {}
    for case in sorted(cases):
        if case in run_matrix.EMBASSY:
            reports[case] = embassy_case(case, matrices, artifacts, readelf, nm)
        else:
            frozen = legacy._frozen_build(artifacts, case, readelf, version)
            reports[case] = legacy._case_report(case, matrices, frozen)
        if reports[case]['mode'] != 'baseline':
            reports[case]['latency_summary_us'] = {
                key: legacy.summary([row['scale'][key] for row in reports[case]['runs']])
                for key in ('rx_p50_us', 'rx_p99_us', 'rx_max_us', 'ack_p99_us')}
    baselines = {'nuttx': 'nuttx-baseline', 'zephyr': 'zephyr-baseline', 'embassy': 'embassy-baseline'}
    for name, case in reports.items():
        baseline_name = ('embassy-baseline-os' if name == 'embassy-packet-os' else baselines[case['os']])
        baseline = reports.get(baseline_name)
        if baseline and case['mode'] != 'baseline':
            case['increment_over_baseline_bytes'] = {
                key: case['resource_ledger'][key] - baseline['resource_ledger'][key]
                for key in ('loadbearing_flash_bytes', 'whole_ram_footprint_bytes')}
            if case['os'] != 'embassy':
                case['increment_over_baseline_bytes']['ram_excluding_spawned_stacks'] = (
                    case['increment_over_baseline_bytes']['whole_ram_footprint_bytes'] - 83968)
            else:
                case['increment_over_baseline_bytes']['ram_excluding_spawned_stacks'] = (
                    case['increment_over_baseline_bytes']['whole_ram_footprint_bytes'])
    return {'schema': 1, 'measurement_date': '2026-10-04', 'board': 'Freenove ESP32-S3-WROOM',
            'scope': 'isolated experiment; Embassy async tasks are not preemptive OS threads',
            'host_transport': 'NuttX USB/NSH commands byte-paced at 5 ms; excluded from primary app CCOUNT timing',
            'experiment_tools_sha256': {p.name: build.sha256(p) for p in sorted(HERE.glob('*.py'))},
            'workload': {'queues': 60, 'roles': 20, 'producers': 4, 'workers': 15, 'collectors': 1,
                         'event_bytes': 248, 'event_reply_pairs': 5760, 'digest': 441445568,
                         'latency_samples_per_invocation': 720},
            'matrices': [{'name': m['directory'].name, 'runs_per_block': m['runs_per_block'],
                          'blocks': m['blocks'], 'order': m['order'], 'restore_verified': True}
                         for m in matrices], 'cases': reports,
            'accounting_note': 'RAM includes resident IRAM/DRAM and reserved stack/storage, not alias gaps. '
                'Embassy has no app heap; async futures/task headers are static and are included. '
                'Flash is load-bearing ELF bytes; merged-image length also contains bootloader/alignment. '
                'Baseline subtraction does not make the platforms feature-equivalent. Energy is not measured.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', required=True, type=Path)
    parser.add_argument('--matrix', required=True, type=Path, action='append')
    parser.add_argument('--readelf', required=True)
    parser.add_argument('--nm', required=True)
    parser.add_argument('--nuttx-compiler')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    report = build_report(args.artifacts, args.matrix, args.readelf, args.nm, args.nuttx_compiler)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print('EMBASSY_COMPARISON_REPORT_PASS')


if __name__ == '__main__':
    main()
