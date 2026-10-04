"""Report evidence checks with synthetic artifacts and transcripts only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import measure as embassy_measure


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location('embassy_report_tests_module', HERE / 'report.py')
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


def marker_text(mode, count):
    rows = []
    for _ in range(count):
        if mode == 'baseline':
            rows.extend(('EMBASSY_BASELINE_PASS heap_allocated=0 shared_stack=8192',
                         'EMBASSY_COMMAND_EXIT status=0'))
        else:
            ack = 16 if mode == 'wire' else 28
            rows.extend((
                f'CQ_TRANSPORT_PASS language=embassy mode=large cycles=2400000 '
                f'event_bytes=248 ack_bytes={ack}',
                'CQ_EMBASSY_SCALE_PASS mode=large queues=60 logical_streams=60 tasks=20 '
                'messages=5760 digest=441445568 rx_p50_us=10 rx_p99_us=20 rx_max_us=30 '
                'ack_p50_us=10 ack_p99_us=20 ack_max_us=30 samples=720',
                f'EMBASSY_RESOURCES heap_allocated=0 queue_buffers={45 * 248 + 60 * ack} '
                'channel_storage=14400 shared_stack=8192 tasks=20 queues=60',
                'EMBASSY_COMMAND_EXIT status=0'))
    return ('\n'.join(rows) + '\n').encode()


def parsed_rows(mode, count):
    return [embassy_measure.validate(marker_text(mode, 1), mode) for _ in range(count)]


class EmbassyReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / 'embassy-packet-2-block-0'
        self.output.mkdir()
        self.image_hash = hashlib.sha256(b'frozen embassy image').hexdigest()
        self.provenance = {'mode': 'packet', 'artifacts': {'embassy.bin': self.image_hash}}
        self.record = {'failure': None, 'flashed': True, 'requested_runs': 2,
                       'completed_runs': 2, 'runs': parsed_rows('packet', 2),
                       'mode': 'packet', 'command': 'large', 'image_sha256': self.image_hash}
        (self.output / 'measurement.json').write_text(json.dumps(self.record))
        (self.output / 'serial.log').write_bytes(marker_text('packet', 2))
        self.matrix = {'cases': ['embassy-packet-2'], 'blocks': 1, 'runs_per_block': 2,
                       'directory': self.root, 'global_block_start': 0}

    def tearDown(self):
        self.temporary.cleanup()

    def _rows(self, *, mode='packet', matrix=None, provenance=None):
        return report.embassy_rows('embassy-packet-2', [matrix or self.matrix], self.root,
                                   provenance or self.provenance)

    def test_accepts_exact_json_runs_matching_raw_markers(self):
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['run'] for row in rows], [0, 1])
        self.assertTrue(all(row['transport']['cycles'] == 2400000 for row in rows))

    def test_rejects_changed_image_command_count_json_fields_and_fail_marker(self):
        changes = (
            ('image hash', lambda record: record.update(image_sha256='0' * 64), None),
            ('command', lambda record: record.update(command='baseline'), None),
            ('requested count', lambda record: record.update(requested_runs=3), None),
            ('completed count', lambda record: record.update(completed_runs=1), None),
            ('run row count', lambda record: record.update(runs=record['runs'][:1]), None),
            ('changed JSON field', lambda record: record['runs'][0]['transport'].update(cycles=1), None),
            ('FAIL transcript', lambda record: None, b'FAIL synthetic firmware failure\n'),
        )
        for label, change, extra_transcript in changes:
            with self.subTest(evidence=label):
                record = json.loads(json.dumps(self.record))
                change(record)
                (self.output / 'measurement.json').write_text(json.dumps(record))
                (self.output / 'serial.log').write_bytes(
                    marker_text('packet', 2) + (extra_transcript or b''))
                with self.assertRaises(ValueError):
                    self._rows()

    def test_baseline_report_has_no_heap_or_timing_claim(self):
        build_directory = self.root / 'embassy-baseline-2-v3'
        build_directory.mkdir()
        image = build_directory / 'embassy.bin'
        elf = build_directory / 'embassy.elf'
        image.write_bytes(b'frozen embassy image')
        elf.write_bytes(b'synthetic elf')
        provenance_path = build_directory / 'build-provenance.json'
        baseline_provenance = {
            'mode': 'baseline', 'status': 'success', 'failure': None,
            'source_sha256': {'synthetic': 'source'}, 'artifacts': {
                'embassy.bin': hashlib.sha256(image.read_bytes()).hexdigest(),
                'embassy.elf': hashlib.sha256(elf.read_bytes()).hexdigest()},
            'profile': 'release', 'cooperative_yield': False, 'configuration': {},
            'tool_versions': {}, 'section_accounting': {'resident_ram_bytes': 64, 'loadbearing_flash_bytes': 32},
            'image_header': {'magic': 0xe9},
        }
        provenance_path.write_text(json.dumps(baseline_provenance))
        baseline_dir = self.root / 'embassy-baseline-block-0'
        baseline_dir.mkdir()
        baseline_hash = baseline_provenance['artifacts']['embassy.bin']
        baseline_run = embassy_measure.validate(marker_text('baseline', 1), 'baseline')
        (baseline_dir / 'measurement.json').write_text(json.dumps({
            'failure': None, 'flashed': True, 'requested_runs': 1, 'completed_runs': 1,
            'runs': [baseline_run], 'mode': 'baseline', 'command': 'baseline',
            'image_sha256': baseline_hash}))
        (baseline_dir / 'serial.log').write_bytes(marker_text('baseline', 1))
        baseline_matrix = {'cases': ['embassy-baseline'], 'blocks': 1, 'runs_per_block': 1,
                           'directory': self.root, 'global_block_start': 0}
        accounting = {'resident_ram_bytes': 64, 'loadbearing_flash_bytes': 32}
        with patch.object(report.build, 'source_inventory', return_value=baseline_provenance['source_sha256']), \
                patch.object(report.build, 'parse_sections', return_value=[{'name': '.stack', 'size': 8192}]), \
                patch.object(report.build, 'assert_stack_section'), \
                patch.object(report.build, 'section_accounting', return_value=accounting), \
                patch.object(report.build, 'image_header', return_value={'magic': 0xe9}), \
                patch.object(report.subprocess, 'check_output', side_effect=['synthetic sections', '']):
            result = report.embassy_case('embassy-baseline', [baseline_matrix], self.root,
                                         'readelf', 'nm')
        self.assertEqual(result['mode'], 'baseline')
        self.assertEqual(result['identity']['profile'], 'release')
        self.assertIs(result['identity']['cooperative_yield'], False)
        self.assertEqual(result['resource_ledger']['peak_heap_bytes'], 0)
        self.assertEqual(result['resource_ledger']['whole_ram_footprint_bytes'], 64)
        self.assertNotIn('timing', result)
        self.assertNotIn('channel_storage_bytes', result['resource_ledger'])

    def test_native_compiler_identity_is_passed_as_path_and_version_tuple(self):
        baseline = {'os': 'nuttx', 'mode': 'baseline', 'resource_ledger': {}}
        with patch.object(report.legacy, '_matrix_records', return_value=([], {'nuttx-baseline'})), \
                patch.object(report.legacy, '_frozen_build', return_value={}) as frozen, \
                patch.object(report.legacy, '_case_report', return_value=baseline), \
                patch.object(report.subprocess, 'check_output', return_value='gcc 14.2.0\nother text\n'):
            report.build_report(self.root, [], 'readelf', 'nm', 'gcc')
        self.assertEqual(frozen.call_args.args[-1], ('gcc', 'gcc 14.2.0'))


if __name__ == '__main__':
    unittest.main()
