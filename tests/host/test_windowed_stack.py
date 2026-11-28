"""The linked stack guard must inspect code hidden by Xtensa property records."""
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'nuttx-std/tracker-sim'))
import check_windowed_stack as guard


TRACKER = '''42056c74 <nxrs_motion_tracker::runtime::track>:
42056c74: 186136 entry a1, 0xc30
42056c77: f01d retw.n
'''
CONSTRUCTOR = '''42043614 <triple_buffer::triple_buffer::<core::option::Option<Sample<GnssFix>>>>:
42043614: 074136 entry a1, 0x3a0
42043617: f83c movi.n a8, 63
42043619: 094c movi.n a9, 64
4204361b: 108180 and a8, a1, a8
4204361e: c08980 sub a8, a9, a8
42043621: 118a add.n a1, a1, a8
'''


class WindowedStack(unittest.TestCase):
    def test_rejects_constructor_realignment(self):
        with self.assertRaisesRegex(ValueError, 'triple_buffer.*changes a1 without MOVSP'):
            guard.check_windowed_stack(TRACKER + CONSTRUCTOR)

    def test_accepts_movsp_but_requires_tracker_entry(self):
        guard.check_windowed_stack(TRACKER.replace('f01d retw.n', '0018b0 movsp a1, a8'))
        with self.assertRaisesRegex(ValueError, 'windowed entry was absent'):
            guard.check_windowed_stack('')

    def test_property_records_cannot_hide_constructor_in_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / 'original.elf'
            elf.write_bytes(b'instructions and .xt.prop')
            copies = []

            def copy_without_properties(command):
                self.assertEqual(command[:2], ['xtensa-esp32s3-elf-objcopy',
                                               '--remove-section=.xt.prop'])
                self.assertEqual(command[2], elf)
                self.assertNotEqual(command[3], elf)
                copies.append(command[3])
                command[3].write_bytes(b'instructions')

            def disassemble(command, *, text):
                self.assertTrue(text)
                self.assertEqual(command[:3], ['xtensa-esp32s3-elf-objdump', '-d', '-C'])
                # Ordinary disassembly of the failed firmware hid the whole
                # constructor as hex words while still decoding runtime::track.
                if b'.xt.prop' in command[3].read_bytes():
                    return TRACKER + '42043614: 3c074136\n42043618: 80094cf8\n'
                return TRACKER + CONSTRUCTOR

            errors = StringIO()
            with patch.object(sys, 'argv', ['check_windowed_stack.py', str(elf)]), \
                    patch.object(guard.subprocess, 'check_call', copy_without_properties), \
                    patch.object(guard.subprocess, 'check_output', disassemble), \
                    redirect_stderr(errors), self.assertRaises(SystemExit) as result:
                guard.main()
            self.assertEqual(result.exception.code, 1)
            self.assertIn('changes a1 without MOVSP', errors.getvalue())
            self.assertEqual(elf.read_bytes(), b'instructions and .xt.prop')
            self.assertTrue(copies)
            self.assertFalse(copies[0].exists())


if __name__ == '__main__':
    unittest.main()
