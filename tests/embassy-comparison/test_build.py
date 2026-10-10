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
    def test_stack_guard_remains_at_the_expected_platform_address(self):
        sections = [dict(name='.stack', size=8192, address=0x3fcfe000)]
        build.assert_stack_guard(
            sections, '  3: 3fcfe040 0 NOTYPE GLOBAL DEFAULT ABS __stack_chk_guard')
        with self.assertRaisesRegex(ValueError, 'stack guard'):
            build.assert_stack_guard(
                sections, '  3: 3fcfe020 0 NOTYPE GLOBAL DEFAULT ABS __stack_chk_guard')

    def test_cargo_modes_preserve_default_features(self):
        wire = build.cargo_build_command('wire', 'release', Path('/tmp/out/cargo'))
        packet = build.cargo_build_command('packet', 'size', Path('/tmp/out/cargo'))
        baseline = build.cargo_build_command('baseline', 'release', Path('/tmp/out/cargo'))
        for command in (wire, packet, baseline):
            self.assertNotIn('--no-default-features', command)
        self.assertNotIn('--features', wire)
        self.assertEqual(packet[-2:], ['--features', 'packet'])
        self.assertEqual(baseline[-2:], ['--features', 'baseline'])


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
