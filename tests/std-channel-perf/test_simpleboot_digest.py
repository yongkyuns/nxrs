"""Synthetic layout and provenance controls; not firmware/physical evidence."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import simpleboot_digest as candidate
from inspect_esp_image import parse_image
from test_esp_image import image


def two_mapped_image():
    data, end = image()
    pad = 131072 - len(data) - 16
    data += struct.pack('<II', 0, pad) + bytes(pad)
    data += struct.pack('<II', 0x3c020000, 16) + b'0123456789abcdef'
    return data, end


class DigestLayoutTests(unittest.TestCase):
    def test_candidate_preserves_all_payloads_and_flash_offsets(self):
        data, end = two_mapped_image()
        transformed, evidence = candidate.repack(data)
        self.assertEqual(len(data), len(transformed))
        self.assertEqual(data[:23], transformed[:23])
        self.assertEqual(data[24:end], transformed[24:end])
        self.assertEqual(data[end + 40:], transformed[end + 40:])
        self.assertEqual(transformed[23], 1)
        self.assertEqual(transformed[end:end + 32], hashlib.sha256(transformed[:end]).digest())
        self.assertTrue(evidence['mapped_offsets_and_bytes_preserved'])
        self.assertTrue(parse_image(transformed)['ram_prefix_digest_verified'])
        self.assertFalse(evidence['boot_warning_resolved'])
        self.assertFalse(evidence['secure_boot_qualified'])

    def test_repacking_and_corrupt_digest_are_rejected(self):
        data, end = image()
        transformed, _ = candidate.repack(data)
        with self.assertRaisesRegex(ValueError, 'already'):
            candidate.repack(transformed)
        for offset in (10, end, end + 31):
            bad = bytearray(transformed)
            bad[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, 'digest'):
                parse_image(bad)

    def test_too_small_padding_is_rejected_without_relocation(self):
        data, end = image()
        # Put the mapped payload at a matching virtual page offset, retaining
        # a valid but too-small 32-byte first padding payload.
        small = data[:end] + struct.pack('<II', 0, 32) + bytes(32)
        small += struct.pack('<II', 0x42000000 + len(small) + 8, 4) + b'DATA'
        parse_image(small)
        with self.assertRaisesRegex(ValueError, 'insufficient'):
            candidate.repack(small)

    def test_hidden_payload_not_covered_by_rom_digest_is_explicit(self):
        data, _ = two_mapped_image()
        transformed, evidence = candidate.repack(data)
        changed = bytearray(transformed)
        changed[-1] ^= 1
        # The ROM-prefix hash is deliberately NOT represented as a whole-image
        # integrity check. Artifact SHA256 must still identify hidden payload.
        self.assertTrue(parse_image(changed)['ram_prefix_digest_verified'])
        self.assertNotEqual(candidate.sha(changed), evidence['output_sha256'])
        self.assertIn('RAM checksum only', evidence['digest_scope'])

    def test_existing_input_corruption_is_not_rehashed_into_a_pass(self):
        data, end = image()
        for offset in (32, end - 1, end + 8):
            bad = bytearray(data)
            bad[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                candidate.repack(bad)

    def test_unsupported_or_truncated_inputs_fail(self):
        data, end = image()
        for bad in (b'', data[:end], data[:-1], b'\0' + data[1:]):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                candidate.repack(bad)


class CandidateBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'nuttx').mkdir()
        self.image = self.root / 'nuttx/nuttx.merged.bin'
        self.data, _ = two_mapped_image()
        self.image.write_bytes(self.data)
        (self.root / 'resolved.config').write_text(
            'CONFIG_ARCH_CHIP_ESP32S3=y\nCONFIG_ESPRESSIF_SIMPLE_BOOT=y\n')
        self.source = self.root / 'nuttx' / candidate.LOADER_PATH
        self.source.parent.mkdir(parents=True)
        self.source.write_text('if (image_header.hash_appended == 1) { offset += 32; }\n')
        self.ledger = dict(component='nuttx', upstream_revision=candidate.KERNEL_REVISION,
            patches=[dict(name=candidate.PATCH_NAME,
                sha256=candidate.sha(candidate.PATCH.read_bytes()),
                files={candidate.LOADER_PATH: dict(before=candidate.LOADER_BEFORE,
                    after=candidate.sha(self.source.read_bytes()))})])
        self.write_ledger()

    def write_ledger(self):
        (self.root / 'nuttx-patches.json').write_text(json.dumps(self.ledger))

    def test_old_archived_firmware_cannot_be_made_deployable_by_repacking(self):
        self.ledger['patches'] = []
        self.write_ledger()
        with self.assertRaisesRegex(ValueError, 'must be built first'):
            candidate.prepare(self.root, self.image)
        self.assertEqual(self.image.read_bytes(), self.data)
        self.assertFalse((self.root / 'nuttx/nuttx.rom-digest.bin').exists())

    def test_wrong_pin_patch_hash_source_and_duplicate_entry_fail(self):
        original = copy.deepcopy(self.ledger)
        for case in ('pin', 'patch', 'source', 'duplicate'):
            self.ledger = copy.deepcopy(original)
            if case == 'pin':
                self.ledger['upstream_revision'] = '0' * 40
            elif case == 'patch':
                self.ledger['patches'][0]['sha256'] = '0' * 64
            elif case == 'source':
                self.ledger['patches'][0]['files'][candidate.LOADER_PATH]['after'] = '0' * 64
            else:
                self.ledger['patches'] *= 2
            self.write_ledger()
            with self.subTest(case=case), self.assertRaises(ValueError):
                candidate.prepare(self.root, self.image)

    def test_candidate_is_exclusive_and_preserves_raw_input_receipt(self):
        with patch.object(candidate, 'LOADER_AFTER', candidate.sha(self.source.read_bytes())):
            output, report = candidate.prepare(self.root, self.image)
            self.assertEqual(self.image.read_bytes(), self.data)
            self.assertTrue(parse_image(output.read_bytes())['ram_prefix_digest_verified'])
            self.assertEqual(json.loads(report.read_text())['qualification'], 'build-candidate-only')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                candidate.prepare(self.root, self.image)
            self.assertEqual(self.image.read_bytes(), self.data)

    def test_wrong_profile_fails_before_creating_output(self):
        (self.root / 'resolved.config').write_text('CONFIG_ESP32S3_APP_FORMAT_MCUBOOT=y\n')
        with self.assertRaisesRegex(ValueError, 'SIMPLE_BOOT'):
            candidate.prepare(self.root, self.image)

    def test_symlink_escape_and_modified_source_fail(self):
        self.source.write_text('arbitrary code with matching ledger is not the pinned patch')
        self.ledger['patches'][0]['files'][candidate.LOADER_PATH]['after'] = candidate.sha(self.source.read_bytes())
        self.write_ledger()
        with self.assertRaisesRegex(ValueError, 'source/ledger'):
            candidate.prepare(self.root, self.image)
        outside = self.root / 'outside.c'
        outside.write_text('external source')
        self.source.unlink()
        self.source.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'outside'):
            candidate.prepare(self.root, self.image)


if __name__ == '__main__':
    unittest.main()
