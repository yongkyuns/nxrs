"""Oracle boundaries for defined source results, including masked poison."""
import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    'zero_select', Path(__file__).parent / 'compiler/check_zero_select.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ZeroSelectOracleTests(unittest.TestCase):
    def test_signed_overflow_is_testable_only_when_unselected(self):
        self.assertEqual(probe.add_predicate_result(0, 0x7fffffff), 0)
        self.assertIsNone(probe.add_predicate_result(1, 0x7fffffff))
        self.assertEqual(probe.add_predicate_result(17, 0x7ffffffe), 17)
        self.assertEqual(probe.add_predicate_result(17, 0x80000000), 0)
        self.assertEqual(probe.add_predicate_result(17, 0xffffffff), 17)

    def test_out_of_range_shift_is_testable_only_when_unselected(self):
        for amount in (32, 64, 255, 0xffffffff):
            self.assertEqual(probe.shift_predicate_result(0, 1, amount), 0)
            self.assertIsNone(probe.shift_predicate_result(17, 1, amount))
        self.assertEqual(probe.shift_predicate_result(17, 1, 31), 0)
        self.assertEqual(probe.shift_predicate_result(17, 1, 30), 17)
        self.assertEqual(probe.shift_predicate_result(17, 0x80000000, 0), 0)
        self.assertEqual(probe.shift_predicate_result(17, 0x80000000, 1), 17)


if __name__ == '__main__':
    unittest.main()
