import unittest
from rust_camera import validate_snapshot, validate_viewer_sharing


class SourceAccountingTests(unittest.TestCase):
    def test_viewers_share_established_source_after_startup_retries(self):
        before = dict(active=1, peak=1, connections=3, disconnected=2, frames=80, busy=2)
        validate_viewer_sharing(before, {**before, 'frames': 180})

    def test_viewer_phase_reconnect_busy_retry_or_overlap_is_rejected(self):
        before = dict(active=1, peak=1, connections=3, disconnected=2, frames=80, busy=2)
        for change in [{'connections': 4, 'disconnected': 3}, {'busy': 3}, {'peak': 2},
                       {'active': 0, 'disconnected': 3}]:
            with self.subTest(change=change), self.assertRaises(AssertionError):
                validate_viewer_sharing(before, {**before, **change})

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
