#!/usr/bin/env python3
"""Aggregate complete, restored Zephyr/NuttX comparison matrix runs."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
SERVICE = HERE.parent / 'service-footprint'
NUTTX_REVISIONS = {
    'nuttx': '2f3eb6d6774ab63b75788c27bde7644da48121b2',
    'nuttx_apps': '85539a1223c4770ee36e68817f5bfe91e6b49369',
}
MAX_FIRST_RETAINED_BYTES = 4096
sys.path.insert(0, str(SERVICE))
from evidence import marker_rows, summary, validate_scale  # noqa: E402
from transport_pipeline_report import validate_policy, validate_transport  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load('zephyr_comparison_build', HERE / 'build.py')
matrix_runner = _load('zephyr_comparison_matrix', HERE / 'run_matrix.py')
measure = _load('zephyr_comparison_measure', HERE / 'measure.py')
nuttx_build = _load('zephyr_comparison_nuttx_build', HERE.parent / 'rtos-bench' / 'build.py')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _nuttx_config_contract(path):
    values = build.resolved_config(Path(path).read_text())
    expected = {
        'CONFIG_ARCH_CHIP': 'esp32s3',
        'CONFIG_ARCH_CHIP_ESP32S3': 'y',
        'CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ': '240',
        'CONFIG_ESP32S3_FLASH_16M': 'y',
        'CONFIG_ESP32S3_PSRAM_8M': 'y',
        'CONFIG_ESP32S3_SPIRAM_BOOT_INIT': 'y',
        'CONFIG_ESP32S3_SPIRAM_COMMON_HEAP': 'y',
        'CONFIG_MM_REGIONS': '1',
        'CONFIG_ARCH_INTERRUPTSTACK': '4096',
        'CONFIG_USEC_PER_TICK': '10000',
        'CONFIG_RR_INTERVAL': '10',
        'CONFIG_SMP': 'n',
    }
    wrong = {key: (values.get(key), value) for key, value in expected.items()
             if values.get(key) != value}
    if wrong:
        raise ValueError(f'NuttX hardware configuration contract mismatch: {wrong}')
    dio = (values.get('CONFIG_ESPRESSIF_FLASH_MODE_DIO') == 'y' or
           values.get('CONFIG_ESP32S3_FLASH_MODE_DIO') == 'y')
    frequency = values.get('CONFIG_ESPRESSIF_FLASH_FREQ', '').lower()
    freq_40 = (frequency == '40m' or values.get('CONFIG_ESPRESSIF_FLASH_FREQ_40M') == 'y' or
               values.get('CONFIG_ESP32S3_FLASH_FREQ') == '40')
    if not dio or not freq_40:
        raise ValueError('NuttX hardware configuration requires DIO flash at 40 MHz')
    selected = {key: values[key] for key in expected}
    for key in ('CONFIG_DEBUG_ASSERTIONS', 'CONFIG_STACK_COLORATION',
                'CONFIG_ARCH_HAVE_STACKCHECK', 'CONFIG_DEFAULT_TASK_STACKSIZE',
                'CONFIG_INIT_STACKSIZE', 'CONFIG_IDLETHREAD_STACKSIZE'):
        if key in values:
            selected[key] = values[key]
    for key in ('CONFIG_ESPRESSIF_FLASH_MODE_DIO', 'CONFIG_ESPRESSIF_FLASH_FREQ',
                'CONFIG_ESPRESSIF_FLASH_FREQ_40M', 'CONFIG_ESP32S3_FLASH_MODE_DIO',
                'CONFIG_ESP32S3_FLASH_FREQ'):
        if key in values:
            selected[key] = values[key]
    return selected


def _zephyr_config_contract(directory, provenance):
    values = build.resolved_config((directory / 'resolved.config').read_text())
    expected = {'CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC': '240000000',
                'CONFIG_MAIN_STACK_SIZE': '8192', 'CONFIG_ISR_STACK_SIZE': '4096',
                'CONFIG_HEAP_MEM_POOL_ADD_SIZE_BOARD': '4096',
                'CONFIG_TIMESLICING': 'y', 'CONFIG_TIMESLICE_SIZE': '10',
                'CONFIG_TIMESLICE_PRIORITY': '0', 'CONFIG_ESPTOOLPY_FLASHMODE_DIO': 'y'}
    wrong = {key: (values.get(key), value) for key, value in expected.items()
             if values.get(key) != value}
    configuration = provenance.get('configuration', {})
    frequency = configuration.get('flash_frequency_mhz')
    if frequency not in (40, 80):
        wrong['flash_frequency_mhz'] = (frequency, '40 or 80')
    else:
        symbol = f'CONFIG_ESPTOOLPY_FLASHFREQ_{frequency}M'
        if values.get(symbol) != 'y':
            wrong[symbol] = (values.get(symbol), 'y')
    if wrong:
        raise ValueError(f'Zephyr hardware configuration contract mismatch: {wrong}')
    debug_policy = {key: values.get(key) for key in
                    ('CONFIG_ASSERT', 'CONFIG_INIT_STACKS', 'CONFIG_STACK_SENTINEL',
                     'CONFIG_THREAD_STACK_INFO', 'CONFIG_THREAD_MONITOR')}
    lean = (debug_policy['CONFIG_ASSERT'] != 'y' and
            debug_policy['CONFIG_STACK_SENTINEL'] != 'y' and
            debug_policy['CONFIG_THREAD_MONITOR'] != 'y')
    return {
        'cpu_clock_hz': int(values['CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC']),
        'main_stack_bytes': int(values['CONFIG_MAIN_STACK_SIZE']),
        'interrupt_stack_bytes': int(values['CONFIG_ISR_STACK_SIZE']),
        'kernel_heap_reserved_bytes': int(values['CONFIG_HEAP_MEM_POOL_ADD_SIZE_BOARD']),
        'flash_mode': 'DIO', 'flash_frequency_mhz': frequency,
        'cache': configuration.get('cache'),
        'timing': configuration.get('timing'),
        'kernel_heap': configuration.get('heap'),
        'debug_policy': debug_policy,
        'configuration_variant': {'native_defaults': frequency == 80, 'lean': lean,
                                  'hardware_matched': frequency == 40,
                                  'assertion_matched': debug_policy['CONFIG_ASSERT'] == 'y'},
    }


def _json(path):
    return json.loads(Path(path).read_text())


def _safe_source_name(value):
    path = Path(value)
    if not path.is_absolute():
        path = HERE / path
    try:
        return path.resolve().relative_to(PROJECT.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _service_source_for_hash(digest):
    candidates = sorted([*SERVICE.glob('*.c'), *SERVICE.glob('*.h')])
    for candidate in candidates:
        if sha256(candidate) == digest:
            return candidate.relative_to(PROJECT).as_posix()
    return None


def _source_field_name(field, value, digest, source_records):
    if field == 'rust_module_sha256':
        return f'tests/service-footprint/src/{Path(value).name}'
    if field in ('helper_sha256', 'header_sha256'):
        match = _service_source_for_hash(digest)
        if match:
            return match
        original = next((name for name, original_digest in source_records.items()
                         if original_digest == digest), None)
        if original:
            return _safe_source_name(original)
        return f'generated-helpers/{Path(value).name}'
    if field == 'source_sha256':
        match = _service_source_for_hash(digest)
        if match:
            return match
    return _safe_source_name(value)


def _source_hashes(provenance, directory):
    sources = {}
    source_records = provenance.get('source_sha256', {})
    for field in ('source_sha256', 'rust_module_sha256', 'helper_sha256', 'header_sha256'):
        values = provenance.get(field, {})
        if isinstance(values, dict):
            for name, digest in values.items():
                safe_name = _source_field_name(field, name, digest, source_records)
                if safe_name in sources and sources[safe_name] != digest:
                    safe_name = f'{field}/{Path(name).name}'
                sources[safe_name] = digest
    if provenance.get('rust_source_sha256'):
        cargo = provenance.get('cargo_command', [])
        binary = cargo[cargo.index('--bin') + 1] if '--bin' in cargo else 'cq-scale'
        source = 'payload_processing.rs' if 'payload' in binary else 'scale.rs'
        sources[f'tests/service-footprint/src/{source}'] = provenance['rust_source_sha256']
    return dict(sorted(sources.items()))


def _safe_artifact_hashes(values):
    return {Path(name).name if Path(name).is_absolute() else Path(name).as_posix(): digest
            for name, digest in values.items()}


def _validate_transport_without_wake(transcript, language, runs, ack_bytes):
    """Validate the matched C control's traffic rows without inventing a wake marker."""
    if '_FAIL' in transcript or 'MEASUREMENT_FAILED' in transcript:
        raise ValueError('failure marker in transport transcript')
    validate_scale(transcript, language, 'wire', runs, require_wake=False)
    rows = marker_rows(transcript, 'CQ_TRANSPORT_PASS', runs)
    expected = {'language': language, 'mode': 'large', 'event_bytes': 248,
                'ack_bytes': ack_bytes}
    for row in rows:
        if any(row.get(key) != value for key, value in expected.items()):
            raise ValueError('transport mode/layout mismatch')
        if not isinstance(row.get('cycles'), int) or not 0 < row['cycles'] < 3_840_000_000:
            raise ValueError('invalid or wrapped cycle clock')
    return rows


def _matrix_records(matrix_dirs):
    if not matrix_dirs:
        raise ValueError('at least one --matrix directory is required')
    all_cases = set()
    seen_directories = set()
    records = []
    for directory in matrix_dirs:
        resolved_directory = directory.resolve()
        if resolved_directory in seen_directories:
            raise ValueError(f'duplicate matrix directory: {directory.name}')
        seen_directories.add(resolved_directory)
        record = _json(directory / 'matrix.json')
        if record.get('failure') is not None or record.get('restore_verified') is not True:
            raise ValueError(f'matrix must be successful and restored: {directory.name}')
        runs, blocks, order = record.get('runs_per_block'), record.get('blocks'), record.get('order')
        if type(runs) is not int or runs < 1 or type(blocks) is not int or blocks < 1:
            raise ValueError(f'invalid matrix dimensions: {directory.name}')
        if not isinstance(order, list):
            raise ValueError(f'invalid matrix order: {directory.name}')
        block_cases = {}
        normalized_order = []
        for item in order:
            if (not isinstance(item, dict) or item.get('case') not in matrix_runner.CASES or
                    type(item.get('block')) is not int):
                raise ValueError(f'invalid matrix order entry: {item!r}')
            case, block = item['case'], item['block']
            if not 0 <= block < blocks:
                raise ValueError(f'out-of-range block index: {block}')
            block_cases.setdefault(block, []).append(case)
            normalized_order.append({'case': case, 'block': block})
        if set(block_cases) != set(range(blocks)):
            raise ValueError(f'matrix is missing one or more blocks: {directory.name}')
        case_sets = []
        for block in range(blocks):
            cases = block_cases[block]
            if len(cases) != len(set(cases)) or not cases:
                raise ValueError(f'block {block} has duplicate or no cases')
            case_sets.append(set(cases))
            if block > 0 and set(cases) != case_sets[0]:
                raise ValueError(f'block {block} has a different case set')
        cases = case_sets[0]
        all_cases.update(cases)
        if len(order) != blocks * len(cases):
            raise ValueError(f'matrix order is not a complete set of blocks: {directory.name}')
        records.append({'directory': directory, 'record': record, 'order': normalized_order,
                        'cases': sorted(cases), 'blocks': blocks, 'runs_per_block': runs})
    return records, all_cases


def _frozen_build(artifacts, case, readelf, nuttx_compiler_version=None):
    directory = matrix_runner.case_directory(artifacts, case)
    image = matrix_runner.frozen_image(directory, case)
    os_name, _, language, mode = matrix_runner.CASES[case]
    source_verification = None
    if os_name == 'zephyr':
        provenance = _json(directory / 'build-provenance.json')
        if sha256(directory / 'zephyr.map') != provenance.get('map_sha256'):
            raise ValueError(f'changed frozen artifact: {case}/zephyr.map')
        artifact_hashes = {
            'zephyr.bin': provenance['zephyr_bin_sha256'],
            'zephyr/zephyr.elf': provenance['elf_sha256'],
            'zephyr.map': provenance['map_sha256'],
            'resolved.config': provenance['resolved_config_sha256'],
        }
        elf = directory / 'zephyr/zephyr.elf'
        config_identity = provenance['resolved_config_sha256']
        hardware_contract = _zephyr_config_contract(directory, provenance)
        source_verification = build.verify_firmware_sources(
            directory, mode, hardware_contract['configuration_variant'], HERE)
        sources = {_safe_source_name(name): digest for name, digest in
                   source_verification.pop('firmware_sources').items()}
        for group in ('historical_non_firmware', 'current_tools'):
            for field in ('source_sha256', 'legacy_source_sha256', 'input_not_used_sha256'):
                if field in source_verification[group]:
                    source_verification[group][field] = {
                        _safe_source_name(name): digest for name, digest in
                        source_verification[group][field].items()}
        for change in source_verification.get('differences', []):
            change['name'] = _safe_source_name(change['name'])
        source_verification['recorded_tooling_snapshot_sha256'] = {
            _safe_source_name(name): digest for name, digest in
            provenance.get('tool_sha256', {}).items()}
        app_profile = {'mode': mode, 'profile': provenance.get('profile'),
                       'toolchain_variant': provenance.get('toolchain_variant'),
                       'configuration_variant': hardware_contract['configuration_variant'],
                       'auxiliary_wake_probe': False}
        os_identity = {'zephyr': provenance.get('revisions', {}).get('zephyr'),
                       'hal_espressif': provenance.get('revisions', {}).get('hal_espressif'),
                       'hal_xtensa': provenance.get('revisions', {}).get('hal_xtensa')}
        compiler = provenance.get('compiler', {})
        version_lines = compiler.get('version', '').splitlines()
        tool_identity = {'readelf': Path(provenance.get('readelf', 'readelf')).name,
                         'compiler': Path(compiler.get('path', 'unknown')).name,
                         'compiler_version': version_lines[0] if version_lines else None}
        image_bytes = image.stat().st_size
        unpadded_bytes = None
    else:
        prov_name = 'c-build-provenance.json' if language == 'c' else 'relink-provenance.json'
        provenance = _json(directory / prov_name)
        artifact_hashes = _safe_artifact_hashes(provenance['artifacts'])
        elf = directory / f'{language}.elf'
        normalized_config_identity = nuttx_build.config_identity(directory / 'resolved.config')
        if provenance.get('config_identity') not in (None, normalized_config_identity):
            raise ValueError(f'NuttX provenance/config identity mismatch: {case}')
        config_identity = normalized_config_identity
        app_profile = {'mode': mode, 'command': provenance.get('command'),
                       'command_stack_bytes': provenance.get('command_stack'),
                       'c_defines': provenance.get('c_defines'),
                       'features': provenance.get('features'),
                       'app_opt_level': provenance.get('app_opt_level'),
                       'auxiliary_wake_probe': mode != 'baseline' and case != 'c-packet-matched-2'}
        os_identity = {**NUTTX_REVISIONS, 'config_sha256': config_identity}
        if mode != 'baseline':
            policy_case = {'rust-wire-2': 'rust-borrowed-2',
                           'c-packet-matched-2': 'c-packet-2'}.get(case, case)
            validate_policy(provenance, policy_case, language, 16 if mode == 'wire' else 28)
        commands = provenance.get('make_command', [])
        prefix = next((str(value).partition('=')[2] for value in commands
                       if str(value).startswith('CROSSDEV=')), 'xtensa-esp32s3-elf-')
        cargo = provenance.get('cargo_command', [])
        tool_identity = {'cross_prefix': prefix,
                         'cargo': Path(str(cargo[0])).name if cargo else None,
                         'rust_toolchain': '1.90.0' if cargo else None,
                         'compiler': Path(nuttx_compiler_version[0]).name if nuttx_compiler_version else None,
                         'compiler_version': nuttx_compiler_version[1] if nuttx_compiler_version else None}
        if nuttx_compiler_version:
            try:
                comment = subprocess.check_output(
                    [str(readelf), '-p', '.comment', str(elf)], text=True,
                    stderr=subprocess.STDOUT)
            except subprocess.SubprocessError:
                comment = ''
            version = next((part.strip('(),') for part in nuttx_compiler_version[1].split()
                            if '.' in part and part[0].isdigit()), None)
            verified = bool(version and version in comment)
            if comment and not verified:
                raise ValueError(f'NuttX compiler version does not match ELF .comment: {case}')
            tool_identity['compiler_version_verified_by_elf'] = verified or None
        image_bytes = image.stat().st_size
        unpadded = directory / f'{language}.unpadded.bin'
        unpadded_bytes = unpadded.stat().st_size if unpadded.is_file() else None
        sources = _source_hashes(provenance, directory)
    output = subprocess.check_output([str(readelf), '-W', '-S', str(elf)], text=True)
    sections = build.parse_sections(output)
    accounting = build.section_accounting(sections)
    if os_name == 'nuttx':
        hardware_contract = _nuttx_config_contract(directory / 'resolved.config')
    return {'directory': directory, 'image': image, 'elf': elf, 'os': os_name,
            'language': language, 'mode': mode, 'provenance': provenance,
            'artifact_hashes': artifact_hashes, 'elf_sha256': sha256(elf),
            'image_sha256': sha256(image), 'config_identity': config_identity,
            'os_identity': os_identity, 'tool_identity': tool_identity,
            'app_profile': app_profile, 'source_hashes': sources,
            'source_verification': source_verification,
            'hardware_contract': hardware_contract,
            'accounting': accounting, 'sections': sections,
            'image_bytes': image_bytes, 'unpadded_bytes': unpadded_bytes}


def _rows_with_optional_primer(transcript, marker, runs):
    for count in (runs, runs + 1):
        try:
            rows = marker_rows(transcript, marker, count)
            return rows, count - runs
        except ValueError:
            pass
    raise ValueError(f'{marker}: expected {runs} rows, optionally one untimed primer')


def _json_run_rows(case, matrix, frozen):
    rows = []
    baseline = frozen['mode'] == 'baseline'
    runs_per_block = matrix['runs_per_block']
    for block in range(matrix['blocks']):
        directory = matrix['directory'] / f'{case}-block-{block}'
        record = _json(directory / 'measurement.json')
        if (record.get('failure') is not None or record.get('requested_runs') != runs_per_block or
                record.get('completed_runs') != runs_per_block or
                len(record.get('runs', [])) != runs_per_block):
            raise ValueError(f'incomplete measurement: {directory.name}')
        if record.get('image_sha256') != frozen['image_sha256']:
            raise ValueError(f'measurement image hash mismatch: {directory.name}')
        transcript = (directory / 'serial.log').read_text(errors='replace')
        if frozen['os'] == 'nuttx':
            app = 'cq_c_scale' if frozen['language'] == 'c' else 'cq_scale'
            expected_command = app + (' baseline' if baseline else ' large')
            if record.get('command') != expected_command or record.get('flashed') is not True:
                raise ValueError(f'NuttX command/image was not freshly flashed: {directory.name}')
            retained = [row.get('used_delta') for row in record['runs']]
            if any(type(value) is not int for value in retained):
                raise ValueError(f'missing NuttX retained-heap evidence: {directory.name}')
            if retained[0] < 0 or retained[0] > MAX_FIRST_RETAINED_BYTES:
                raise ValueError(f'first NuttX retained heap exceeds the {MAX_FIRST_RETAINED_BYTES}-byte bound')
            if any(value != 0 for value in retained[1:]):
                raise ValueError(f'repeated NuttX runs retained heap: {directory.name}')
            if baseline:
                if '_FAIL' in transcript or 'MEASUREMENT_FAILED' in transcript:
                    raise ValueError(f'failed baseline transcript: {directory.name}')
                if transcript.replace('\r', '').splitlines().count('NUTTX_BASELINE_PASS') != runs_per_block:
                    raise ValueError(f'baseline marker count mismatch: {directory.name}')
                scale_rows = transport_rows = wake_rows = []
            else:
                ack = 16 if frozen['mode'] == 'wire' else 28
                prefix = 'CQ_C_' if frozen['language'] == 'c' else 'CQ_'
                scale_rows = marker_rows(transcript, prefix + 'SCALE_PASS', runs_per_block)
                if case == 'c-packet-matched-2':
                    transport_rows = _validate_transport_without_wake(
                        transcript, frozen['language'], runs_per_block, ack)
                    wake_rows = [None] * runs_per_block
                else:
                    validate_transport(transcript, frozen['language'], runs_per_block, ack)
                    transport_rows = marker_rows(transcript, 'CQ_TRANSPORT_PASS', runs_per_block)
                    wake_rows = marker_rows(transcript, prefix + 'WAKE_PASS', runs_per_block)
            for index, measurement in enumerate(record['runs']):
                row = {'matrix': matrix['directory'].name,
                       'block': matrix['global_block_start'] + block,
                       'matrix_block': block, 'run': index}
                if not baseline:
                    row['measurement'] = _numeric_tree(measurement)
                    row.update(transport=transport_rows[index], scale=scale_rows[index], wake=wake_rows[index])
                else:
                    row['measurement'] = _numeric_tree(measurement)
                rows.append(row)
        else:
            if record.get('mode') != frozen['mode'] or record.get('command') != ('baseline' if baseline else 'large'):
                raise ValueError(f'measurement command/mode mismatch: {directory.name}')
            if 'FAIL' in transcript:
                raise ValueError(f'failure marker in Zephyr transcript: {directory.name}')
            zephyr_transcript = transcript.replace('zephyr> ', '').replace('\r', '')
            if baseline:
                memory_rows, memory_offset = _rows_with_optional_primer(
                    zephyr_transcript, 'ZEPHYR_MEMORY', runs_per_block)
                exit_rows, exit_offset = _rows_with_optional_primer(
                    zephyr_transcript, 'ZEPHYR_COMMAND_EXIT', runs_per_block)
                if memory_offset != exit_offset:
                    raise ValueError(f'baseline primer marker mismatch: {directory.name}')
                markers = {'memory': memory_rows, 'exit': exit_rows}
                baseline_count = zephyr_transcript.splitlines().count('ZEPHYR_BASELINE_PASS')
                if baseline_count not in (runs_per_block, runs_per_block + 1):
                    raise ValueError(f'baseline marker count mismatch: {directory.name}')
                if baseline_count == runs_per_block + 1 and memory_offset == 1:
                    primer = ('ZEPHYR_BASELINE_PASS\n' +
                              _marker_line('memory', markers['memory'][0]) + '\n' +
                              _marker_line('exit', markers['exit'][0]))
                    measure.validate(primer.encode(), 'baseline', 'baseline')
                    markers['memory'], markers['exit'] = markers['memory'][1:], markers['exit'][1:]
                elif baseline_count != runs_per_block or memory_offset:
                    raise ValueError(f'baseline measurement marker count mismatch: {directory.name}')
                for index, saved in enumerate(record['runs']):
                    if markers['exit'][index] != {'status': 0} or saved.get('memory') != markers['memory'][index]:
                        raise ValueError(f'Zephyr baseline JSON/serial evidence mismatch: {directory.name} run {index}')
            else:
                parsed = {key: marker_rows(zephyr_transcript, marker, runs_per_block)
                          for key, marker in (('transport', 'CQ_TRANSPORT_PASS'),
                                              ('scale', 'CQ_ZEPHYR_SCALE_PASS'))}
                resource_rows = {key: _rows_with_optional_primer(zephyr_transcript, marker, runs_per_block)
                                 for key, marker in (('resources', 'ZEPHYR_RESOURCES'),
                                                     ('memory', 'ZEPHYR_MEMORY'),
                                                     ('exit', 'ZEPHYR_COMMAND_EXIT'))}
                offsets = {offset for _, offset in resource_rows.values()}
                if len(offsets) != 1:
                    raise ValueError(f'Zephyr primer rows are inconsistent: {directory.name}')
                primer_offset = offsets.pop()
                parsed.update({key: rows for key, (rows, _) in resource_rows.items()})
                if primer_offset:
                    thread = marker_rows(zephyr_transcript, 'CQ_ZEPHYR_THREADS_PASS', 1)[0]
                    primer = '\n'.join((_marker_line('thread', thread),
                                         _marker_line('resources', parsed['resources'][0]),
                                         _marker_line('memory', parsed['memory'][0]),
                                         _marker_line('exit', parsed['exit'][0])))
                    measure.validate(primer.encode(), 'threads', frozen['mode'])
                    for key in ('resources', 'memory', 'exit'):
                        parsed[key] = parsed[key][1:]
                else:
                    marker_rows(zephyr_transcript, 'CQ_ZEPHYR_THREADS_PASS', 0)
                for index, saved in enumerate(record['runs']):
                    synthetic = '\n'.join(_marker_line(key, parsed[key][index]) for key in
                                           ('transport', 'scale', 'resources', 'memory', 'exit'))
                    validated = measure.validate(synthetic.encode(), 'large', frozen['mode'])
                    for key in ('transport', 'scale', 'resources', 'memory'):
                        if saved.get(key) != validated[key] or saved.get(key) != parsed[key][index]:
                            raise ValueError(f'Zephyr JSON/serial evidence mismatch: {directory.name} run {index}')
                    required_scale = ('elapsed_us', 'rx_p99_us', 'heap_before', 'heap_queues',
                                      'heap_workers', 'heap_producers', 'heap_after_join',
                                      'stack_main', 'stack_producer_max', 'stack_worker_max',
                                      'stack_collector')
                    if any(type(saved['scale'].get(key)) is not int for key in required_scale):
                        raise ValueError(f'incomplete Zephyr scale resource/timing row: {directory.name} run {index}')
                    expected_buffers = 12120 if frozen['mode'] == 'wire' else 12840
                    if (saved['resources'].get('queue_buffers') != expected_buffers or
                            saved['resources'].get('stack_storage') != 83968 or
                            saved['memory'].get('kernel_heap_reserved') !=
                            frozen['hardware_contract']['kernel_heap_reserved_bytes']):
                        raise ValueError(f'Zephyr resource reservation mismatch: {directory.name} run {index}')
            for index, _measurement in enumerate(record['runs']):
                row = {'matrix': matrix['directory'].name,
                       'block': matrix['global_block_start'] + block,
                       'matrix_block': block, 'run': index}
                if not baseline:
                    for key in ('transport', 'scale', 'resources', 'memory'):
                        row[key] = parsed[key][index]
                else:
                    row['memory'] = markers['memory'][index]
                rows.append(row)
    return rows


def _numeric_tree(value):
    if isinstance(value, dict):
        return {key: _numeric_tree(item) for key, item in value.items()
                if isinstance(item, (int, float)) and not isinstance(item, bool) or isinstance(item, dict)}
    if isinstance(value, list):
        return [_numeric_tree(item) for item in value if isinstance(item, (int, float, dict))]
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _marker_line(key, values):
    marker = {'transport': 'CQ_TRANSPORT_PASS', 'scale': 'CQ_ZEPHYR_SCALE_PASS',
              'resources': 'ZEPHYR_RESOURCES', 'memory': 'ZEPHYR_MEMORY',
              'exit': 'ZEPHYR_COMMAND_EXIT', 'thread': 'CQ_ZEPHYR_THREADS_PASS'}[key]
    return marker + ' ' + ' '.join(f'{name}={value}' for name, value in values.items())


def _timing_summary(rows):
    cycles = [row['transport']['cycles'] for row in rows]
    return {'completed_runs': len(rows),
            'primary_cycles': summary(cycles),
            'primary_elapsed_us': summary([value / 240 for value in cycles]),
            'latency_rx_p99_us': summary([row['scale']['rx_p99_us'] for row in rows]),
            'elapsed_us': summary([row['scale']['elapsed_us'] for row in rows])}


def _paired_timing(left, right):
    """Compare only invocations from shared, rotated matrix blocks."""
    common = sorted({row['matrix'] for row in left['runs']} &
                    {row['matrix'] for row in right['runs']})
    if not common:
        raise ValueError('matched packet cases have no shared measurement matrix')
    paired = []
    for case in (left, right):
        rows = [row for row in case['runs'] if row['matrix'] in common]
        keys = {(row['matrix'], row['matrix_block'], row['run']) for row in rows}
        if len(keys) != len(rows):
            raise ValueError('duplicate matched packet invocation')
        paired.append((rows, keys))
    if paired[0][1] != paired[1][1]:
        raise ValueError('matched packet matrix invocation counts differ')
    return {'matrices': common, 'runs_per_case': len(paired[0][0]),
            'nuttx_c': _timing_summary(paired[0][0]),
            'zephyr_c': _timing_summary(paired[1][0])}


def _case_report(case, matrices, frozen):
    all_rows = []
    for matrix in matrices:
        if case in matrix['cases']:
            all_rows.extend(_json_run_rows(case, matrix, frozen))
    baseline = frozen['mode'] == 'baseline'
    ram = frozen['accounting']['resident_ram_bytes']
    out = {
        'os': frozen['os'], 'language': frozen['language'], 'mode': frozen['mode'],
        'identity': {'os': frozen['os_identity'], 'toolchain': frozen['tool_identity'],
                     'compiler': {'executable': frozen['tool_identity'].get('compiler') or
                                               frozen['tool_identity'].get('cross_prefix'),
                                  'version': frozen['tool_identity'].get('compiler_version')},
                     'app_profile': frozen['app_profile'], 'config': frozen['config_identity'],
                     'hardware_contract': frozen['hardware_contract'],
                     'sources_sha256': frozen['source_hashes'],
                     'source_verification': frozen['source_verification'],
                     'artifacts_sha256': frozen['artifact_hashes'],
                     'image_sha256': frozen['image_sha256'], 'elf_sha256': frozen['elf_sha256']},
        'sizes': {'bin_bytes': frozen['image_bytes'], 'unpadded_bin_bytes': frozen['unpadded_bytes']},
        'resource_ledger': {'loadbearing_flash_bytes': frozen['accounting']['loadbearing_flash_bytes'],
                            'resident_ram_bytes': ram,
                            'flash_sections': frozen['accounting']['flash_sections'],
                            'resident_ram_sections': frozen['accounting']['resident_ram_sections'],
                            'excluded_dummy_padding': frozen['accounting']['excluded_dummy_padding']},
        'elf_sections_bytes': {section['name']: section['size'] for section in frozen['sections']},
        'completed_runs': len(all_rows),
        'runs_per_matrix': {matrix['directory'].name:
                            sum(row['matrix'] == matrix['directory'].name for row in all_rows)
                            for matrix in matrices if case in matrix['cases']},
        'runs': all_rows,
    }
    if frozen['os'] == 'nuttx':
        peaks = [row['measurement']['after']['maxused'] for row in all_rows]
        out['resource_ledger']['peak_heap_bytes'] = max(peaks)
        out['resource_ledger']['whole_ram_footprint_bytes'] = ram + max(peaks)
        out['resource_ledger']['peak_heap_scope'] = 'internal SRAM'
        out['heap_retention'] = {
            'first_retained_limit_bytes': MAX_FIRST_RETAINED_BYTES,
            'first_retained_by_block_bytes': [
                {'matrix': row['matrix'], 'block': row['block'],
                 'bytes': row['measurement']['used_delta']}
                for row in all_rows if row['run'] == 0],
            'repeat_retained_bytes': [row['measurement']['used_delta']
                                      for row in all_rows if row['run'] > 0],
            'repeat_retained_all_zero': all(row['measurement']['used_delta'] == 0
                                            for row in all_rows if row['run'] > 0),
        }
    else:
        memories = [row['memory'] for row in all_rows]
        if not memories:
            raise ValueError('Zephyr case has no memory evidence')
        reserved = {row['kernel_heap_reserved'] for row in memories}
        if len(reserved) != 1:
            raise ValueError('Zephyr kernel heap reservation changed between runs')
        out['resource_ledger']['kernel_heap_reserved_bytes'] = reserved.pop()
        out['resource_ledger']['kernel_heap_used_peak_bytes'] = max(row['kernel_heap_peak'] for row in memories)
        # Static resident RAM already includes the kernel heap arena and all static resources.
        out['resource_ledger']['whole_ram_footprint_bytes'] = ram
    if not baseline:
        scale_rows = [row['scale'] for row in all_rows]
        out['heap_milestones_bytes'] = {
            key: summary([row[key] for row in scale_rows])
            for key in ('heap_before', 'heap_queues', 'heap_workers', 'heap_producers', 'heap_after_join')}
        out['stack_high_water_bytes'] = {
            key: summary([row[key] for row in scale_rows])
            for key in ('stack_main', 'stack_producer_max', 'stack_worker_max', 'stack_collector')}
        out['resource_ledger']['native'] = {
            'heap_milestones_bytes': out['heap_milestones_bytes'],
            'stack_high_water_bytes': out['stack_high_water_bytes'],
        }
        out['timing'] = _timing_summary(all_rows)
        out['timing_by_matrix'] = {
            name: _timing_summary([row for row in all_rows if row['matrix'] == name])
            for name in out['runs_per_matrix']}
        if frozen['os'] == 'zephyr':
            resources = [row['resources'] for row in all_rows]
            out['resource_ledger']['native'].update({
                key: summary([row[key] for row in resources])
                for key in ('queue_buffers', 'queue_objects', 'thread_objects',
                            'stack_storage', 'entry_storage', 'heap_allocated')})
            out['resource_ledger']['native']['kernel_heap_used_bytes'] = summary(
                [row['kernel_heap_used'] for row in memories])
            out['resource_ledger']['native']['kernel_heap_peak_bytes'] = summary(
                [row['kernel_heap_peak'] for row in memories])
    else:
        # Baseline comparisons are intentionally limited to linked/static/peak resource values.
        out['resource_ledger'] = {
            'loadbearing_flash_bytes': frozen['accounting']['loadbearing_flash_bytes'],
            'resident_ram_bytes': ram,
            'peak_heap_bytes': max(row['measurement']['after']['maxused'] for row in all_rows)
            if frozen['os'] == 'nuttx' else max(row['memory']['kernel_heap_peak'] for row in all_rows),
            'whole_ram_footprint_bytes': (ram + max(row['measurement']['after']['maxused'] for row in all_rows)
                                          if frozen['os'] == 'nuttx' else ram),
        }
    return out


def build_report(artifacts, matrix_dirs, readelf, nuttx_compiler=None):
    readelf = Path(readelf).resolve(strict=True)
    compiler_version = None
    if nuttx_compiler is not None:
        compiler = Path(nuttx_compiler).resolve(strict=True)
        version_lines = subprocess.check_output([str(compiler), '--version'], text=True).splitlines()
        if not version_lines:
            raise ValueError('NuttX compiler returned no version information')
        compiler_version = (compiler.name, version_lines[0])
    matrices, cases = _matrix_records(matrix_dirs)
    global_block_start = 0
    for matrix in matrices:
        matrix['global_block_start'] = global_block_start
        global_block_start += matrix['blocks']
    frozen_cases = {case: _frozen_build(artifacts, case, readelf, compiler_version)
                    for case in sorted(cases)}
    nuttx_identities = {case['config_identity'] for case in frozen_cases.values()
                        if case['os'] == 'nuttx'}
    if len(nuttx_identities) > 1:
        raise ValueError('NuttX cases do not share one normalized kernel configuration identity')
    result_cases = {case: _case_report(case, matrices, frozen_cases[case])
                    for case in sorted(cases)}
    baseline_deltas = {}
    for os_name, baseline_name in (('nuttx', 'nuttx-baseline'), ('zephyr', 'zephyr-baseline')):
        baseline = result_cases.get(baseline_name)
        if not baseline:
            continue
        for name, case in result_cases.items():
            if name == baseline_name or case['os'] != os_name:
                continue
            baseline_deltas[name] = {
                'baseline_case': baseline_name,
                'loadbearing_flash_bytes': case['resource_ledger']['loadbearing_flash_bytes'] - baseline['resource_ledger']['loadbearing_flash_bytes'],
                'resident_ram_bytes': case['resource_ledger']['resident_ram_bytes'] - baseline['resource_ledger']['resident_ram_bytes'],
                'whole_ram_footprint_bytes': case['resource_ledger']['whole_ram_footprint_bytes'] - baseline['resource_ledger']['whole_ram_footprint_bytes'],
                'elf_sections_bytes': {
                    section: case['elf_sections_bytes'].get(section, 0) - baseline['elf_sections_bytes'].get(section, 0)
                    for section in sorted(set(case['elf_sections_bytes']) | set(baseline['elf_sections_bytes']))
                    if case['elf_sections_bytes'].get(section, 0) != baseline['elf_sections_bytes'].get(section, 0)
                },
            }
    matrix_summary = []
    global_block = 0
    for matrix in matrices:
        for block in range(matrix['blocks']):
            entries = [item for item in matrix['order'] if item['block'] == block]
            matrix_summary.append({'block': global_block, 'source': matrix['directory'].name,
                                   'matrix_block': block,
                                   'runs_per_block': matrix['runs_per_block'],
                                   'restored': True,
                                   'order': [{'case': item['case'], 'block': item['block']}
                                             for item in entries]})
            global_block += 1
    report = {'schema': 1, 'cpu_mhz': 240, 'blocks': len(matrix_summary),
              'timing_aggregation': 'Case summaries pool all validated runs of the same '
                                    'frozen image; per-matrix summaries and counts are separate.',
              'nuttx_kernel_config_identity': next(iter(nuttx_identities), None),
              'cases': result_cases, 'baseline_deltas': baseline_deltas, 'matrix': matrix_summary}
    if {'c-packet-matched-2', 'zephyr-packet-2'} <= result_cases.keys():
        report['matched_packet_comparison'] = _paired_timing(
            result_cases['c-packet-matched-2'], result_cases['zephyr-packet-2'])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', required=True, type=Path)
    parser.add_argument('--matrix', required=True, action='append', type=Path)
    parser.add_argument('--readelf', required=True, type=Path)
    parser.add_argument('--nuttx-compiler', type=Path,
                        help='optional pinned NuttX compiler executable for --version identity')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error('--out must be a fresh JSON path')
    if not args.readelf.is_file() or not os.access(args.readelf, os.X_OK):
        parser.error('--readelf must name an executable file')
    if args.nuttx_compiler and (not args.nuttx_compiler.is_file() or
                                not os.access(args.nuttx_compiler, os.X_OK)):
        parser.error('--nuttx-compiler must name an executable file')
    if not args.artifacts.is_dir():
        parser.error('--artifacts must be a directory')
    try:
        report = build_report(args.artifacts, args.matrix, args.readelf, args.nuttx_compiler)
        with args.out.open('x') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f'comparison report failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
