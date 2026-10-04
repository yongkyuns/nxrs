"""Pure validation tests for the Zephyr build sidecar; no SDK required."""
import importlib.util
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('zephyr_build', Path(__file__).with_name('build.py'))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class SectionTests(unittest.TestCase):
    def test_readelf_wide_rows_and_accounting(self):
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
        sections = build.parse_sections(output)
        self.assertEqual(len(sections), 15)
        self.assertEqual(sections[0]['size'], 0x120)
        result = build.section_accounting(sections)
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
        result = build.section_accounting(sections)
        self.assertEqual(result['loadbearing_flash_bytes'], 4)
        self.assertEqual(result['resident_ram_bytes'], 4 + 8 + 16)
        self.assertIn('.text.z_dummy_thread_init', result['flash_sections'])
        self.assertEqual({item['name'] for item in result['excluded_dummy_padding']},
                         {'.drom0.dummy', '.flash.text_dummy'})

    def test_malformed_readelf_output_fails(self):
        with self.assertRaisesRegex(ValueError, 'no parseable'):
            build.parse_sections('readelf: not an ELF file')


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = '\n'.join([
            'CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC=240000000',
            'CONFIG_MINIMAL_LIBC=y', 'CONFIG_ESP_SIMPLE_BOOT=y',
            'CONFIG_MAIN_STACK_SIZE=8192', 'CONFIG_ISR_STACK_SIZE=4096',
            'CONFIG_SIZE_OPTIMIZATIONS=y', 'CONFIG_TIMESLICING=y',
            'CONFIG_TIMESLICE_SIZE=10', 'CONFIG_TIMESLICE_PRIORITY=0',
            'CONFIG_ESPTOOLPY_FLASHMODE_DIO=y', 'CONFIG_ESPTOOLPY_FLASHFREQ_40M=y',
            'CONFIG_ESP32S3_INSTRUCTION_CACHE_16KB=y',
            'CONFIG_ESP32S3_INSTRUCTION_CACHE_8WAYS=y',
            'CONFIG_ESP32S3_INSTRUCTION_CACHE_LINE_32B=y',
            'CONFIG_ESP32S3_DATA_CACHE_32KB=y', 'CONFIG_ESP32S3_DATA_CACHE_8WAYS=y',
            'CONFIG_ESP32S3_DATA_CACHE_LINE_32B=y',
            'CONFIG_HEAP_MEM_POOL_SIZE=0', 'CONFIG_HEAP_MEM_POOL_ADD_SIZE_BOARD=4096',
            'CONFIG_SYS_HEAP_RUNTIME_STATS=y',
            '# CONFIG_SMP is not set', '# CONFIG_NETWORKING is not set',
            '# CONFIG_WIFI is not set', '# CONFIG_BT is not set',
            '# CONFIG_SHELL is not set', '# CONFIG_ESP_SPIRAM is not set',
        ]) + '\n'

    def test_matched_kernel_and_flash_report(self):
        values = build.assert_config(self.config)
        self.assertEqual(values['CONFIG_TIMESLICE_SIZE'], '10')
        report = build.config_report(self.config)
        self.assertEqual(report['flash_mode'], 'DIO')
        self.assertEqual(report['flash_frequency_mhz'], 40)
        self.assertEqual(report['nuttx_reference']['cpu_mhz'], 240)
        self.assertEqual(report['differences_vs_nuttx'], {})

    def test_80mhz_flash_sensitivity_is_reported_as_a_difference(self):
        speed_flash = self.config.replace('CONFIG_ESPTOOLPY_FLASHFREQ_40M=y',
                                          'CONFIG_ESPTOOLPY_FLASHFREQ_80M=y')
        report = build.config_report(speed_flash)
        self.assertEqual(report['flash_frequency_mhz'], 80)
        self.assertEqual(report['differences_vs_nuttx']['flash_frequency_mhz'],
                         {'zephyr': 80, 'nuttx': 40})

    def test_invalid_clock_or_enabled_shell_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC'):
            build.assert_config(self.config.replace('240000000', '160000000'))
        with self.assertRaisesRegex(ValueError, 'CONFIG_SHELL'):
            build.assert_config(self.config.replace('# CONFIG_SHELL is not set', 'CONFIG_SHELL=y'))

    def test_application_thread_stacks_are_pinned_in_project_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'native_adapter.c').write_text(
                'K_THREAD_STACK_DEFINE(collector_stack, 6144);\n'
                'K_THREAD_STACK_ARRAY_DEFINE(role_stacks, THREADS - 1, 4096);\n')
            build.assert_project_stacks(root)
            (root / 'native_adapter.c').write_text('K_THREAD_STACK_DEFINE(stack, 4096);\n')
            with self.assertRaisesRegex(ValueError, 'collector'):
                build.assert_project_stacks(root)


class ArgumentAndProvenanceTests(unittest.TestCase):
    def test_rejects_existing_output_and_nonexecutable_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / 'sdk'
            sdk.mkdir()
            python = root / 'python'
            python.write_text('#!/bin/sh\n')
            existing = root / 'existing'
            existing.mkdir()
            base = ['--zephyr', str(root), '--espressif', str(root), '--xtensa', str(root),
                    '--sdk', str(sdk), '--python', str(python), '--profile', 'size',
                    '--mode', 'wire']
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    build.arguments(base + ['--out', str(existing)])
            python.chmod(0o755)
            args = build.arguments(base + ['--out', str(root / 'fresh')])
            self.assertEqual(args.mode, 'wire')

    def test_cmake_keeps_venv_symlink_and_scopes_speed_to_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / 'sdk'
            sdk.mkdir()
            venv_bin = root / 'venv' / 'bin'
            venv_bin.mkdir(parents=True)
            python = venv_bin / 'python'
            python.symlink_to(sys.executable)
            common = ['--zephyr', str(root), '--espressif', str(root), '--xtensa', str(root),
                      '--sdk', str(sdk), '--python', str(python), '--out', str(root / 'out'),
                      '--profile', 'speed', '--mode', 'packet']
            args = build.arguments(common)
            command = build.cmake_command(args, args.out)
            self.assertIn(f'-DPython3_EXECUTABLE={python.absolute()}', command)
            self.assertIn('-DNXRS_APP_SPEED=ON', command)
            self.assertIn(f'-DZephyr_DIR={root.resolve()}/share/zephyr-package/cmake', command)
            command_text = list(map(str, command))
            self.assertFalse(any('speed.conf' in value for value in command_text))
            self.assertFalse(any(value.startswith('-DEXTRA_CONF_FILE=') for value in command_text))
            args = build.arguments(common + ['--native-defaults'])
            command = build.cmake_command(args, args.out)
            self.assertTrue(any(value.endswith('native-defaults.conf') for value in map(str, command)))
            args = build.arguments(common + ['--native-defaults', '--lean'])
            command_text = list(map(str, build.cmake_command(args, args.out)))
            overlay_arg = next(value for value in command_text if value.startswith('-DEXTRA_CONF_FILE='))
            self.assertIn('native-defaults.conf;'+str(Path(build.HERE / 'lean.conf')), overlay_arg)
            self.assertIn('-DNXRS_APP_SPEED=ON', command_text)

    def test_readelf_path_selects_sdk_xtensa_toolchain_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            tool = (Path(temporary) / 'xtensa-espressif_esp32s3_zephyr-elf' / 'bin' /
                    'xtensa-espressif_esp32s3_zephyr-elf-readelf')
            tool.parent.mkdir(parents=True)
            tool.write_text('#!/bin/sh\n')
            tool.chmod(0o755)
            self.assertEqual(build.readelf_for(temporary), tool.resolve())

    def test_source_hashes_are_mode_scoped(self):
        root = Path(build.__file__).parent
        wire = build.source_hashes(root, 'wire')
        packet = build.source_hashes(root, 'packet')
        baseline = build.source_hashes(root, 'baseline')
        self.assertNotIn('../service-footprint/payload_processing.c', wire)
        self.assertIn('../service-footprint/payload_processing.c', packet)
        self.assertNotIn('native_adapter.c', baseline)
        self.assertNotIn('../service-footprint/channel_scale_mq.c', baseline)
        self.assertNotIn('test_build.py', packet)
        self.assertNotIn('results/esp32s3-2026-10-03.json', packet)


if __name__ == '__main__':
    unittest.main()
