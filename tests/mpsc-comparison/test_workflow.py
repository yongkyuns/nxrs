import argparse
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build
import capture


class WorkflowTests(unittest.TestCase):
    def test_native_simpleboot_parser_handles_legacy_and_digest_offsets(self):
        """Execute the actual SDK parser block before/after the carried patch."""
        name = 'arch/xtensa/src/common/espressif/esp_loader.c'
        source = subprocess.check_output(['git', '-C', str(build.ROOT / 'external/nuttx'),
                                          'show', '2b5c0de5e933281c209f954a63b99cf10c132346:' + name], text=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / name
            target.parent.mkdir(parents=True)
            target.write_text(source)
            subprocess.run(['git', 'apply', '--unsafe-paths', str(build.ROOT / 'upstream/nuttx/patches/0108-xtensa-simpleboot-hash-offset.patch')],
                           cwd=root, check=True, capture_output=True)
            patched = target.read_text()
            self.assertEqual(hashlib.sha256(patched.encode()).hexdigest(),
                             'a35533da7d7b800cbac6a6e6de2a307cff727284c0ced2db54269290691c00a9')
            def block(text):
                start = text.index('      if (ram_segments == image_header.segment_count && !padding_checksum)')
                return text[start:text.index('\n    }', start)]
            functions = []
            for label, text in [('old', source), ('fixed', patched)]:
                functions.append('unsigned ' + label + '(unsigned flag) {unsigned offset=36, ram_segments=1;'
                                 'struct {unsigned segment_count, hash_appended;} image_header={1,flag};'
                                 'bool padding_checksum=false;\n' + block(text) + '\nreturn offset;}')
            program = '#include <stdbool.h>\n#define CHECKSUM_ALIGN 16\n' + '\n'.join(functions)
            program += '\nint main(void) {return !(old(0)==48 && old(1)==48 && fixed(0)==48 && fixed(1)==80);}\n'
            (root / 'test.c').write_text(program)
            subprocess.run(['cc', '-Wall', '-Werror', str(root / 'test.c'), '-o', str(root / 'test')], check=True, capture_output=True)
            subprocess.run([str(root / 'test')], check=True)

    def test_plan_is_the_retained_sixteen_cases(self):
        cases = capture.cases()
        self.assertEqual(len(cases), 16)
        self.assertEqual({c['producers'] for c in cases.values()}, {1, 4})
        self.assertEqual(len({tuple(c.items()) for c in cases.values()}), 16)
        self.assertEqual(cases['stream-p4-b16-q8']['samples'], 2048)
        for board in build.BOARDS.values():
            self.assertEqual(len(board['modules']), 2)

    def test_image_digest_preserves_flash_mapping_and_rejects_bad_input(self):
        header = bytearray(24)
        header[0:2] = bytes([0xe9, 1])
        struct.pack_into('<H', header, 12, 9)
        ram = b'CODE'
        checksum = 0xef
        for value in ram:
            checksum ^= value
        image = bytes(header) + struct.pack('<II', 0x3fc90000, len(ram)) + ram + bytes(11) + bytes([checksum])
        image += struct.pack('<II', 0, 65480) + bytes(65480)
        image += struct.pack('<II', 0x42000000, 4) + b'TEXT'
        packed = build.pack_image(image)
        self.assertEqual(len(image), len(packed))
        self.assertEqual(image[88:], packed[88:])
        self.assertEqual(packed[48:80], hashlib.sha256(packed[:48]).digest())
        with self.assertRaises(ValueError):
            build.pack_image(packed)
        with self.assertRaises(ValueError):
            build.pack_image(image[:-1])

    def test_receipt_rejects_artifact_source_and_path_drift(self):
        inventory = build.inventory()
        self.assertIn('upstream/rust-std/patches/0007-nuttx-bounded-channel.patch', inventory)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('elf', 'image', 'config'):
                (root / name).write_text(name)
            record = dict(schema='mpsc-comparison-build-v2', board='pico2w', runtime='nuttx',
                          source_sha256=inventory, board_contract=build.BOARDS['pico2w'],
                          nonce='test', status='complete', failure=None,
                          artifacts_sha256={name: build.digest(root / name) for name in ('elf', 'image', 'config')},
                          **{name + '_file': name for name in ('elf', 'image', 'config')})
            record['build_id'] = build.canonical({k: record[k] for k in ('schema', 'board', 'runtime', 'source_sha256', 'board_contract', 'nonce')})
            def write(value):
                (root / 'build.json').write_text(json.dumps(value))
            write(record)
            with patch.object(build, 'validate_config'):
                self.assertEqual(build.receipt(root)['board'], 'pico2w')
                (root / 'image').write_text('changed')
                with self.assertRaisesRegex(ValueError, 'artifact'):
                    build.receipt(root)
                (root / 'image').write_text('image')
                with patch.object(build, 'inventory', return_value={}):
                    with self.assertRaisesRegex(ValueError, 'source drift'):
                        build.receipt(root)
                broken = copy.deepcopy(record)
                broken['artifacts_sha256']['../escape'] = 'bad'
                write(broken)
                with self.assertRaisesRegex(ValueError, 'escape'):
                    build.receipt(root)

    def test_sessions_require_verified_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'image').write_bytes(b'image')
            receipt = {'image_file': 'image'}
            value = dict(schema='mpsc-comparison-session-v1', image_sha256=build.digest(root / 'image'),
                         fresh_backup_verified=True, flash_verified=True, fresh_boot=True, **{'pass': 1})
            capture.session(value, receipt, root)
            for key in ('fresh_backup_verified', 'flash_verified', 'fresh_boot', 'image_sha256'):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    capture.session({**value, key: False}, receipt, root)

    def test_compare_rejects_different_compilers_and_unpaired_passes(self):
        n = dict(runtime='nuttx', board='pico2w', board_contract={}, source_sha256={}, rust_compiler='same')
        z = {**n, 'runtime': 'zephyr'}
        args = argparse.Namespace(nuttx_build=Path('n'), zephyr_build=Path('z'), captures=[])
        for right in ({**z, 'rust_compiler': 'different'}, z):
            with patch.object(build, 'receipt', side_effect=[n, right]), self.assertRaises(ValueError):
                capture.compare(args)


if __name__ == '__main__':
    unittest.main()
