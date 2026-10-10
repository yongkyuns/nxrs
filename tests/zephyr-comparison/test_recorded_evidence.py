"""Regression checks for the publishable, sanitized device evidence."""
import json
from pathlib import Path
import unittest


RESULTS = Path(__file__).with_name('results') / 'esp32s3-2026-10-03.json'


class RecordedResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RESULTS.read_text()
        cls.result = json.loads(cls.text)

    def test_complete_raw_evidence_and_restored_matrices(self):
        cases = self.result['cases']
        self.assertEqual(len(cases), 15)
        self.assertEqual(sum(len(case['runs']) for case in cases.values()), 326)
        self.assertEqual(sum(len(case['runs']) for case in cases.values()
                             if case['mode'] != 'baseline'), 320)
        for case in cases.values():
            self.assertEqual(case['completed_runs'], len(case['runs']))
            self.assertEqual(sum(case['runs_per_matrix'].values()), len(case['runs']))
            if case['mode'] == 'baseline':
                continue
            for row in case['runs']:
                self.assertEqual(row['scale']['messages'], 5760)
                self.assertEqual(row['scale']['digest'], 441445568)
        self.assertTrue(all(block['restored'] for block in self.result['matrix']))

    def test_pooled_and_matched_counts_and_resource_scope(self):
        cases = self.result['cases']
        zephyr = cases['zephyr-packet-2']
        nuttx = cases['c-packet-matched-2']
        self.assertEqual(zephyr['runs_per_matrix'],
                         {'packet-matrix': 30, 'strict-packet-matrix': 20})
        self.assertEqual(nuttx['completed_runs'], 20)
        paired = self.result['matched_packet_comparison']
        self.assertEqual(paired['runs_per_case'], 20)
        self.assertEqual(paired['matrices'], ['strict-packet-matrix'])
        self.assertEqual(paired['zephyr_c']['primary_elapsed_us']['median'],
                         zephyr['timing']['primary_elapsed_us']['median'])
        for name, flash, ram in (('c-packet-matched-2', 174616, 206376),
                                 ('zephyr-packet-2', 86639, 186840)):
            ledger = cases[name]['resource_ledger']
            self.assertEqual(ledger['loadbearing_flash_bytes'], flash)
            self.assertEqual(ledger['whole_ram_footprint_bytes'], ram)

    def test_public_identity_separates_firmware_and_historical_tools(self):
        self.assertNotRegex(self.text, r'/Users/|/home/|/dev/(?:cu|tty)|\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b')
        for case in self.result['cases'].values():
            if case['os'] != 'zephyr':
                continue
            identity = case['identity']
            self.assertFalse(any(name.endswith('.py') for name in identity['sources_sha256']))
            proof = identity['source_verification']
            self.assertRegex(proof['historical_non_firmware']['manifest_sha256'], r'^[0-9a-f]{64}$')
            if case['mode'] != 'baseline':
                self.assertTrue(proof['generated_control']['matches_frozen'])
            self.assertIn('tests/zephyr-comparison/build.py',
                          proof['current_tools']['source_sha256'])


if __name__ == '__main__':
    unittest.main()
