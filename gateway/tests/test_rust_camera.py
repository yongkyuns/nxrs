import unittest
from rust_camera import validate_snapshot


class SourceAccountingTests(unittest.TestCase):
    def test_busy_retries_are_not_concurrent_admissions(self):
        counters = dict(active=1, peak=1, connections=3, disconnected=2, frames=80, busy=2)
        self.assertEqual(validate_snapshot(counters), counters)
        closed = {**counters, 'active': 0, 'disconnected': 3}
        self.assertEqual(validate_snapshot(closed), closed)

    def test_overlapping_or_inconsistent_admission_is_rejected(self):
        valid = dict(active=1, peak=1, connections=3, disconnected=2, frames=80, busy=0)
        for changed in [{'active': 2}, {'peak': 2}, {'disconnected': 1}, {'disconnected': 4},
                        {'busy': -1}, {'active': True}, {'connections': '3'}]:
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                validate_snapshot({**valid, **changed})


if __name__ == '__main__':
    unittest.main()
