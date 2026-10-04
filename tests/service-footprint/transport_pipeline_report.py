"""Validate matched cycle-clock transport and packet-service board records."""
import argparse
import hashlib
import json
from pathlib import Path

from evidence import image_record, load_measurement, marker_rows, summary, validate_scale


def validate_transport(transcript, language, runs, ack_bytes):
    if '_FAIL' in transcript or 'MEASUREMENT_FAILED' in transcript:
        raise ValueError('failure marker in transport transcript')
    protocol = validate_scale(transcript, language, 'wire', runs)
    rows = marker_rows(transcript, 'CQ_TRANSPORT_PASS', runs)
    for row in rows:
        expected = dict(language=language, mode='large', event_bytes=248, ack_bytes=ack_bytes)
        if any(row.get(key) != value for key, value in expected.items()):
            raise ValueError('transport mode/layout mismatch')
        if not isinstance(row.get('cycles'), int) or not 0 < row['cycles'] < 3_840_000_000:
            raise ValueError('invalid or wrapped cycle clock')
    protocol['traffic_us'] = summary([row['cycles'] / 240 for row in rows])
    protocol['traffic_cycles'] = [row['cycles'] for row in rows]
    return protocol


def validate_threads(transcript, runs, language='rust'):
    if '_FAIL' in transcript or 'MEASUREMENT_FAILED' in transcript:
        raise ValueError('failure marker in thread baseline')
    rows = marker_rows(transcript, 'CQ_C_THREADS_PASS' if language == 'c' else 'CQ_THREADS_PASS', runs)
    for row in rows:
        if row.get('threads') != 20 or row.get('heap_live', 0) <= row.get('heap_before', 0):
            raise ValueError('thread baseline topology or live heap mismatch')
    return summary([row['heap_live'] - row['heap_before'] for row in rows])


CASES = {
    'c-wire-os': ('transport-c-wire-os-v2', 'c', 16),
    'c-wire-2': ('transport-c-wire-2-v2', 'c', 16),
    'rust-owned-z': ('transport-rust-owned-z-v3', 'rust', 16),
    'rust-borrowed-z': ('transport-rust-borrowed-z-v2', 'rust', 16),
    'rust-borrowed-2': ('transport-rust-borrowed-2-v2', 'rust', 16),
    'c-packet-wire-2': ('transport-c-packet-wire-2-v1', 'c', 28),
    'rust-packet-wire-2': ('transport-rust-packet-wire-2-v1', 'rust', 28),
    'c-packet-2': ('transport-c-packet-2-v1', 'c', 28),
    'rust-packet-2': ('transport-rust-packet-2-v1', 'rust', 28),
    'rust-packet-inplace-2': ('transport-rust-packet-inplace-2-v2', 'rust', 28),
}


def validate_policy(build, case, language, ack_bytes):
    processing = case in ('c-packet-2', 'rust-packet-2', 'rust-packet-inplace-2')
    if language == 'c':
        flags = set(build['c_defines'])
        if not {'NXRS_CQ_DEVICE', 'NXRS_CQ_SYNCHRONIZED'} <= flags:
            raise ValueError('C device/gate policy missing')
        for flag, expected in [('NXRS_CQ_WIRE_ONLY', not processing),
                               ('NXRS_CQ_PACKET_SERVICE', ack_bytes == 28),
                               ('NXRS_CQ_SPEED', case.endswith('-2')),
                               ('NXRS_PAYLOAD_C_SPEED', processing)]:
            if (flag in flags) != expected: raise ValueError('C processing/optimization policy mismatch')
    else:
        flags = set(build['features'])
        if not {'mq-backend','native-scale-worker','ffi-scale-entry','shared-mq-code','synchronized-scale'} <= flags:
            raise ValueError('Rust thread/queue/gate policy missing')
        for flag, expected in [('wire-only', not processing), ('packet-service', ack_bytes == 28),
                               ('borrowed-mq-io', case != 'rust-owned-z'),
                               ('packet-inplace-samples', case == 'rust-packet-inplace-2')]:
            if (flag in flags) != expected: raise ValueError('Rust processing/buffer policy mismatch')
        if build['app_opt_level'] != ('2' if case.endswith('-2') else 'z'):
            raise ValueError('Rust application optimization mismatch')
        if not build['std_source']['selected_source_matches']:
            raise ValueError('Rust std source mismatch')


def validate_artifacts(directory, build):
    """Bind the section counts to the same frozen build as the flashed image."""
    for name, expected in build['artifacts'].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'frozen build artifact changed: {name}')


def heap_accounting(workload, idle_increment, idle_live_heap, peak_heap):
    setup = workload['heap_producers']['median'] - workload['heap_before']['median']
    if setup < idle_increment or peak_heap < idle_live_heap:
        raise ValueError('workload heap is below its live idle-thread baseline')
    # Live idle heap includes command resources. Shell-idle heap plus the
    # spawned-thread increment alone would mischarge the command stack.
    return dict(queue_setup_heap=workload['heap_queues']['median'] - workload['heap_before']['median'],
                live_traffic_setup_heap=setup,
                above_idle_thread_heap=setup - idle_increment,
                peak_above_idle_thread_heap=peak_heap - idle_live_heap)


def build_report(root, runs):
    result = dict(schema=1, cases={}, cpu_mhz=240, thread_stack_reservation=19*4096+6144,
                  command_stack_reservation=8192)
    for name, (folder, language, ack_bytes) in CASES.items():
        directory = root / folder
        row = image_record('xtensa-esp32s3-elf-', directory, language)
        row['build'] = json.loads((directory / ('c-build-provenance.json' if language == 'c' else 'relink-provenance.json')).read_text())
        validate_artifacts(directory, row['build'])
        validate_policy(row['build'], name, language, ack_bytes)
        image = directory / f'{language}.merged.bin'
        measurement = directory / f'measure-{runs}'
        row['board'] = load_measurement(measurement, image, runs)
        if any(row['board']['repeat_retained']):
            raise ValueError('retained heap in repeated traffic runs')
        row['transport'] = validate_transport((measurement / 'serial.log').read_text(), language, runs, ack_bytes)
        baseline = directory / 'threads-3'
        row['thread_board'] = load_measurement(baseline, image, 3)
        if any(row['thread_board']['repeat_retained']):
            raise ValueError('retained heap after repeated idle-thread commands')
        row['thread_heap'] = validate_threads((baseline / 'serial.log').read_text(), 3, language)
        thread_rows = marker_rows((baseline / 'serial.log').read_text(),
                                  'CQ_C_THREADS_PASS' if language == 'c' else 'CQ_THREADS_PASS', 3)
        row['idle_thread_live_heap'] = summary([item['heap_live'] for item in thread_rows])
        workload = row['transport']['workload']
        row.update(heap_accounting(workload, row['thread_heap']['median'],
                                   row['idle_thread_live_heap']['median'], row['board']['peak_heap']))
        row['fixed_noinit'] = row['sections'].get('.noinit', 0)
        row['iram_sections'] = sum(row['sections'].get(k,0) for k in
                                  ('.iram0.vectors','.iram0.text','.iram0.text_end','.iram0.data','.iram0.bss'))
        row['section_ram_plus_peak_heap'] = row['fixed_dram'] + row['fixed_noinit'] + row['iram_sections'] + row['board']['peak_heap']
        result['cases'][name] = row
    identities = {row['config_identity'] for row in result['cases'].values()}
    iram = {row['sections']['.iram0.text'] for row in result['cases'].values()}
    if len(identities) != 1 or len(iram) != 1:
        raise ValueError('kernel configuration or IRAM section size changed')
    result['matched_config_identity'] = identities.pop()
    result['common_iram_bytes'] = iram.pop()
    result['sources'] = {str(p.relative_to(Path(__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in [Path(__file__), *Path(__file__).parent.glob('*processing.[ch]'),
                                   Path(__file__).parent / 'transport_gate.c',
                                   *Path(__file__).parent.joinpath('src').glob('*.rs')]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1: parser.error('runs must be positive')
    if args.out.exists(): parser.error('fresh output required')
    args.out.write_text(json.dumps(build_report(args.root.resolve(), args.runs), indent=2) + '\n')

if __name__ == '__main__': main()
