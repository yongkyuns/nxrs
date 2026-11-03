"""Synthetic image controls; no test fixture is hardware evidence."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from inspect_esp_image import diagnose, inspect_image


def image():
    header = bytearray(24)
    header[0], header[1], header[12] = 0xe9, 1, 9
    data = header + struct.pack('<II', 0x3fc90000, 4) + b'ABCD'
    checksum = 0xef
    for value in b'ABCD':
        checksum ^= value
    data += bytes(15 - len(data) % 16) + bytes([checksum])
    end = len(data)
    padding = 65536 - end - 16
    data += struct.pack('<II', 0, padding) + bytes(padding)
    data += struct.pack('<II', 0x42010000, 16) + bytes(range(16))
    return bytes(data), end


def console(data, end):
    return ('ESP-ROM:esp32s3-test\nSHA-256 comparison failed:\nCalculated: '
            + hashlib.sha256(data[:end]).hexdigest() + '\nExpected: '
            + data[end:end + 32].hex() + '\nAttempting to boot anyway...\n')


class EspImageTests(unittest.TestCase):
    def test_expected_bytes_are_padding_header_not_digest(self):
        data, end = image()
        report = inspect_image(data, console(data, end))
        self.assertTrue(report['rom_warning_matches_image_layout'])
        self.assertEqual(report['ram_checksum_end_offset'], end)
        self.assertEqual(report['hidden_segments'][0]['kind'], 'padding')
        self.assertFalse(report['boot_warning_resolved'])
        self.assertFalse(report['physical_timing_qualified'])

    def test_erased_flash_tail_and_no_warning_are_explicit(self):
        data, _ = image()
        report = inspect_image(data + b'\xff' * 64, 'ESP-ROM:esp32s3-test\n')
        self.assertFalse(report['rom_warning_observed'])
        self.assertFalse(report['rom_warning_matches_image_layout'])
        self.assertFalse(report['boot_warning_resolved'])

    def test_wrong_or_partial_log_never_matches(self):
        data, end = image()
        log = console(data, end)
        for broken in [log.replace('Calculated: ', 'Calculated: 0', 1),
                       log.replace(hashlib.sha256(data[:end]).hexdigest(), 'a' * 64),
                       log.replace(data[end:end + 32].hex(), 'b' * 64), log + log,
                       log.replace('ESP-ROM:esp32s3-', 'ESP-ROM:esp32-')]:
            with self.subTest(log=broken[:80]), self.assertRaises(ValueError):
                inspect_image(data, broken)

    def test_corruption_bad_length_header_and_mapping_rejected(self):
        data, end = image()
        cases = []
        for offset, value in [(0, 0), (1, 0), (12, 0), (23, 1), (end - 1, 0),
                              (end + 8, 1), (65536 - 8, 1)]:
            modified = bytearray(data)
            modified[offset] = value
            cases.append(bytes(modified))
        cases += [data[:15], data[:end], data[:-1], data + b'\xff\x00']
        for broken in cases:
            with self.subTest(length=len(broken)), self.assertRaises(ValueError):
                inspect_image(broken, console(data, end))

    def test_receipt_digest_and_path_are_required(self):
        data, end = image()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'image.bin').write_bytes(data)
            receipt = dict(schema='std-channel-build-v1', status='complete',
                           image_file='image.bin', image_sha256=hashlib.sha256(data).hexdigest(),
                           build_id='b' * 64, source_revision='c' * 40)
            (root / 'build.json').write_text(json.dumps(receipt))
            report = diagnose(root, console(data, end))
            self.assertEqual(report['build_id'], 'b' * 64)
            (root / 'image.bin').write_bytes(data + b'\xff')
            with self.assertRaisesRegex(ValueError, 'differs'):
                diagnose(root, console(data, end))
            receipt['image_file'] = '../image.bin'
            (root / 'build.json').write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, 'escapes'):
                diagnose(root, console(data, end))


if __name__ == '__main__':
    unittest.main()
