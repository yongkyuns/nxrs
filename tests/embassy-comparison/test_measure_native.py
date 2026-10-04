import unittest
from unittest.mock import patch, call
import measure_native


class NativeCommandTests(unittest.TestCase):
    def test_input_pacing_keeps_complete_command_and_existing_prompt_reader(self):
        with patch.object(measure_native.os, 'write', return_value=1) as write, \
                patch.object(measure_native.time, 'sleep') as pause, \
                patch.object(measure_native.native, 'read_prompt', return_value=b'nsh> ') as read:
            self.assertEqual(measure_native.paced_command(123, 'free', 10), b'nsh> ')
        self.assertEqual(write.call_args_list, [call(123, bytes([b])) for b in b'free\r'])
        self.assertEqual(pause.call_args_list, [call(0.005)] * 5)
        read.assert_called_once_with(123, 10)

    def test_short_write_is_not_silently_accepted_or_retried(self):
        with patch.object(measure_native.os, 'write', return_value=0), \
                patch.object(measure_native.native, 'read_prompt') as read:
            with self.assertRaises(OSError):
                measure_native.paced_command(123, 'free', 10)
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
