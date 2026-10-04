import unittest
from transport_resource_report import FIELDS, message_ledger, nominal_allocation, parse_layout

class ResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.values = [12, 259, 27, 39, 8, 4, 8, 16, 104, 40, 24, 24, 8, 8, 264, 1]
        self.layout = dict(zip(FIELDS, self.values))

    def test_extracts_exact_target_constants_and_rejects_missing_words(self):
        text = 'nxrs_transport_resource_layout:\n' + ''.join(f'\t.word\t{x}\n' for x in self.values)
        self.assertEqual(parse_layout(text), self.layout)
        for invalid in (text.replace('.word\t12', '.word\tinvalid'), text.replace('nxrs_transport', 'other')):
            with self.assertRaises(ValueError): parse_layout(invalid)

    def test_heap_rounding_and_active_call_allowance_are_not_just_queue_slots(self):
        ledger = message_ledger(self.layout)
        self.assertEqual(ledger['16']['queued_slots_nominal'], 13800)
        self.assertEqual(ledger['28']['queued_slots_nominal'], 14760)
        self.assertEqual(ledger['16']['slots_plus_active_nominal'], 18848)
        self.assertEqual(ledger['28']['slots_plus_active_nominal'], 19824)
        self.assertEqual(ledger['queue_metadata_nominal'], 6720)
        self.assertEqual(ledger['posix_fixed_pool_bss'], 4160)
        self.assertEqual(ledger['sysv_fixed_pool_bss'], 2112)
        self.assertEqual(ledger['fixed_pool_bss'], 6272)
        self.assertEqual(nominal_allocation(1, self.layout), 16)
        self.layout['allocator_alignment'] = 3
        with self.assertRaises(ValueError): nominal_allocation(1, self.layout)

if __name__ == '__main__': unittest.main()
