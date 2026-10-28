"""Owned-filesystem controls for the fixed disposable-image permission repair."""
from contextlib import ExitStack, contextmanager, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/native-packages'))
spec = importlib.util.spec_from_file_location('python_data_setup_tests', ROOT / 'tools/native-packages/python_data_protection.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def observed(info, **changes):
    fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_size',
              'st_nlink', 'st_mtime_ns', 'st_ctime_ns')
    return SimpleNamespace(**{name: changes.get(name, getattr(info, name)) for name in fields})


class PythonDataProtectionTests(unittest.TestCase):
    @contextmanager
    def observation(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            report = Path(directory) / 'report.json'
            stack.enter_context(mock.patch.object(setup.u, 'hosted'))
            stack.enter_context(mock.patch.object(setup.os, 'geteuid', return_value=501))
            stack.enter_context(mock.patch.object(setup.sys, 'flags', SimpleNamespace(isolated=1)))
            stack.enter_context(mock.patch.object(setup, 'source_inputs', return_value={'owned': 'b' * 64}))
            command = stack.enter_context(mock.patch.object(setup.u, 'command',
                side_effect=lambda args: 'a' * 40 if args[-1] == 'HEAD' else ''))
            metadata = stack.enter_context(mock.patch.object(Path, 'lstat', autospec=True,
                return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)))
            yield report, metadata, command

    def test_fixed_permission_observation_is_private_read_only_and_unqualified(self):
        with self.observation() as (report, metadata, command), \
             mock.patch.object(Path, 'readlink') as readlink, \
             mock.patch.object(Path, 'read_bytes') as read_bytes, \
             mock.patch.object(setup.u, 'execute') as execute, \
             mock.patch.object(setup, 'harden') as harden:
            value = setup.observe_image_permissions('before-tests', report)
            self.assertEqual(metadata.call_count, len(setup.OBSERVATION_PATHS))
            self.assertEqual(metadata.call_args_list, [mock.call(path) for path in setup.OBSERVATION_PATHS])
            with report.open() as source:
                self.assertEqual(json.load(source), value)
            self.assertEqual(value['paths'], [dict(path=str(path), kind='directory', mode='0777', root_owned=True)
                                             for path in setup.OBSERVATION_PATHS])
            self.assertEqual(value['checkout'], dict(clean=True, commit='a' * 40))
            self.assertEqual(value['inputs'], {'owned': 'b' * 64})
            self.assertEqual(value['phase'], 'before-tests')
            self.assertEqual(value['scope'], 'fixed-hosted-image-permission-observation')
            self.assertFalse(value['permissions_changed'])
            self.assertTrue(all(value[key] is False for key in setup.FALSE_FLAGS))
            self.assertEqual(value['advisory_findings_cleared'], 0)
            self.assertLessEqual(report.stat().st_size, 16384)
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            readlink.assert_not_called(); read_bytes.assert_not_called()
            execute.assert_not_called(); harden.assert_not_called()

    def test_observation_handles_missing_and_symlink_without_reading_targets(self):
        with self.observation() as (report, metadata, _):
            metadata.side_effect = [
                FileNotFoundError('PRIVATE-PATH'),
                SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=501),
                SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0),
                *[SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_uid=0)
                  for _ in setup.OBSERVATION_PATHS[3:]]]
            value = setup.observe_image_permissions('after-tests', report)
            self.assertEqual(value['paths'][0], dict(path='/', kind='missing', mode=None, root_owned=None))
            self.assertEqual(value['paths'][1], dict(path='/usr', kind='symlink', mode='0777', root_owned=False))
            self.assertEqual(value['paths'][2]['kind'], 'file')
            self.assertTrue(all(row['kind'] == 'other' for row in value['paths'][3:]))
            self.assertNotIn('PRIVATE', json.dumps(value))

    def test_observation_rejects_wrong_context_and_phase_before_metadata_io(self):
        for case in ('personal-host', 'root', 'not-isolated', 'phase'):
            with self.subTest(case=case), self.observation() as (report, metadata, command), ExitStack() as stack:
                if case == 'personal-host':
                    stack.enter_context(mock.patch.object(setup.u, 'hosted', side_effect=setup.u.Refusal('hosted-ubuntu-required')))
                elif case == 'root':
                    stack.enter_context(mock.patch.object(setup.os, 'geteuid', return_value=0))
                elif case == 'not-isolated':
                    stack.enter_context(mock.patch.object(setup.sys, 'flags', SimpleNamespace(isolated=0)))
                with self.assertRaises(setup.u.Refusal):
                    setup.observe_image_permissions('PRIVATE-PHASE' if case == 'phase' else 'before-tests', report)
                metadata.assert_not_called(); command.assert_not_called()
                self.assertFalse(report.exists())

    def test_observation_never_overwrites_regular_file_or_symlink(self):
        for linked in (False, True):
            with self.subTest(linked=linked), self.observation() as (report, metadata, _):
                if linked: report.symlink_to(report.with_name('absent'))
                else: report.write_bytes(b'owned-existing-report')
                with self.assertRaisesRegex(setup.u.Refusal, 'new-image-permission-report'):
                    setup.observe_image_permissions('before-tests', report)
                metadata.assert_not_called()
                if not linked: self.assertEqual(report.read_bytes(), b'owned-existing-report')

    def test_observation_source_changes_and_unexpected_metadata_errors_emit_no_report(self):
        for case in ('changed-input', 'dirty', 'invalid-head', 'metadata-error'):
            with self.subTest(case=case), self.observation() as (report, metadata, command), ExitStack() as stack:
                if case == 'changed-input':
                    stack.enter_context(mock.patch.object(setup, 'source_inputs', side_effect=[{'a': 'b'}, {'a': 'c'}]))
                elif case == 'dirty':
                    command.side_effect = lambda args: 'a' * 40 if args[-1] == 'HEAD' else 'changed'
                elif case == 'invalid-head':
                    command.side_effect = lambda args: 'PRIVATE-HEAD' if args[-1] == 'HEAD' else ''
                else: metadata.side_effect = PermissionError('PRIVATE-METADATA-ERROR')
                with self.assertRaises((setup.u.Refusal, PermissionError)):
                    setup.observe_image_permissions('after-refresh', report)
                self.assertFalse(report.exists())

    @contextmanager
    def fixture(self, mode=0o777, uid=0, command=None, parent_mode=0o755):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            target = Path(temporary) / 'locale-data'
            target.parent.chmod(parent_mode)
            target.mkdir(); target.chmod(mode)
            real_fstat, real_lstat = os.fstat, Path.lstat
            calls = []

            def execute(args):
                path = Path(args[-1])
                self.assertIn(path, (target.parent, target))
                self.assertEqual(args, ['/usr/bin/sudo', '-n', '/usr/bin/chmod', 'go-w', '--', str(path)])
                if path == target:
                    self.assertEqual(stat.S_IMODE(real_lstat(target.parent).st_mode), 0o755)
                calls.append(args)
                if command:
                    return command(path)
                path.chmod(stat.S_IMODE(real_lstat(path).st_mode) & ~0o022)
                return 0, b''

            stack.enter_context(mock.patch.object(setup, 'TARGET', target))
            stack.enter_context(mock.patch.object(setup.u, 'hosted'))
            stack.enter_context(mock.patch.object(setup.os, 'geteuid', return_value=501))
            protected = stack.enter_context(mock.patch.object(setup.components, 'protected'))
            stack.enter_context(mock.patch.object(setup.os, 'fstat', side_effect=lambda fd: observed(real_fstat(fd), st_uid=uid)))
            stack.enter_context(mock.patch.object(Path, 'lstat', autospec=True,
                side_effect=lambda path: observed(real_lstat(path), st_uid=uid)))
            stack.enter_context(mock.patch.object(setup.u, 'execute', side_effect=execute))
            yield target, calls, protected

    def test_only_write_bits_removed_for_each_admitted_mode(self):
        for mode in (0o755, 0o757, 0o775, 0o777):
            with self.subTest(mode=mode), self.fixture(mode) as (target, calls, protected):
                payload = target / 'owned-data'; payload.write_bytes(b'PRIVATE-OWNED-CONTENTS')
                parent, result = setup.harden()
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
                self.assertEqual(payload.read_bytes(), b'PRIVATE-OWNED-CONTENTS')
                self.assertEqual(result['before_mode'], format(mode, '04o'))
                self.assertIs(result['changed'], mode != 0o755)
                self.assertEqual(len(calls), int(mode != 0o755))
                self.assertEqual(parent['path'], str(target.parent))
                self.assertEqual(protected.call_args_list, [mock.call(target.parent.parent), mock.call(target.parent),
                                                           mock.call(target.parent), mock.call(target)])
                self.assertNotIn('PRIVATE-OWNED-CONTENTS', json.dumps(result))

    def test_other_modes_and_special_bits_refuse_without_privileged_command(self):
        for mode in (0o700, 0o750, 0o705, 0o744, 0o1777):
            with self.subTest(mode=mode), self.fixture(mode) as (_, calls, _):
                with self.assertRaisesRegex(setup.u.Refusal, 'reviewed-root-owned-python-data-directory'):
                    setup.harden()
                self.assertEqual(calls, [])

    def test_parent_is_secured_before_child_and_no_unrelated_tree_is_touched(self):
        with self.fixture(parent_mode=0o777) as (target, calls, _):
            unrelated = target.parent / 'unrelated'; unrelated.mkdir(mode=0o777); unrelated.chmod(0o777)
            rows = setup.harden()
            self.assertEqual([Path(args[-1]) for args in calls], [target.parent, target])
            self.assertEqual([row['path'] for row in rows], [str(target.parent), str(target)])
            self.assertTrue(all(row['changed'] for row in rows))
            self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o777)

    def test_refused_child_does_not_restore_unsafe_parent_permissions(self):
        with self.fixture(mode=0o700, parent_mode=0o777) as (target, calls, _):
            with self.assertRaisesRegex(setup.u.Refusal, 'reviewed-root-owned-python-data-directory'):
                setup.harden()
            self.assertEqual([Path(args[-1]) for args in calls], [target.parent])
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_other_repair_paths_refuse_before_inspection(self):
        with self.fixture() as (target, calls, protected):
            with self.assertRaisesRegex(setup.u.Refusal, 'fixed-python-data-setup-directory'):
                setup.harden_directory(target.with_name('unreviewed'))
            protected.assert_not_called(); self.assertEqual(calls, [])

    def test_refusal_diagnostics_admit_only_fixed_paths_and_protection_booleans(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)
        output = io.StringIO()
        with redirect_stdout(output):
            setup.protection_diagnostic(setup.components.PathProtectionRefusal(setup.TARGET.parent, setup.TARGET.parent, info))
        prefix, encoded = output.getvalue().strip().split(' ', 1)
        self.assertEqual(prefix, 'PYTHON_DATA_PROTECTION_PATH_DIAGNOSTICS')
        value = json.loads(encoded)
        self.assertEqual(value['failed_ancestor'], str(setup.TARGET.parent))
        self.assertTrue(value['group_or_other_writable']); self.assertTrue(value['root_owned'])
        self.assertFalse(value['qualified']); self.assertFalse(value['symbolic_link'])
        self.assertLessEqual(len(output.getvalue()), 1100)
        output = io.StringIO()
        with redirect_stdout(output):
            setup.protection_diagnostic(setup.components.PathProtectionRefusal(Path('/private/request'), setup.TARGET.parent, info))
            setup.protection_diagnostic(setup.components.PathProtectionRefusal(setup.TARGET, Path('/private/ancestor'), info))
            setup.protection_diagnostic(ValueError('PRIVATE-MESSAGE'))
        self.assertEqual(output.getvalue(), '')

    def test_special_bits_refuse_even_on_hosts_that_strip_directory_setid_bits(self):
        with self.fixture(0o755) as (target, calls, _):
            info = target.lstat()
            for bit in (stat.S_ISUID, stat.S_ISGID, stat.S_ISVTX):
                with self.subTest(bit=bit), mock.patch.object(setup.os, 'fstat',
                        return_value=observed(info, st_mode=info.st_mode | bit)), \
                     self.assertRaisesRegex(setup.u.Refusal, 'reviewed-root-owned-python-data-directory'):
                    setup.harden()
            self.assertEqual(calls, [])

    def test_nonroot_owner_refuses(self):
        with self.fixture(uid=501) as (_, calls, _):
            with self.assertRaisesRegex(setup.u.Refusal, 'reviewed-root-owned-python-data-directory'):
                setup.harden()
            self.assertEqual(calls, [])

    def test_symlink_and_regular_file_refuse_before_command(self):
        for kind in ('symlink', 'file'):
            with self.subTest(kind=kind), self.fixture() as (target, calls, _):
                target.rmdir()
                if kind == 'symlink': target.symlink_to(target.parent, target_is_directory=True)
                else: target.write_bytes(b'PRIVATE-OWNED-DATA')
                with self.assertRaises(OSError): setup.harden()
                self.assertEqual(calls, [])

    def test_unprotected_parent_refuses_before_open(self):
        with self.fixture() as (_, calls, protected), mock.patch.object(setup.os, 'open') as opening:
            protected.side_effect = setup.u.Refusal('owned-parent-refusal')
            with self.assertRaisesRegex(setup.u.Refusal, 'owned-parent-refusal'): setup.harden()
            opening.assert_not_called(); self.assertEqual(calls, [])

    def test_personal_host_and_root_caller_refuse_before_external_io(self):
        for root in (False, True):
            with self.subTest(root=root), self.fixture() as (_, calls, protected), \
                 mock.patch.object(setup.os, 'open') as opening:
                if root: context = mock.patch.object(setup.os, 'geteuid', return_value=0)
                else: context = mock.patch.object(setup.u, 'hosted', side_effect=setup.u.Refusal('hosted-ubuntu-required'))
                with context, self.assertRaises(setup.u.Refusal): setup.harden()
                protected.assert_not_called(); opening.assert_not_called(); self.assertEqual(calls, [])

    def test_failed_or_ineffective_command_never_produces_success(self):
        for status in (0, 1):
            with self.subTest(status=status), self.fixture(command=lambda _: (status, b'PRIVATE-COMMAND-OUTPUT')):
                with self.assertRaises(setup.u.Refusal) as caught: setup.harden()
                self.assertNotIn('PRIVATE', str(caught.exception))

    def test_replaced_directory_during_command_refuses(self):
        def replacing(target):
            target.rename(target.with_name('retired'))
            target.mkdir(mode=0o755)
            return 0, b''
        with self.fixture(command=replacing):
            with self.assertRaisesRegex(setup.u.Refusal, 'stable-protected-python-data-directory-after'):
                setup.harden()

    def test_identity_change_before_command_refuses_and_closes_descriptor(self):
        with self.fixture() as (target, calls, _), mock.patch.object(setup.os, 'close', wraps=os.close) as closing:
            original = target.lstat()
            with mock.patch.object(Path, 'lstat', return_value=observed(original, st_ino=original.st_ino + 1)):
                with self.assertRaisesRegex(setup.u.Refusal, 'stable-python-data-directory-before'): setup.harden()
            self.assertEqual(calls, []); closing.assert_called_once()

    def test_change_to_ownership_after_command_refuses(self):
        with self.fixture() as (target, _, _):
            real_fstat = setup.os.fstat
            count = 0
            def changing(fd):
                nonlocal count
                count += 1
                return observed(real_fstat(fd), st_uid=501 if count == 4 else 0)
            with mock.patch.object(setup.os, 'fstat', side_effect=changing), \
                 self.assertRaisesRegex(setup.u.Refusal, 'stable-protected-python-data-directory-after'):
                setup.harden()
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_run_refuses_on_personal_host_before_reading_candidate_or_report(self):
        with mock.patch.object(setup.u, 'hosted', side_effect=setup.u.Refusal('hosted-ubuntu-required')), \
             mock.patch.object(setup.candidate, 'read') as read, \
             mock.patch.object(setup.os.path, 'lexists') as exists, \
             mock.patch.object(setup, 'harden') as harden:
            with self.assertRaisesRegex(setup.u.Refusal, 'hosted-ubuntu-required'):
                setup.run(Path('candidate'), Path('report'))
            read.assert_not_called(); exists.assert_not_called(); harden.assert_not_called()

    def test_source_closure_includes_setup_and_controls(self):
        for name in ('tools/native-packages/python_data_protection.py', 'tests/host/test_python_data_protection.py'):
            self.assertIn(name, setup.candidate.INPUTS)
            self.assertIn(name, setup.source_inputs())
        self.assertEqual(setup.TARGET, Path('/usr/share/python-babel-localedata/locale-data'))

    def test_receipt_is_private_source_bound_and_clears_no_advisory(self):
        with self.fixture() as (target, _, _), ExitStack() as stack:
            report = target.parent / 'report.json'
            inputs = setup.source_inputs()
            parent = dict(checkout=dict(clean=True, commit='a' * 40), inputs={'owned': 'b' * 64},
                          advisory_status='complete')
            stack.enter_context(mock.patch.object(setup.sys, 'flags', SimpleNamespace(isolated=1)))
            stack.enter_context(mock.patch.object(setup.candidate, 'read', return_value=(b'owned-candidate', parent)))
            stack.enter_context(mock.patch.object(setup.candidate, 'source_inputs', return_value=parent['inputs']))
            stack.enter_context(mock.patch.object(setup.candidate, 'validate_advisory_result'))
            stack.enter_context(mock.patch.object(setup.u, 'command',
                side_effect=lambda args: 'a' * 40 if args[-1] == 'HEAD' else ''))
            value = setup.run(target.parent / 'candidate.json', report)
            self.assertEqual(json.loads(report.read_bytes()), value)
            self.assertEqual(value['inputs'], inputs)
            self.assertEqual(value['checkout'], parent['checkout'])
            self.assertEqual(value['candidate_report_sha256'], setup.u.digest(b'owned-candidate'))
            self.assertTrue(all(value[key] is False for key in setup.FALSE_FLAGS))
            self.assertEqual(value['advisory_findings_cleared'], 0)
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            self.assertEqual(value['schema'], 2)
            self.assertEqual(len(value['directories']), 2)
            self.assertTrue(all(row['protection_complete'] for row in value['directories']))
            with self.assertRaisesRegex(setup.u.Refusal, 'new-python-data-protection-report'):
                setup.run(target.parent / 'candidate.json', report)

    def test_changed_source_after_permission_removal_emits_no_receipt(self):
        with self.fixture() as (target, _, _), ExitStack() as stack:
            report = target.parent / 'report.json'
            parent = dict(checkout=dict(clean=True, commit='a' * 40), inputs={}, advisory_status='complete')
            stack.enter_context(mock.patch.object(setup.sys, 'flags', SimpleNamespace(isolated=1)))
            stack.enter_context(mock.patch.object(setup.candidate, 'read', return_value=(b'owned', parent)))
            stack.enter_context(mock.patch.object(setup.candidate, 'source_inputs', return_value={}))
            stack.enter_context(mock.patch.object(setup.candidate, 'validate_advisory_result'))
            stack.enter_context(mock.patch.object(setup.u, 'command',
                side_effect=lambda args: 'a' * 40 if args[-1] == 'HEAD' else ''))
            stack.enter_context(mock.patch.object(setup, 'source_inputs', side_effect=[{'owned': 'before'}, {'owned': 'after'}]))
            with self.assertRaisesRegex(setup.u.Refusal, 'unchanged-python-data-setup-inputs'):
                setup.run(target.parent / 'candidate.json', report)
            self.assertFalse(report.exists())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)


if __name__ == '__main__':
    unittest.main()
