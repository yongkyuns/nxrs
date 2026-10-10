"""Pure regressions for shared ELF section helpers."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rtos_harness.images import (assert_stack_section, image_header, parse_sections,
                                 section_accounting)


class ImageSectionTests(unittest.TestCase):
    def test_parse_and_account_sections(self):
        output = '''
  [ 1] .text PROGBITS 40374000 001000 000100 00 AX 0 0 16
  [ 2] .bss NOBITS 3fc80000 001100 000020 00 WA 0 0 16
  [ 3] .dram0.dummy NOBITS 3fc81000 001120 000040 00 WA 0 0 16
  [ 4] .debug_info PROGBITS 00000000 001160 000010 00 0 0 1
'''
        sections = parse_sections(output)
        self.assertEqual(sections[0], dict(name='.text', type='PROGBITS',
                                           address=0x40374000, offset=0x1000,
                                           size=0x100, flags='AX'))
        accounted = section_accounting(sections)
        self.assertEqual(accounted['loadbearing_flash_bytes'], 0x100)
        self.assertEqual(accounted['resident_ram_bytes'], 0x120)
        self.assertEqual(accounted['excluded_dummy_padding'],
                         [dict(name='.dram0.dummy', size=0x40)])

    def test_rejects_unparseable_output(self):
        with self.assertRaisesRegex(ValueError, 'no parseable'):
            parse_sections('not readelf output')

    def test_image_header_and_stack_validators(self):
        self.assertEqual(image_header(bytes([0xe9, 0, 2, 0x40]))['flash_size_mb'], 16)
        self.assertEqual(assert_stack_section([dict(name='.stack', size=8192)]),
                         dict(name='.stack', size=8192))


if __name__ == '__main__':
    unittest.main()
