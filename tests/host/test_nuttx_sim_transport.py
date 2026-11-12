"""Command transport checks, independent of target timing or execution claims."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import call, patch

SPEC = importlib.util.spec_from_file_location('nuttx_sim_transport', Path(__file__).with_name('test-nuttx-sim.py'))
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


class CommandTransportTests(unittest.TestCase):
    def test_qemu_settles_and_paces_every_command_byte(self):
        events = []
        def write(fd, data):
            self.assertEqual(fd, 7)
            events.append(('write', data))
            return len(data)
        with patch.object(HARNESS.os, 'write', side_effect=write), \
                patch.object(HARNESS.time, 'sleep', side_effect=lambda delay: events.append(('sleep', delay))):
            HARNESS.write_command(7, 'nxrs_sim fail', paced=True)
        expected = [('sleep', 0.02)]
        for byte in b'nxrs_sim fail\n':
            expected += [('write', bytes((byte,))), ('sleep', 0.01)]
        self.assertEqual(events, expected)

    def test_simulator_bulk_input_completes_short_writes_without_pacing(self):
        with patch.object(HARNESS.os, 'write', side_effect=[2, 4]) as write, \
                patch.object(HARNESS.time, 'sleep') as sleep:
            HARNESS.write_command(7, 'uname')
        self.assertEqual(write.call_args_list, [call(7, b'uname\n'), call(7, b'ame\n')])
        sleep.assert_not_called()

    def test_zero_progress_fails_instead_of_losing_or_repeating_a_command(self):
        for paced in (False, True):
            with self.subTest(paced=paced), patch.object(HARNESS.os, 'write', return_value=0) as write, \
                    patch.object(HARNESS.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'no progress'):
                HARNESS.write_command(7, 'nxrs_sim fail', paced=paced)
            self.assertEqual(write.call_count, 1)


if __name__ == '__main__':
    unittest.main()
