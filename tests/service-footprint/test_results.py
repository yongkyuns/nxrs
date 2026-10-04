"""Keep the published summary tied to its recorded units and accounting."""
import json
from pathlib import Path
import unittest


class RecordedResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((Path(__file__).parent /
                              'results/esp32s3-2026-10-03.json').read_text())

    def test_full_matrix_is_complete_and_preserves_the_same_kernel(self):
        self.assertEqual(len(self.data['cases']), 10)
        for case in self.data['cases'].values():
            self.assertEqual(case['config_identity'], self.data['matched_config_identity'])
            self.assertEqual(case['board']['completed_runs'], 20)
            self.assertFalse(any(case['board']['repeat_retained']))

    def test_packet_sizes_and_ram_totals_are_not_interchanged(self):
        c, rust = (self.data['cases'][name] for name in ('c-packet-2', 'rust-packet-2'))
        self.assertEqual(rust['linked_flash'] - c['linked_flash'], 20_018)
        self.assertEqual(rust['unpadded_bytes'] - c['unpadded_bytes'], 676)
        for row, total in ((c, 205_792), (rust, 209_528)):
            self.assertEqual(row['fixed_dram'] + row['fixed_noinit'] + row['iram_sections']
                             + row['board']['peak_heap'], total)
            self.assertEqual(row['section_ram_plus_peak_heap'], total)
        self.assertEqual(rust['above_idle_thread_heap'] - c['above_idle_thread_heap'], 2_120)

    def test_pair_samples_remain_grouped_by_command(self):
        for row in self.data['paired'].values():
            paired = row['paired']
            self.assertEqual(paired['runs'], 10)
            self.assertEqual(paired['pairs_per_command'], 4)
            self.assertEqual(len(paired['pairs']), 40)

    def test_public_record_has_no_local_paths_or_device_identifiers(self):
        serialized = json.dumps(self.data)
        for private in ('/Users/', '/home/', '/dev/', 'device-before.bin'):
            self.assertNotIn(private, serialized)


if __name__ == '__main__':
    unittest.main()
