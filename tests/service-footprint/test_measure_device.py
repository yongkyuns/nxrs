"""Parser checks for the physical NSH heap probe."""
import unittest

from measure_device import (count_output_lines, flash_command, parse_free,
                                  verify_silent_exit)


class FreeOutputTest(unittest.TestCase):
    def test_flash_command_uses_pinned_esptool_v4_syntax(self):
        self.assertEqual(
            flash_command('esptool.py', '/dev/ttyUSB0', 'firmware.bin'),
            ['esptool.py', '--chip', 'esp32s3', '--port', '/dev/ttyUSB0',
             '--baud', '460800', 'write_flash', '--flash_size', 'detect',
             '0x0', 'firmware.bin'],
        )

    def test_requires_one_complete_expected_line(self):
        transcript = (b'nsh> cq_probe\r\nSERVICE_SLICE_PASS observed=3\r\n'
                      b'nsh> ')
        self.assertEqual(count_output_lines(transcript, 'SERVICE_SLICE_PASS observed=3'), 1)
        self.assertEqual(count_output_lines(transcript, 'SERVICE_SLICE_PASS'), 0)
        self.assertEqual(count_output_lines(transcript, 'SERVICE_SLICE_PASS ', prefix=True), 1)
        self.assertEqual(count_output_lines(transcript + transcript,
                                            'SERVICE_SLICE_PASS ', prefix=True), 2)

    def test_parses_only_umem_row(self):
        output = (b'free\r\n total used free maxused maxfree nused nfree name\r\n'
                  b' 375788 6900 368888 7256 368888 24 1 Umem\r\nnsh> ')
        self.assertEqual(parse_free(output), {
            'total': 375788, 'used': 6900, 'free': 368888, 'maxused': 7256,
            'maxfree': 368888, 'nused': 24, 'nfree': 1,
        })

    def test_rejects_missing_or_duplicate_rows(self):
        row = b' 375788 6900 368888 7256 368888 24 1 Umem\n'
        with self.assertRaises(ValueError):
            parse_free(b'nsh> ')
        with self.assertRaises(ValueError):
            parse_free(row + row)

    def test_silent_exit_checks_both_output_and_return_status(self):
        app = b'\x1b[Kif cq_led_prod\r\nnsh> '
        passed = b'then echo NXRS_SILENT_EXIT_0\r\nNXRS_SILENT_EXIT_0\r\nnsh> '
        skipped = b'else echo NXRS_SILENT_EXIT_NONZERO\r\nnsh> '
        verify_silent_exit(app, passed, skipped, 'cq_led_prod')
        verify_silent_exit(b'Kif cq_led_prod\r\nnsh> ', passed, skipped,
                           'cq_led_prod')
        with self.assertRaises(RuntimeError):
            verify_silent_exit(app.replace(b'nsh>', b'unexpected\r\nnsh>'),
                               passed, skipped, 'cq_led_prod')
        with self.assertRaises(RuntimeError):
            verify_silent_exit(app, b'then echo NXRS_SILENT_EXIT_0\r\nnsh> ',
                               b'NXRS_SILENT_EXIT_NONZERO\r\nnsh> ', 'cq_led_prod')

    def test_silent_exit_accepts_split_trailing_prompt_escape_only(self):
        app = b'if cq_led_prod\r\nnsh> '
        passed = b'then echo NXRS_SILENT_EXIT_0\r\nNXRS_SILENT_EXIT_0\r\nnsh> '
        skipped = b'else echo NXRS_SILENT_EXIT_NONZERO\r\nnsh> '
        for suffix in (b'\x1b', b'\x1b[', b'\x1b[0;', b'\x1b[K'):
            with self.subTest(suffix=suffix):
                verify_silent_exit(app + suffix, passed, skipped, 'cq_led_prod')
        for unexpected in (b'warning\r\nnsh> \x1b', b'\x1bX', b'\x1b[diagnostic'):
            with self.subTest(unexpected=unexpected), self.assertRaises(RuntimeError):
                verify_silent_exit(app + unexpected, passed, skipped, 'cq_led_prod')


if __name__ == '__main__':
    unittest.main()
