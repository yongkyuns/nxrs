"""Owned-filesystem controls for the fixed disposable-image permission repair."""
from contextlib import ExitStack, contextmanager
import importlib.util
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
    def fixture(self, mode=0o777, uid=0, command=None):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            target = Path(temporary) / 'locale-data'
            target.mkdir(); target.chmod(mode)
            real_fstat, real_lstat = os.fstat, Path.lstat
            calls = []

            def execute(args):
                self.assertEqual(args, ['/usr/bin/sudo', '-n', '/usr/bin/chmod', 'go-w', '--', str(target)])
                calls.append(args)
                if command:
                    return command(target)
                target.chmod(stat.S_IMODE(real_lstat(target).st_mode) & ~0o022)
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
                result = setup.harden()
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
                self.assertEqual(payload.read_bytes(), b'PRIVATE-OWNED-CONTENTS')
                self.assertEqual(result['before_mode'], format(mode, '04o'))
                self.assertIs(result['changed'], mode != 0o755)
                self.assertEqual(len(calls), int(mode != 0o755))
                self.assertEqual(protected.call_args_list, [mock.call(target.parent), mock.call(target)])
                self.assertNotIn('PRIVATE-OWNED-CONTENTS', json.dumps(result))

    def test_other_modes_and_special_bits_refuse_without_privileged_command(self):
        for mode in (0o700, 0o750, 0o705, 0o744, 0o1777):
            with self.subTest(mode=mode), self.fixture(mode) as (_, calls, _):
                with self.assertRaisesRegex(setup.u.Refusal, 'reviewed-root-owned-python-data-directory'):
                    setup.harden()
                self.assertEqual(calls, [])

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
                return observed(real_fstat(fd), st_uid=0 if count == 1 else 501)
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
            self.assertTrue(value['directory']['protection_complete'])
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
