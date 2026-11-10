import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
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

    def test_native_control_plan_rotates_distinct_arms(self):
        rows = capture.plan(3, include_native_control=True)
        self.assertEqual(len(rows), 3 * 16 * 3)
        case = 'stream-p4-b16-q8'
        orders = [[(r['runtime'], r['backend']) for r in rows if r['pass_number'] == p and r['case'] == case]
                  for p in (1, 2, 3)]
        arms = {('nuttx', 'std'), ('nuttx', 'native'), ('zephyr', 'native')}
        self.assertTrue(all(set(order) == arms for order in orders))
        self.assertTrue(all({order[position] for order in orders} == arms for position in range(3)))
        self.assertEqual(len(capture.plan(2)), 2 * 16 * 2)

    def test_backend_defaults_and_unsupported_pair(self):
        self.assertEqual(build.selected_backend('nuttx'), 'std')
        self.assertEqual(build.selected_backend('nuttx', 'native'), 'native')
        self.assertEqual(build.selected_backend('zephyr'), 'native')
        with self.assertRaisesRegex(ValueError, 'runtime/backend'):
            build.selected_backend('zephyr', 'std')

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
            selection = dict(schema='mpsc-transport-selection-v1', elf_sha256=build.digest(root / 'elf'),
                             backend='native', native_queue_roots=list(build.NATIVE_QUEUE_ROOTS),
                             retained_native_queue_roots=list(build.NATIVE_QUEUE_ROOTS),
                             native_queue_roots_required=True, passed=True)
            (root / 'transport-selection.json').write_text(json.dumps(selection))
            record = dict(schema=build.BUILD_SCHEMA, board='pico2w', runtime='zephyr', backend='native',
                          source_sha256=inventory, board_contract=build.BOARDS['pico2w'],
                          native_build_inputs=None, nonce='test', status='complete', failure=None,
                          transport_selection_file='transport-selection.json',
                          artifacts_sha256={name: build.digest(root / name) for name in ('elf', 'image', 'config', 'transport-selection.json')},
                          **{name + '_file': name for name in ('elf', 'image', 'config')})
            record['build_id'] = build.canonical({k: record[k] for k in build.IDENTITY_FIELDS})
            def write(value):
                (root / 'build.json').write_text(json.dumps(value))
            write(record)
            with patch.object(build, 'validate_config'):
                self.assertEqual(build.receipt(root)['board'], 'pico2w')
                (root / 'image').write_text('changed')
                with self.assertRaisesRegex(ValueError, 'artifact'):
                    build.receipt(root)
                (root / 'image').write_text('image')
                write({**record, 'runtime': 'nuttx', 'backend': 'std'})
                with self.assertRaisesRegex(ValueError, 'identity'):
                    build.receipt(root)
                write(record)
                with patch.object(build, 'inventory', return_value={}):
                    with self.assertRaisesRegex(ValueError, 'source drift'):
                        build.receipt(root)
                wrong_selection = {**selection, 'backend': 'std', 'retained_native_queue_roots': []}
                (root / 'transport-selection.json').write_text(json.dumps(wrong_selection))
                changed = copy.deepcopy(record)
                changed['artifacts_sha256']['transport-selection.json'] = build.digest(root / 'transport-selection.json')
                write(changed)
                with self.assertRaisesRegex(ValueError, 'transport selection'):
                    build.receipt(root)
                (root / 'transport-selection.json').write_text(json.dumps(selection))
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

    def control_receipts(self):
        kernel = dict(schema='mpsc-nuttx-kernel-v1', scope='test archive scope',
                      resolved_config_sha256='config', patch_receipts={'kernel': 'patch'},
                      archives={'libkernel.a': {'raw_sha256': 'raw-a', 'link_sha256': 'code'}})
        n = dict(runtime='nuttx', backend='std', board='pico2w', board_contract={}, source_sha256={},
                 rust_compiler='rust', rust_compiler_sha256='rust-hash',
                 c_compiler='gcc', c_compiler_sha256='gcc-hash', kernel_identity=kernel,
                 native_build_inputs={'source_date_epoch': 123})
        native = copy.deepcopy(n)
        native['backend'] = 'native'
        native['kernel_identity']['archives']['libkernel.a']['raw_sha256'] = 'raw-b'
        z = {**n, 'runtime': 'zephyr', 'backend': 'native'}
        return n, native, z

    def test_compare_rejects_different_compilers_and_unpaired_passes(self):
        n, _, z = self.control_receipts()
        args = argparse.Namespace(nuttx_build=Path('n'), zephyr_build=Path('z'), captures=[])
        for right in ({**z, 'rust_compiler': 'different'}, z):
            with patch.object(build, 'receipt', side_effect=[n, right]), self.assertRaises(ValueError):
                capture.compare(args)

    def test_control_rejects_changed_kernel_and_preserves_debug_distinction(self):
        std, native, _ = self.control_receipts()
        result = build.validate_nuttx_control(std, native)
        self.assertTrue(result['non_application_link_inputs_equal'])
        self.assertFalse(result['raw_archives_identical'])
        self.assertFalse(result['final_image_layout_equal'])
        self.assertFalse(result['matched_placement_qualified'])
        changes = [
            lambda r: r.update(backend='std'),
            lambda r: r.update(c_compiler_sha256='different'),
            lambda r: r.update(board='esp32s3'),
            lambda r: r['native_build_inputs'].update(source_date_epoch=456),
            lambda r: r['kernel_identity'].update(resolved_config_sha256='changed'),
            lambda r: r['kernel_identity']['patch_receipts'].update(kernel='changed'),
            lambda r: r['kernel_identity']['archives']['libkernel.a'].update(link_sha256='changed'),
            lambda r: r['kernel_identity']['archives'].update(extra={'raw_sha256': 'x', 'link_sha256': 'x'}),
        ]
        for change in changes:
            changed = copy.deepcopy(native)
            change(changed)
            with self.subTest(change=change), self.assertRaises(ValueError):
                build.validate_nuttx_control(std, changed)

    def test_compare_pairs_backend_and_pass_without_pooling(self):
        std, native, zephyr = self.control_receipts()
        args = argparse.Namespace(nuttx_build=Path('n'), nuttx_native_build=Path('q'),
                                  zephyr_build=Path('z'), captures=[])
        receipts = {Path('n'): std, Path('q'): native, Path('z'): zephyr}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = {}
            for number in (1, 2):
                for label, receipt in receipts.items():
                    path = root / f'{label}-{number}'
                    path.mkdir()
                    (path / 'run.json').write_text(json.dumps({'expected': {
                        key: receipt[key] for key in ('runtime', 'backend')}}))
                    runs[path] = dict(pass_number=number, case='stream-p1-b16-q8', build_id=str(label),
                                      delivered_per_nominal_second=100, delivery_ticks={'p99': 1},
                                      clock_hz=1000000, offered=10, admitted=10, delivered=10, rejected=0,
                                      normal_loss_gate_passed=True, cpu_ns_per_delivered=None,
                                      wait_observer='queue-object' if label != Path('n') else 'semaphore-state-and-phase')
                    args.captures.append(path)
            with patch.object(build, 'receipt', side_effect=receipts.__getitem__), \
                    patch.object(capture, 'replay', side_effect=lambda path, _: runs[path]):
                result = capture.compare(args)
                self.assertEqual(len(result['comparisons']), 2)
                self.assertTrue(all(len(row['arms']) == 3 for row in result['comparisons']))
                self.assertFalse(result['samples_pooled'])
                self.assertFalse(result['physical_timing_qualified'])
                complete = args.captures[:]
                for bad in (complete[:-1], complete + [complete[0]]):
                    args.captures = bad
                    with self.assertRaises(ValueError):
                        capture.compare(args)
                args.captures = complete
                args.zephyr_build = None
                with self.assertRaisesRegex(ValueError, 'unselected'):
                    capture.compare(args)
                args.captures = [p for p in complete if not p.name.startswith('z-')]
                self.assertEqual(len(capture.compare(args)['comparisons']), 2)
                args.nuttx_native_build = None
                with self.assertRaisesRegex(ValueError, 'requires'):
                    capture.compare(args)

    def test_archive_identity_ignores_debug_paths_but_retains_code_and_placement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives = []
            for label, value in [('a', 7), ('b', 7), ('changed', 8)]:
                output = root / label
                output.mkdir()
                (output / 'kernel.c').write_text(f'extern int other(int); int kernel(void) {{return other({value});}}\n')
                subprocess.run(['cc', '-g', '-c', 'kernel.c', '-o', 'kernel.o'], cwd=output, check=True, capture_output=True)
                archive = output / 'libkernel.a'
                subprocess.run(['ar', 'rcsD', str(archive), 'kernel.o'], cwd=output, check=True, capture_output=True)
                archives.append(archive)
            first, second, changed = (build.archive_members(p, '') for p in archives)
            self.assertNotEqual(build.digest(archives[0]), build.digest(archives[1]))
            self.assertEqual(first, second)
            self.assertNotEqual(first, changed)
            placed = root / 'placed.a'
            subprocess.run(['objcopy', '--rename-section', '.text=.time_critical.text', str(archives[0]), str(placed)],
                           check=True, capture_output=True)
            self.assertNotEqual(first, build.archive_members(placed, ''))

    def test_transport_selection_rejects_wrong_or_mixed_final_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declarations = '\n'.join(f'int {name}(void) {{ return 1; }}' for name in build.NATIVE_QUEUE_ROOTS)
            for label, selected in [('std', ()), ('native', build.NATIVE_QUEUE_ROOTS),
                                    ('mixed', build.NATIVE_QUEUE_ROOTS[:1])]:
                source = root / f'{label}.c'
                source.write_text(declarations + '\nint main(void) { return '
                                  + '+'.join([f'{name}()' for name in selected] or ['0']) + '; }\n')
                elf = root / label
                subprocess.run(['cc', '-ffunction-sections', '-Wl,--gc-sections', str(source), '-o', str(elf)],
                               check=True, capture_output=True)
                destination = root / f'{label}.json'
                if label != 'mixed':
                    result = build.transport_selection(elf, '', label, destination)
                    self.assertTrue(result['passed'])
                    self.assertEqual(result['elf_sha256'], build.digest(elf))
                    self.assertEqual(result['retained_native_queue_roots'], list(selected))
                for backend in (('native',) if label == 'std' else ('std',) if label == 'native' else ('std', 'native')):
                    with self.subTest(image=label, backend=backend), self.assertRaisesRegex(ValueError, 'linked transport'):
                        build.transport_selection(elf, '', backend, destination)
                    self.assertFalse(json.loads(destination.read_text())['passed'])

    def test_duplicate_archive_members_preserve_order_and_each_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            objects = []
            for label, value in [('first', 7), ('second', 8), ('changed', 9)]:
                output = root / label
                output.mkdir()
                source, obj = output / 'same.c', output / 'same.o'
                source.write_text(f'int shared(void) {{ return {value}; }}\n')
                subprocess.run(['cc', '-g', '-c', str(source), '-o', str(obj)], check=True, capture_output=True)
                objects.append(obj)
            versions = []
            for label, order in [('original', [0, 1]), ('swapped', [1, 0]), ('changed', [0, 2])]:
                archive = root / f'{label}.a'
                subprocess.run(['ar', 'qcD', str(archive), *[str(objects[i]) for i in order]],
                               check=True, capture_output=True)
                versions.append(build.archive_members(archive, ''))
            first, swapped, changed = versions
            self.assertEqual([m['occurrence'] for m in first], [1, 2])
            self.assertEqual([m['name'] for m in first], ['same.o', 'same.o'])
            self.assertEqual(first[0]['sha256'], swapped[1]['sha256'])
            self.assertEqual(first[1]['sha256'], swapped[0]['sha256'])
            self.assertNotEqual(build.canonical(first), build.canonical(swapped))
            self.assertEqual(first[0], changed[0])
            self.assertNotEqual(first[1], changed[1])

    def test_compiled_timestamp_and_path_are_repeatable_without_erasing_runtime_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fingerprints, policies = [], []
            for label, value, repeatable in [('first', 7, True), ('second', 7, True),
                                             ('changed', 8, True), ('uncontrolled', 7, False)]:
                output = root / label
                output.mkdir()
                source, obj, archive = output / 'source.c', output / 'source.o', output / 'libkernel.a'
                source.write_text('const char *stamp(void) { return __DATE__ " " __TIME__; }\n'
                                  'const char *source(void) { return __FILE__; }\n'
                                  f'int runtime(void) {{ return KEEP_ME + {value}; }}\n')
                env = {k: v for k, v in os.environ.items() if k not in ('SOURCE_DATE_EPOCH', 'EXTRAFLAGS')}
                inherited = '-DKEEP_ME=11 '
                env['EXTRAFLAGS'] = inherited
                if repeatable:
                    policies.append(build.configure_native_inputs(env, output))
                    self.assertTrue(env['EXTRAFLAGS'].startswith(inherited))
                    self.assertEqual(int(env['SOURCE_DATE_EPOCH']), policies[-1]['source_date_epoch'])
                else:
                    env['SOURCE_DATE_EPOCH'] = '1'
                subprocess.run(['cc', '-g', *shlex.split(env['EXTRAFLAGS']), '-c', str(source), '-o', str(obj)],
                               env=env, check=True, capture_output=True)
                subprocess.run(['ar', 'rcsD', str(archive), str(obj)], check=True, capture_output=True)
                fingerprints.append(build.archive_members(archive, ''))
            self.assertEqual(policies[0], policies[1])
            self.assertEqual(fingerprints[0], fingerprints[1])
            self.assertNotEqual(fingerprints[0], fingerprints[2])
            self.assertNotEqual(fingerprints[0], fingerprints[3])

    def test_native_build_inputs_do_not_overwrite_unsupported_inherited_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for flags in ['-DOPEN="', '-DX=1 # comment', '$(OTHER_FLAGS)', '-DX=1\n-DY=2', '-DX=1; false']:
                env = {'EXTRAFLAGS': flags}
                with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, 'EXTRAFLAGS'):
                    build.configure_native_inputs(env, root)
                self.assertEqual(env, {'EXTRAFLAGS': flags})
            env = {'EXTRAFLAGS': '-DKEEP=1'}
            with self.assertRaisesRegex(ValueError, 'output path'):
                build.configure_native_inputs(env, root / 'space path')
            self.assertEqual(env, {'EXTRAFLAGS': '-DKEEP=1'})
            env = {'EXTRAFLAGS': '-DKEEP=1', 'SOURCE_DATE_EPOCH': 'not-the-pin'}
            with self.assertRaisesRegex(ValueError, 'SOURCE_DATE_EPOCH'):
                build.configure_native_inputs(env, root)
            self.assertEqual(env, {'EXTRAFLAGS': '-DKEEP=1', 'SOURCE_DATE_EPOCH': 'not-the-pin'})

    def test_kernel_receipt_binds_config_patches_and_archive_census(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            firmware = root / 'firmware'
            staging = firmware / 'nuttx/staging'
            staging.mkdir(parents=True)
            source = root / 'kernel.c'
            source.write_text('int kernel(void) { return 7; }\n')
            obj = root / 'kernel.o'
            subprocess.run(['cc', '-g', '-c', str(source), '-o', str(obj)], check=True, capture_output=True)
            archive = staging / 'libkernel.a'
            subprocess.run(['ar', 'rcsD', str(archive), str(obj)], check=True, capture_output=True)
            for name in ('nuttx-patches.json', 'nuttx-apps-patches.json', 'std-patch.json'):
                (firmware / name).write_text(json.dumps({'component': name, 'revision': 'same'}))
            config = firmware / 'resolved.config'
            config.write_text('configuration')
            identity, evidence = build.kernel_identity(root, '', config)
            record = dict(kernel_identity_file=identity.name, config_file=str(config.relative_to(root)),
                          artifacts_sha256={str(p.relative_to(root)): build.digest(p)
                                            for p in [identity, config, *evidence]})
            original = build.read_kernel_identity(root, record)
            self.assertEqual(set(original['archives']), {'libkernel.a'})
            alias = firmware / 'bound-but-wrong-archive.a'
            alias.write_bytes(archive.read_bytes())
            alias_name = str(alias.relative_to(root))
            record['artifacts_sha256'][alias_name] = build.digest(alias)
            wrong = copy.deepcopy(original)
            wrong['archives']['libkernel.a']['file'] = alias_name
            identity.write_text(json.dumps(wrong))
            with self.assertRaisesRegex(ValueError, 'archive identity'):
                build.read_kernel_identity(root, record)
            identity.write_text(json.dumps(original))
            (firmware / 'std-patch.json').write_text('{"revision":"changed"}')
            with self.assertRaisesRegex(ValueError, 'patch'):
                build.read_kernel_identity(root, record)
            (firmware / 'std-patch.json').write_text(json.dumps({'component': 'std-patch.json', 'revision': 'same'}))
            (staging / 'extra.a').write_bytes(archive.read_bytes())
            with self.assertRaisesRegex(ValueError, 'census'):
                build.read_kernel_identity(root, record)
            (staging / 'extra.a').unlink()
            config.write_text('changed')
            with self.assertRaisesRegex(ValueError, 'configuration'):
                build.read_kernel_identity(root, record)


if __name__ == '__main__':
    unittest.main()
