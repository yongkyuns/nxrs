import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('comparison_report', HERE / 'report.py')
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def readelf_sections():
    return ('There are 5 section headers:\n'
            '  [ 1] .flash.text PROGBITS 0000000042000000 000040 000064 00 AX 0 0 4\n'
            '  [ 2] .dram0.dummy PROGBITS 000000003fc80000 0000a4 000020 00 WA 0 0 4\n'
            '  [ 3] .dram0.bss NOBITS 000000003fc80020 0000c4 000040 00 WA 0 0 4\n'
            '  [ 4] .iram0.text PROGBITS 0000000040370000 0000c4 000080 00 AX 0 0 4\n'
            '  [ 5] .rtc.data PROGBITS 0000000050000000 000144 000010 00 WA 0 0 4\n')


def n_run(index):
    return {'before': {'used': 100, 'maxused': 130},
            'after': {'used': 100 + (168 if index == 0 else 0), 'maxused': 140064},
            'used_delta': 168 if index == 0 else 0,
            'high_water_delta': 1, 'elapsed_ms': 25}


def n_transcript(runs):
    lines = []
    for _ in range(runs):
        lines.extend((
            'CQ_TRANSPORT_PASS language=c mode=large cycles=2400000 event_bytes=248 ack_bytes=28',
            'CQ_C_SCALE_PASS mode=large queues=60 logical_streams=60 threads=20 messages=5760 '
            'digest=441445568 elapsed_us=100 rx_p50_us=10 rx_p99_us=20 rx_max_us=30 ack_p99_us=25 '
            'heap_before=100 heap_queues=200 heap_workers=300 heap_producers=400 heap_after_join=150 '
            'stack_main=100 stack_producer_max=90 stack_worker_max=80 stack_collector=70',
            'CQ_C_WAKE_PASS trials=64 queues=3 threads=2 sleep_clock_us=100 rx_p50_us=10 '
            'rx_p99_us=20 rx_max_us=30 ack_p99_us=25'))
    return '\n'.join(lines) + '\n'


def z_lines():
    scale_metrics = (' elapsed_us=100 rx_p50_us=10 rx_p99_us=20 rx_max_us=30 ack_p99_us=25'
                     ' digest=441445568 stack_main=100 stack_producer_max=90'
                     ' stack_worker_max=80 stack_collector=70 heap_before=100'
                     ' heap_queues=200 heap_workers=300 heap_producers=400 heap_after_join=150')
    return [
        'CQ_TRANSPORT_PASS language=zephyr mode=large cycles=2400000 event_bytes=248 ack_bytes=28',
        'CQ_ZEPHYR_SCALE_PASS mode=large queues=60 logical_streams=60 threads=20 messages=5760' + scale_metrics,
        'ZEPHYR_RESOURCES queue_buffers=12840 queue_objects=3420 thread_objects=4480 stack_storage=83968 entry_storage=240 heap_allocated=0',
        'ZEPHYR_MEMORY kernel_heap_reserved=4096 kernel_heap_used=100 kernel_heap_peak=200 kernel_heap_free=3896',
        'ZEPHYR_COMMAND_EXIT status=0',
    ]


def z_saved():
    return report.measure.validate(('\n'.join(z_lines()) + '\n').encode(), 'large', 'packet')


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / 'artifacts'
        self.readelf = self.root / 'readelf'
        self.readelf.touch()
        self.n_case = 'c-packet-2'
        self.n_build = self.artifacts / 'nuttx-matched' / 'transport-c-packet-2-v1'
        self._n_build()

    def tearDown(self):
        self.temporary.cleanup()

    def _n_build(self):
        self.n_build.mkdir(parents=True)
        config = ('CONFIG_ARCH_CHIP="esp32s3"\nCONFIG_ARCH_CHIP_ESP32S3=y\n'
                  'CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ=240\nCONFIG_ESP32S3_FLASH_16M=y\n'
            'CONFIG_ESPRESSIF_FLASH_MODE_DIO=y\nCONFIG_ESPRESSIF_FLASH_FREQ="40m"\n'
            'CONFIG_ESPRESSIF_FLASH_FREQ_40M=y\n'
                  'CONFIG_ESP32S3_PSRAM_8M=y\nCONFIG_ESP32S3_SPIRAM_BOOT_INIT=y\n'
                  'CONFIG_ESP32S3_SPIRAM_COMMON_HEAP=y\nCONFIG_MM_REGIONS=1\n'
                  'CONFIG_ARCH_INTERRUPTSTACK=4096\nCONFIG_USEC_PER_TICK=10000\n'
                  'CONFIG_RR_INTERVAL=10\n# CONFIG_SMP is not set\n')
        for name, content in (('c.elf', b'elf'), ('c.merged.bin', b'merged'),
                              ('c.unpadded.bin', b'unpadded'), ('resolved.config', config.encode())):
            (self.n_build / name).write_bytes(content)
        artifacts = {name: digest(self.n_build / name) for name in
                     ('c.elf', 'c.merged.bin', 'c.unpadded.bin', 'resolved.config')}
        write_json(self.n_build / 'c-build-provenance.json', {
            'config_identity': report.nuttx_build.config_identity(self.n_build / 'resolved.config'),
            'command': 'cq_c_scale', 'command_stack': 8192,
            'c_defines': ['NXRS_CQ_DEVICE', 'NXRS_CQ_SYNCHRONIZED', 'NXRS_CQ_PACKET_SERVICE',
                          'NXRS_CQ_SPEED', 'NXRS_PAYLOAD_C_SPEED'],
            'make_command': ['make', 'CROSSDEV=xtensa-esp32s3-elf-'], 'source_sha256': {'/tmp/app.c': 'd' * 64},
            'artifacts': artifacts,
        })

    def _matrix(self, name, case, *, runs=2, blocks=1, record_overrides=None):
        directory = self.root / name
        directory.mkdir()
        record = {'schema': 1, 'runs_per_block': runs, 'blocks': blocks,
                  'order': [{'case': case, 'block': block} for block in range(blocks)],
                  'failure': None, 'restore_verified': True}
        record.update(record_overrides or {})
        write_json(directory / 'matrix.json', record)
        for block in range(blocks):
            measurement_dir = directory / f'{case}-block-{block}'
            measurement_dir.mkdir()
            rows = [n_run(index) for index in range(runs)]
            write_json(measurement_dir / 'measurement.json', {
                'schema': 1, 'image_sha256': digest(self.n_build / 'c.merged.bin'),
                'command': 'cq_c_scale large', 'flashed': True,
                'requested_runs': runs, 'completed_runs': runs, 'failure': None, 'runs': rows,
            })
            (measurement_dir / 'serial.log').write_text(n_transcript(runs))
        return directory

    def _z_case(self):
        directory = self.artifacts / 'zephyr-matched-packet-2'
        (directory / 'zephyr').mkdir(parents=True)
        config = ('CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC=240000000\nCONFIG_MAIN_STACK_SIZE=8192\n'
                  'CONFIG_ISR_STACK_SIZE=4096\nCONFIG_HEAP_MEM_POOL_ADD_SIZE_BOARD=4096\n'
                  'CONFIG_TIMESLICING=y\nCONFIG_TIMESLICE_SIZE=10\nCONFIG_TIMESLICE_PRIORITY=0\n'
                  'CONFIG_ESPTOOLPY_FLASHMODE_DIO=y\nCONFIG_ESPTOOLPY_FLASHFREQ_40M=y\n'
                  'CONFIG_ASSERT=y\nCONFIG_INIT_STACKS=y\nCONFIG_STACK_SENTINEL=y\n'
                  'CONFIG_THREAD_STACK_INFO=y\nCONFIG_THREAD_MONITOR=y\n')
        for name, content in ((directory / 'zephyr.bin', b'zbin'),
                              (directory / 'zephyr.map', b'zmap'),
                              (directory / 'resolved.config', config.encode()),
                              (directory / 'zephyr' / 'zephyr.elf', b'zelf')):
            name.write_bytes(content)
        write_json(directory / 'build-provenance.json', {
            'status': 'success', 'failure': None, 'mode': 'packet', 'profile': 'speed',
            'zephyr_bin_sha256': digest(directory / 'zephyr.bin'),
            'elf_sha256': digest(directory / 'zephyr' / 'zephyr.elf'),
            'map_sha256': digest(directory / 'zephyr.map'),
            'resolved_config_sha256': digest(directory / 'resolved.config'),
            'revisions': {'zephyr': 'a' * 40, 'hal_espressif': 'b' * 40, 'hal_xtensa': 'c' * 40},
            'readelf': '/sdk/bin/readelf', 'compiler': {'path': '/sdk/bin/gcc', 'version': 'gcc 12.2'},
            'configuration': {'flash_frequency_mhz': 40,
                              'cache': {'instruction_kib': 16, 'data_kib': 32},
                              'timing': {'zephyr_timeslice_ms': 10},
                              'heap': {'board_added_kernel_heap_bytes': 4096}},
        })
        write_json(directory / 'source-hashes.json', report.build.source_hashes(HERE, 'packet'))
        generator = report._load('test_report_generator', HERE / 'generate_control.py')
        (directory / 'matched_control.c').write_text(
            generator.adapt((report.SERVICE / 'channel_scale_mq.c').read_text()))
        return directory

    def _z_matrix(self, runs=1, primer=False):
        directory = self.root / 'zmatrix'
        directory.mkdir()
        write_json(directory / 'matrix.json', {
            'runs_per_block': runs, 'blocks': 1,
            'order': [{'case': 'zephyr-packet-2', 'block': 0}],
            'failure': None, 'restore_verified': True,
        })
        measure_dir = directory / 'zephyr-packet-2-block-0'
        measure_dir.mkdir()
        row = z_saved()
        write_json(measure_dir / 'measurement.json', {
            'mode': 'packet', 'command': 'large', 'requested_runs': runs,
            'completed_runs': runs, 'failure': None, 'image_sha256': digest(self.artifacts / 'zephyr-matched-packet-2' / 'zephyr.bin'),
            'runs': [row for _ in range(runs)],
        })
        transcript = 'zephyr> '
        if primer:
            transcript += ('CQ_ZEPHYR_THREADS_PASS threads=20\n' + '\n'.join(z_lines()[2:]) +
                           '\nzephyr> ')
        transcript += ('\n'.join(z_lines()) + '\nzephyr> ') * runs
        (measure_dir / 'serial.log').write_text(transcript)
        return directory

    def test_aggregates_raw_runs_and_counts_all_elf_ram_regions(self):
        first = self._matrix('matrix-a', self.n_case)
        second = self._matrix('matrix-b', self.n_case)
        with patch.object(report.subprocess, 'check_output', return_value=readelf_sections()):
            result = report.build_report(self.artifacts, [first, second], self.readelf)
        case = result['cases'][self.n_case]
        self.assertEqual(len(case['runs']), 4)
        self.assertEqual(case['completed_runs'], 4)
        self.assertEqual(case['runs_per_matrix'], {'matrix-a': 2, 'matrix-b': 2})
        self.assertEqual(case['timing_by_matrix']['matrix-a']['completed_runs'], 2)
        self.assertEqual(case['sizes']['unpadded_bin_bytes'], len(b'unpadded'))
        self.assertEqual(case['resource_ledger']['loadbearing_flash_bytes'], 100 + 128 + 16)
        self.assertEqual(case['resource_ledger']['resident_ram_bytes'], 64 + 128 + 16)
        self.assertEqual(case['resource_ledger']['whole_ram_footprint_bytes'], 64 + 128 + 16 + 140064)
        self.assertEqual(case['resource_ledger']['excluded_dummy_padding'], [{'name': '.dram0.dummy', 'size': 32}])
        self.assertEqual(case['timing']['primary_elapsed_us']['median'], 10000)
        self.assertTrue(all(row['restored'] for row in result['matrix']))

    def test_zephyr_static_ram_does_not_add_kernel_heap_twice(self):
        self._z_case()
        matrix = self._z_matrix(runs=2, primer=True)
        with patch.object(report.subprocess, 'check_output', return_value=readelf_sections()):
            result = report.build_report(self.artifacts, [matrix], self.readelf)
        case = result['cases']['zephyr-packet-2']
        self.assertEqual(len(case['runs']), 2)
        self.assertEqual(case['resource_ledger']['kernel_heap_reserved_bytes'], 4096)
        self.assertEqual(case['resource_ledger']['whole_ram_footprint_bytes'], 64 + 128 + 16)
        self.assertEqual(case['resource_ledger']['whole_ram_footprint_bytes'],
                         case['resource_ledger']['resident_ram_bytes'])
        self.assertTrue(case['identity']['source_verification']['generated_control']['matches_frozen'])
        self.assertNotIn('tests/zephyr-comparison/build.py', case['identity']['sources_sha256'])

    def test_report_rejects_changed_firmware_input_and_generated_source(self):
        directory = self._z_case()
        matrix = self._z_matrix()
        manifest = json.loads((directory / 'source-hashes.json').read_text())
        manifest['main.c'] = '0' * 64
        write_json(directory / 'source-hashes.json', manifest)
        with patch.object(report.subprocess, 'check_output', return_value=readelf_sections()):
            with self.assertRaisesRegex(ValueError, 'modified firmware inputs: main.c'):
                report.build_report(self.artifacts, [matrix], self.readelf)
            write_json(directory / 'source-hashes.json', report.build.source_hashes(HERE, 'packet'))
            (directory / 'matched_control.c').write_text('modified generated firmware')
            with self.assertRaisesRegex(ValueError, 'generated matched_control.c differs'):
                report.build_report(self.artifacts, [matrix], self.readelf)

    def test_rejects_changed_images_incomplete_runs_and_bad_matrices(self):
        matrix = self._matrix('matrix', self.n_case)
        measure_path = matrix / f'{self.n_case}-block-0' / 'measurement.json'
        data = json.loads(measure_path.read_text())
        data['image_sha256'] = '0' * 64
        write_json(measure_path, data)
        with patch.object(report.subprocess, 'check_output', return_value=readelf_sections()):
            with self.assertRaisesRegex(ValueError, 'image hash mismatch'):
                report.build_report(self.artifacts, [matrix], self.readelf)

        incomplete = self._matrix('incomplete', self.n_case)
        data = json.loads((incomplete / f'{self.n_case}-block-0' / 'measurement.json').read_text())
        data['completed_runs'] = 1
        write_json(incomplete / f'{self.n_case}-block-0' / 'measurement.json', data)
        with patch.object(report.subprocess, 'check_output', return_value=readelf_sections()):
            with self.assertRaisesRegex(ValueError, 'incomplete measurement'):
                report.build_report(self.artifacts, [incomplete], self.readelf)

        for overrides, message in (({'failure': 'flash failure'}, 'successful and restored'),
                                   ({'restore_verified': False}, 'successful and restored'),
                                   ({'order': []}, 'missing one or more blocks')):
            bad = self._matrix('bad-' + str(len(list(self.root.iterdir()))), self.n_case,
                               record_overrides=overrides)
            with self.assertRaisesRegex(ValueError, message):
                report._matrix_records([bad])

    def test_matrix_directories_can_have_different_cases_and_run_counts(self):
        a = self._matrix('packet-matrix', 'c-packet-2', runs=10, blocks=3)
        b = self._matrix('wire-matrix', 'zephyr-packet-2', runs=20, blocks=1)
        c = self._matrix('baseline-matrix', 'nuttx-baseline', runs=3, blocks=1)
        matrices, cases = report._matrix_records([a, b, c])
        self.assertEqual(cases, {'c-packet-2', 'zephyr-packet-2', 'nuttx-baseline'})
        self.assertEqual([item['runs_per_block'] for item in matrices], [10, 20, 3])
        self.assertEqual([item['blocks'] for item in matrices], [3, 1, 1])

    def test_matched_c_packet_control_accepts_scale_without_wake_marker(self):
        transcript = '\n'.join((
            'CQ_TRANSPORT_PASS language=c mode=large cycles=2400000 event_bytes=248 ack_bytes=28',
            'CQ_C_SCALE_PASS mode=large queues=60 logical_streams=60 threads=20 messages=5760 '
            'digest=441445568 elapsed_us=100 rx_p50_us=10 rx_p99_us=20 rx_max_us=30 ack_p99_us=25 '
            'heap_before=100 heap_queues=200 heap_workers=300 heap_producers=400 heap_after_join=150 '
            'stack_main=100 stack_producer_max=90 stack_worker_max=80 stack_collector=70',
        ))
        rows = report._validate_transport_without_wake(transcript, 'c', 1, 28)
        self.assertEqual(rows[0]['cycles'], 2400000)
        with self.assertRaisesRegex(ValueError, 'cycle clock'):
            report._validate_transport_without_wake(transcript.replace('cycles=2400000',
                                                                        'cycles=3840000000'), 'c', 1, 28)

    def test_paired_timing_excludes_other_matrices_and_requires_equal_counts(self):
        def row(matrix, run, cycles):
            return {'matrix': matrix, 'matrix_block': 0, 'run': run,
                    'transport': {'cycles': cycles},
                    'scale': {'rx_p99_us': 10, 'elapsed_us': cycles // 240}}
        left = {'runs': [row('matched', 0, 24000), row('matched', 1, 48000)]}
        right = {'runs': [row('older', 0, 999999), row('matched', 0, 12000),
                          row('matched', 1, 24000)]}
        paired = report._paired_timing(left, right)
        self.assertEqual(paired['runs_per_case'], 2)
        self.assertEqual(paired['matrices'], ['matched'])
        self.assertEqual(paired['nuttx_c']['primary_elapsed_us']['median'], 150)
        self.assertEqual(paired['zephyr_c']['primary_elapsed_us']['median'], 75)
        with self.assertRaisesRegex(ValueError, 'counts differ'):
            report._paired_timing(left, {'runs': right['runs'][:2]})
        with self.assertRaisesRegex(ValueError, 'no shared'):
            report._paired_timing(left, {'runs': right['runs'][:1]})


if __name__ == '__main__':
    unittest.main()
