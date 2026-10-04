"""Protect serial-use diagnostic command registration regeneration."""
import io
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

from relink_rust import freeze_partial, lock_build_tree, optimization_override, refresh_registration, selected_helpers


class RegistrationTests(unittest.TestCase):
    def test_stale_paired_helpers_are_not_selected_for_individual_image(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            for name in ('esp32s3_cycles.c', 'nxrs_target_helper.c',
                         *[f'nxrs_target_helper_{i}.c' for i in range(1, 7)]):
                (app / name).touch()
            self.assertEqual(selected_helpers(app, ['nxrs_target_helper.c', 'nxrs_target_helper_1.c']),
                             ['esp32s3_cycles.c', 'nxrs_target_helper.c',
                              'nxrs_target_helper_1.c'])
            with self.assertRaises(ValueError):
                selected_helpers(app, ['missing-current-helper.c'])

    def test_build_tree_lock_rejects_parallel_mutation_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            with lock_build_tree(tree):
                with self.assertRaisesRegex(RuntimeError, 'already in use'):
                    lock_build_tree(tree)
            with lock_build_tree(tree):
                pass
    def test_numeric_optimization_is_not_a_toml_string(self):
        self.assertTrue(optimization_override('2').endswith('opt-level=2'))
        self.assertTrue(optimization_override('z').endswith('opt-level="z"'))
        self.assertIn('nxrs-footprint-demo', optimization_override('2'))

    def test_fresh_partial_is_not_replaced_by_cache(self):
        with patch.object(Path, 'is_file', return_value=True), \
                patch('relink_rust.shutil.copy2') as copy:
            self.assertFalse(freeze_partial([], 'cq-scale', Path('/fresh/input.elf')))
            copy.assert_not_called()

    def test_freezes_only_identified_relocatable_xtensa_cache(self):
        messages = [dict(reason='compiler-artifact', target=dict(name='cq-scale'),
                         executable='/cargo/release/cq-scale')]
        with patch.object(Path, 'is_file', return_value=False), \
                patch('relink_rust.subprocess.check_output',
                      return_value='Type: REL (Relocatable file)\nMachine: Xtensa\n'), \
                patch('relink_rust.shutil.copy2') as copy:
            self.assertTrue(freeze_partial(messages, 'cq-scale', Path('/frozen/input.elf')))
            copy.assert_called_once_with(Path('/cargo/release/cq-scale'), Path('/frozen/input.elf'))

    def test_rejects_missing_duplicate_host_or_final_executables(self):
        message = dict(reason='compiler-artifact', target=dict(name='cq-scale'),
                       executable='/cargo/release/cq-scale')
        with patch.object(Path, 'is_file', return_value=False):
            for messages in ([], [message, message]):
                with self.subTest(messages=messages), self.assertRaises(ValueError):
                    freeze_partial(messages, 'cq-scale', Path('/frozen/input.elf'))
            for header in ('Type: EXEC\nMachine: Xtensa', 'Type: REL\nMachine: X86-64'):
                with self.subTest(header=header), \
                        patch('relink_rust.subprocess.check_output', return_value=header), \
                        self.assertRaises(ValueError):
                    freeze_partial([message], 'cq-scale', Path('/frozen/input.elf'))

    def test_removes_stale_registry_and_rebuilds_all_app_stamps(self):
        tree = Path('/prepared/tree')
        variables = ['NXRS_APP_COMMAND=cq_reuse', 'NXRS_STD_ELF=/saved/input.elf']
        env, log = {'PATH': '/tool/bin'}, io.StringIO()
        with patch('relink_rust.subprocess.run') as run:
            refresh_registration(tree, variables, env, log)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[-1] for command in commands],
                         ['clean_context', 'clean', 'context'])
        self.assertEqual([command[command.index('-C') + 1] for command in commands],
                         ['/prepared/tree/apps/builtin', '/prepared/tree/apps',
                          '/prepared/tree/apps'])
        for call in run.call_args_list:
            self.assertIn('TOPDIR=/prepared/tree/nuttx', call.args[0])
            self.assertIn('APPDIR=/prepared/tree/apps', call.args[0])
            for variable in variables:
                self.assertIn(variable, call.args[0])
            self.assertTrue(call.kwargs['check'])
            self.assertIs(call.kwargs['env'], env)
            self.assertIs(call.kwargs['stdout'], log)


if __name__ == '__main__':
    unittest.main()
