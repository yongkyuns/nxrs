"""Pure validation tests for the Embassy build sidecar; no Cargo build."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    'embassy_build_tests_module', Path(__file__).with_name('build.py'))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class SectionTests(unittest.TestCase):
    def test_accounting_keeps_sections_but_deduplicates_alias_and_padding(self):
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
        result = build.section_accounting(sections)
        self.assertEqual(result['resident_ram_bytes'], 0x120)
        self.assertEqual(result['resident_ram_sections'], ['.text', '.bss'])
        self.assertEqual(result['loadbearing_flash_bytes'], 0x200)
        self.assertEqual({s['name'] for s in result['excluded_dummy_padding']},
                         {'.rotext_dummy'})
        self.assertEqual(result['excluded_iram_aliases'],
                         [{'name': '.rwdata_dummy', 'size': 0x100}])
        self.assertEqual(len(sections), 4)  # Full input inventory remains intact.

    def test_stack_section_is_exactly_eight_kib(self):
        sections = [dict(name='.stack', size=8192, address=0x3fcfe000)]
        build.assert_stack_section(sections)
        build.assert_stack_guard(
            sections, '  3: 3fcfe040 0 NOTYPE GLOBAL DEFAULT ABS __stack_chk_guard')
        with self.assertRaisesRegex(ValueError, 'stack guard'):
            build.assert_stack_guard(
                sections, '  3: 3fcfe020 0 NOTYPE GLOBAL DEFAULT ABS __stack_chk_guard')
        with self.assertRaisesRegex(ValueError, 'exactly 8192'):
            build.assert_stack_section([dict(name='.stack', size=8191)])
        with self.assertRaisesRegex(ValueError, 'exactly 8192'):
            build.assert_stack_section([])

    def test_cargo_modes_preserve_default_features(self):
        wire = build.cargo_build_command('wire', 'release', Path('/tmp/out/cargo'))
        packet = build.cargo_build_command('packet', 'size', Path('/tmp/out/cargo'))
        baseline = build.cargo_build_command('baseline', 'release', Path('/tmp/out/cargo'))
        for command in (wire, packet, baseline):
            self.assertNotIn('--no-default-features', command)
        self.assertNotIn('--features', wire)
        self.assertEqual(packet[-2:], ['--features', 'packet'])
        self.assertEqual(baseline[-2:], ['--features', 'baseline'])

    def test_merged_image_header_checks_exact_flash_settings(self):
        self.assertEqual(build.image_header(bytes([0xe9, 0, 2, 0x40])), {
            'magic': 0xe9, 'flash_mode': 'DIO', 'flash_mode_value': 2,
            'flash_frequency_mhz': 40, 'flash_frequency_value': 0,
            'flash_size_mb': 16, 'flash_size_value': 4,
        })
        for image in (bytes([0, 0, 2, 0x40]), bytes([0xe9, 0, 3, 0x40]),
                      bytes([0xe9, 0, 2, 0x41]), bytes([0xe9, 0, 2, 0x30])):
            with self.subTest(image=image), self.assertRaises(ValueError):
                build.image_header(image)


class SourceInventoryTests(unittest.TestCase):
    def test_hashes_inventory_of_fixed_and_rust_source_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'tests' / 'embassy-comparison'
            (root / '.cargo').mkdir(parents=True)
            (root / 'src').mkdir()
            shared = root.parent / 'service-footprint' / 'src'
            shared.mkdir(parents=True)
            (root.parent / 'rtos_harness').mkdir()
            files = {
                'Cargo.toml': 'manifest', 'Cargo.lock': 'lock', 'build.rs': 'build',
                'stack.x': 'stack', '.cargo/config.toml': 'target',
                'src/main.rs': 'main', 'src/lib.rs': 'lib',
                '../service-footprint/src/payload_kernels.rs': 'kernels',
                '../service-footprint/src/packet_service.rs': 'packet',
                '../rtos_harness/images.py': 'shared images',
            }
            for relative, contents in files.items():
                path = (root.parent / relative[3:] if relative.startswith('../')
                        else root / relative)
                if relative.startswith('../service-footprint/src/'):
                    path = shared / Path(relative).name
                path.write_text(contents)
            inventory = build.source_inventory(root)
            self.assertEqual(set(inventory), set(files))
            self.assertEqual(inventory['src/main.rs'], build.sha256(root / 'src/main.rs'))
            (root / 'src/main.rs').write_text('changed')
            changed = build.source_inventory(root)
            self.assertNotEqual(inventory['src/main.rs'], changed['src/main.rs'])
            (root / 'src/extra.rs').write_text('extra source')
            expanded = build.source_inventory(root)
            self.assertEqual(expanded['src/extra.rs'], build.sha256(root / 'src/extra.rs'))


class ArgumentTests(unittest.TestCase):
    def test_optional_cargo_target_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = []
            for name in ('espflash', 'readelf'):
                tool = root / name
                tool.write_text('#!/bin/sh\n')
                tool.chmod(0o755)
                tools.append(tool)
            base = ['--mode', 'wire', '--profile', 'release', '--espflash', str(tools[0]),
                    '--readelf', str(tools[1])]
            default = build.arguments(base + ['--out', str(root / 'default')])
            self.assertIsNone(default.cargo_target_dir)
            custom = root / 'shared-target'
            configured = build.arguments(base + ['--out', str(root / 'case'),
                                                  '--cargo-target-dir', str(custom)])
            self.assertEqual(configured.cargo_target_dir, custom)
            command = build.cargo_build_command(configured.mode, configured.profile,
                                                configured.cargo_target_dir)
            self.assertEqual(command[command.index('--target-dir') + 1], custom)


if __name__ == '__main__':
    unittest.main()
