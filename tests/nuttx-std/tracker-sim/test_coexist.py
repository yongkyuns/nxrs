import contextlib
import io
import unittest

from coexist import STACK_TASKS, check_stack_headroom


def snapshot(used):
    names = [name for name, count in STACK_TASKS.items() for _ in range(count)]
    rows = [f'{pid} 1000 {used if name == "motion_tracker" else 700} {name}'
            for pid, name in enumerate(names, 1)]
    return ('\n'.join(['PID   SIZE   USED   THREAD NAME', *rows]) + '\n').encode()


class StackHeadroomTests(unittest.TestCase):
    def test_headroom_boundaries_use_the_highest_recorded_usage(self):
        for used, status in [(800, 'PASS'), (801, 'WARN'), (850, 'WARN')]:
            with self.subTest(used=used), contextlib.redirect_stdout(io.StringIO()) as output:
                check_stack_headroom(snapshot(used) + snapshot(700))
                self.assertIn(f'{status}: motion_tracker', output.getvalue())

        with self.assertRaisesRegex(AssertionError, 'below the 15% stack headroom floor'):
            check_stack_headroom(snapshot(851) + snapshot(700))

    def test_missing_imu_cannot_pass(self):
        incomplete = snapshot(800).replace(b'5 1000 700 imu-acquire\n', b'')
        with self.assertRaisesRegex(AssertionError, 'never observed all tracker tasks'):
            check_stack_headroom(incomplete)
