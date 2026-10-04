"""Shared validation for frozen firmware and completed device runs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics
import subprocess


def sections(prefix, path):
    output = subprocess.check_output([prefix + "size", "-A", str(path)], text=True)
    return {p[0]: int(p[1]) for line in output.splitlines()
            if len(p := line.split()) == 3 and p[0].startswith(".")}


_build_spec = importlib.util.spec_from_file_location(
    'nxrs_rtos_build', Path(__file__).resolve().parents[1] / 'rtos-bench/build.py')
_build = importlib.util.module_from_spec(_build_spec)
_build_spec.loader.exec_module(_build)


def summary(values):
    if not values:
        raise ValueError('empty sample set')
    return dict(min=min(values), median=statistics.median(values), max=max(values))


def load_measurement(directory, image, expected_runs):
    record = json.loads((directory / 'measurement.json').read_text())
    if (record['failure'] is not None or record['completed_runs'] != expected_runs
            or record['requested_runs'] != expected_runs
            or len(record['runs']) != expected_runs):
        raise ValueError(f'incomplete board measurement: {directory}')
    if record['image_sha256'] != hashlib.sha256(image.read_bytes()).hexdigest():
        raise ValueError(f'board image changed: {directory}')
    runs = record['runs']
    return dict(
        completed_runs=expected_runs, flashed=record['flashed'],
        command=record['command'], peak_heap=max(r['after']['maxused'] for r in runs),
        before_used=summary([r['before']['used'] for r in runs]),
        after_used=summary([r['after']['used'] for r in runs]),
        first_retained=runs[0]['used_delta'],
        repeat_retained=[r['used_delta'] for r in runs[1:]],
    )

def marker_rows(transcript, marker, count):
    rows = []
    for line in transcript.replace('\r', '').splitlines():
        if line.startswith(marker + ' '):
            fields = dict(re.findall(r'([a-z0-9_]+)=([^\s]+)', line))
            rows.append({key: int(value) if value.isdecimal() else value
                         for key, value in fields.items()})
    if len(rows) != count:
        raise ValueError(f'{marker}: expected {count} rows, found {len(rows)}')
    return rows


def validate_scale(transcript, language, workload, count, *, require_wake=True):
    prefix = 'CQ_C_' if language == 'c' else 'CQ_'
    rows = marker_rows(transcript, prefix + 'SCALE_PASS', count)
    expected = dict(mode='large', queues=60, logical_streams=60, threads=20,
                    messages=5760, digest=441_445_568 if workload == 'wire' else 455_992_726)
    for row in rows:
        if any(row.get(key) != value for key, value in expected.items()):
            raise ValueError(f'scale protocol/topology mismatch: {row}')
    wakes = marker_rows(transcript, prefix + 'WAKE_PASS', count) if require_wake else []
    for row in wakes:
        if any(row.get(key) != value for key, value in
               dict(trials=64, queues=3, threads=2).items()):
            raise ValueError(f'wake protocol/topology mismatch: {row}')
    keys = ('elapsed_us', 'rx_p50_us', 'rx_p99_us', 'rx_max_us', 'ack_p99_us',
            'heap_before', 'heap_queues', 'heap_workers', 'heap_producers', 'heap_after_join',
            'stack_main', 'stack_producer_max', 'stack_worker_max', 'stack_collector')
    wake_keys = ('sleep_clock_us', 'rx_p50_us', 'rx_p99_us', 'rx_max_us', 'ack_p99_us')
    return dict(checked_protocol=expected,
                workload={key: summary([row[key] for row in rows]) for key in keys},
                blocked_wake={key: summary([row[key] for row in wakes]) for key in wake_keys} if require_wake else None)


def image_record(prefix, directory, language):
    elf, image = directory / f'{language}.elf', directory / f'{language}.merged.bin'
    sizes = sections(prefix, elf)
    return dict(
        directory=str(directory), elf_sha256=hashlib.sha256(elf.read_bytes()).hexdigest(),
        image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        config_identity=_build.config_identity(directory / 'resolved.config'),
        sections=sizes,
        linked_flash=sum(sizes[k] for k in ('.flash.text', '.flash.rodata', '.dram0.data')),
        fixed_dram=sizes['.dram0.data'] + sizes['.dram0.bss'],
        unpadded_bytes=(directory / f'{language}.unpadded.bin').stat().st_size,
    )
