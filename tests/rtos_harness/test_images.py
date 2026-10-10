"""Pure regressions for shared ESP32-S3 ELF and image helpers."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rtos_harness.images import (assert_stack_section, esp_idf_section_accounting,
                                 image_header, parse_sections, section_accounting)


class ImageSectionTests(unittest.TestCase):
    def test_wide_readelf_rows_and_section_accounting(self):
        output = """
  [ 1] .text             PROGBITS        0000000042001000 001000 000120 00  AX  0   0 16
  [ 2] .rodata           PROGBITS        000000003c001120 001120 000080 00   A  0   0  4
  [ 3] .iram0.text       PROGBITS        0000000040374000 002000 000040 00  AX  0   0 16
  [ 4] .dram0.data       PROGBITS        000000003fc80000 003000 000020 00  WA  0   0  4
  [ 5] .bss              NOBITS          000000003fc80020 003020 000100 00  WA  0   0 16
  [ 6] .noinit           NOBITS          000000003fc80120 003020 000010 00  WA  0   0  4
  [ 7] .heap              NOBITS          000000003fc80130 003020 001000 00  WA  0   0 16
  [ 8] .loader.text      PROGBITS        0000000040374100 003020 000010 00  AX  0   0 16
  [ 9] .loader.data      PROGBITS        000000003fc80200 003030 000008 00  WA  0   0  4
  [10] .sw_isr_table     PROGBITS        000000003fc80208 003038 000030 00  WA  0   0  4
  [11] .device_states    NOBITS          000000003fc80238 003068 000020 00  WA  0   0  4
  [12] .k_heap_area      NOBITS          000000003fc80258 003088 000080 00  WA  0   0 16
  [13] .dram0.dummy      NOBITS          000000003fc88000 003108 008130 00  WA  0   0 16
  [14] .flash.rodata_dummy PROGBITS      000000003c000000 004000 010000 00   A  0   0 16
  [15] .debug_info       PROGBITS        0000000000000000 014000 000400 00      0   0  1
"""
        sections = parse_sections(output)
        self.assertEqual(len(sections), 15)
        self.assertEqual(sections[0], dict(name='.text', type='PROGBITS',
                                           address=0x42001000, offset=0x1000,
                                           size=0x120, flags='AX'))
        result = section_accounting(sections)
        self.assertEqual(result['loadbearing_flash_bytes'],
                         0x120 + 0x80 + 0x40 + 0x20 + 0x10 + 0x8 + 0x30)
        self.assertEqual(result['resident_ram_bytes'],
                         0x40 + 0x20 + 0x100 + 0x10 + 0x10 + 0x8 + 0x30 + 0x20 + 0x80)
        self.assertIn('.loader.text', result['resident_ram_sections'])
        self.assertIn('.k_heap_area', result['resident_ram_sections'])
        self.assertNotIn('.heap', result['resident_ram_sections'])
        self.assertNotIn('.debug_info', result['flash_sections'])
        self.assertEqual([row['name'] for row in result['excluded_dummy_padding']],
                         ['.dram0.dummy', '.flash.rodata_dummy'])

    def test_address_based_residency_and_named_dummy_exclusions(self):
        sections = [
            dict(name='.text.z_dummy_thread_init', type='PROGBITS', address=0x40374000,
                 size=4, flags='AX'),
            dict(name='.drom0.dummy', type='PROGBITS', address=0x3C000000,
                 size=64, flags='A'),
            dict(name='.flash.text_dummy', type='PROGBITS', address=0x42000000,
                 size=128, flags='AX'),
            dict(name='.rtc_state', type='NOBITS', address=0x50000000,
                 size=8, flags='WA'),
            dict(name='.psram_state', type='NOBITS', address=0x3D000000,
                 size=16, flags='WA'),
        ]
        result = section_accounting(sections)
        self.assertEqual(result['loadbearing_flash_bytes'], 4)
        self.assertEqual(result['resident_ram_bytes'], 4 + 8 + 16)
        self.assertIn('.text.z_dummy_thread_init', result['flash_sections'])
        self.assertEqual({item['name'] for item in result['excluded_dummy_padding']},
                         {'.drom0.dummy', '.flash.text_dummy'})

    def test_esp_idf_alias_and_padding_accounting_preserves_inventory(self):
        sections = [
            dict(name='.text', type='PROGBITS', address=0x40374000,
                 offset=0x1000, size=0x100, flags='AX'),
            dict(name='.rwdata_dummy', type='PROGBITS', address=0x40374000,
                 offset=0x1100, size=0x100, flags='AX'),
            dict(name='.rotext_dummy', type='PROGBITS', address=0x42000000,
                 offset=0x1200, size=0x80, flags='A'),
            dict(name='.bss', type='NOBITS', address=0x3fc80000,
                 offset=0x1280, size=0x20, flags='WA'),
        ]
        result = esp_idf_section_accounting(sections)
        self.assertEqual(result['resident_ram_bytes'], 0x120)
        self.assertEqual(result['resident_ram_sections'], ['.text', '.bss'])
        self.assertEqual(result['loadbearing_flash_bytes'], 0x200)
        self.assertEqual({section['name'] for section in result['excluded_dummy_padding']},
                         {'.rotext_dummy'})
        self.assertEqual(result['excluded_iram_aliases'],
                         [{'name': '.rwdata_dummy', 'size': 0x100}])
        self.assertEqual(len(sections), 4)
        sections[1]['flags'] = 'WA'
        self.assertEqual(esp_idf_section_accounting(sections), result)

    def test_unparseable_readelf_output_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'no parseable'):
            parse_sections('readelf: not an ELF file')


class ImageValidationTests(unittest.TestCase):
    def test_header_reports_exact_flash_settings_and_rejects_invalid_values(self):
        self.assertEqual(image_header(bytes([0xe9, 0, 2, 0x40])), {
            'magic': 0xe9, 'flash_mode': 'DIO', 'flash_mode_value': 2,
            'flash_frequency_mhz': 40, 'flash_frequency_value': 0,
            'flash_size_mb': 16, 'flash_size_value': 4,
        })
        for image in (bytes([0, 0, 2, 0x40]), bytes([0xe9, 0, 3, 0x40]),
                      bytes([0xe9, 0, 2, 0x41]), bytes([0xe9, 0, 2, 0x30])):
            with self.subTest(image=image), self.assertRaises(ValueError):
                image_header(image)

    def test_stack_section_must_be_unique_and_exactly_eight_kib(self):
        section = dict(name='.stack', size=8192, address=0x3fcfe000)
        self.assertEqual(assert_stack_section([section]), section)
        for sections in ([dict(name='.stack', size=8191)], [], [section, section]):
            with self.subTest(sections=sections), self.assertRaisesRegex(ValueError, 'exactly 8192'):
                assert_stack_section(sections)


if __name__ == '__main__':
    unittest.main()
