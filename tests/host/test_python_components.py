"""Actual synthetic metadata, mutation, privacy and collection-boundary controls."""
import copy
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/native-packages'))
spec = importlib.util.spec_from_file_location('python_component_tests', ROOT / 'tools/native-packages/python_components.py')
components = importlib.util.module_from_spec(spec)
spec.loader.exec_module(components)


class PythonComponentTests(unittest.TestCase):
    def metadata(self, root, directory='public_package-1.0.dist-info', name='Public.Package', version='1.0'):
        path = root / directory
        path.mkdir()
        (path / 'METADATA').write_text('Metadata-Version: 2.1\nName: ' + name + '\nVersion: ' + version +
                                     '\n\nPRIVATE-DESCRIPTION-MUST-NOT-ESCAPE\n')
        (path / 'RECORD').write_text('public_package/__init__.py,sha256=observed-record-only,1\n')
        return path

    def test_real_wheel_and_legacy_metadata_are_observed_without_exporting_descriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.metadata(root)
            (root / 'legacy.egg-info').write_text('Name: legacy\nVersion: 2.0\n\nprivate legacy text\n')
            egg = root / 'directory.egg-info'; egg.mkdir()
            (egg / 'PKG-INFO').write_text('Name: directory\nVersion: 3.0\n')
            report = components.scan([root])
            self.assertEqual(report['packages'], {'directory': '3.0', 'legacy': '2.0', 'public-package': '1.0'})
            self.assertEqual(report['metadata']['public-package']['metadata']['sha256'],
                             hashlib.sha256((path / 'METADATA').read_bytes()).hexdigest())
            self.assertEqual(report['metadata']['public-package']['record']['size'], (path / 'RECORD').stat().st_size)
            self.assertNotIn('PRIVATE-DESCRIPTION', json.dumps(report))
            self.assertNotIn('private legacy text', json.dumps(report))
            self.assertIsNone(report['metadata']['legacy']['record'])

    def test_duplicate_normalized_distributions_never_select_the_first_search_path(self):
        with tempfile.TemporaryDirectory() as directory:
            a = Path(directory) / 'a'; b = Path(directory) / 'b'; a.mkdir(); b.mkdir()
            self.metadata(a, name='Public.Package'); self.metadata(b, name='public_package')
            with self.assertRaisesRegex(components.u.Refusal, 'unique-bounded-python-distribution-set'):
                components.scan([a, b])

    def test_missing_duplicate_or_non_public_name_and_version_headers_refuse(self):
        for value in [b'Version: 1\n', b'Name: one\nName: two\nVersion: 1\n',
                      b'Name: one\nVersion: 1\nVersion: 2\n', b'Name: private/path\nVersion: 1\n',
                      b'Name: one\nVersion: private value\n', b'Name: one\nVersion: \xff\n']:
            with self.subTest(value=value), self.assertRaises(components.u.Refusal):
                components.metadata_fields(value)

    def test_metadata_directory_files_and_record_links_or_special_files_refuse(self):
        for target in ['directory', 'metadata', 'record', 'fifo']:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path = self.metadata(root)
                if target == 'directory':
                    moved = root / 'owned'; path.rename(moved); path.symlink_to(moved, target_is_directory=True)
                else:
                    file = path / ('RECORD' if target == 'record' else 'METADATA')
                    file.unlink()
                    if target == 'fifo': os.mkfifo(file)
                    else: file.symlink_to(path / ('METADATA' if target == 'record' else 'RECORD'))
                with self.assertRaises((components.u.Refusal, OSError)):
                    components.scan([root])

    def test_replaced_rewritten_grown_or_truncated_metadata_refuses_and_closes_its_fd(self):
        for operation in ['replace', 'rewrite', 'grow', 'truncate']:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'METADATA'; original = b'Name: public\nVersion: 1.0\n'; path.write_bytes(original)
                opened = []; read = os.read; opening = os.open; changed = False
                def open_file(*args, **kwargs):
                    fd = opening(*args, **kwargs); opened.append(fd); return fd
                def reading(fd, size):
                    nonlocal changed
                    chunk = read(fd, min(size, 8))
                    if not changed:
                        changed = True
                        if operation == 'replace':
                            other = path.with_name('replacement'); other.write_bytes(original); other.replace(path)
                        elif operation == 'rewrite': path.write_bytes(original.replace(b'public', b'PUBLIC'))
                        elif operation == 'grow':
                            with path.open('ab') as f: f.write(b'extra')
                        else: path.write_bytes(original[:8])
                    return chunk
                with mock.patch.object(components.os, 'open', side_effect=open_file), \
                     mock.patch.object(components.os, 'read', side_effect=reading), \
                     self.assertRaises(components.u.Refusal):
                    components.stable_file(path, 1024)
                self.assertEqual(len(opened), 1)
                with self.assertRaises(OSError): os.fstat(opened[0])

    def test_metadata_directory_replacement_cannot_redirect_the_held_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = self.metadata(root); read = os.read; changed = False
            def reading(fd, size):
                nonlocal changed
                value = read(fd, size)
                if not changed:
                    changed = True
                    path.rename(root / 'retired')
                    self.metadata(root, name='replacement')
                return value
            with mock.patch.object(components.os, 'read', side_effect=reading), self.assertRaises(components.u.Refusal):
                components.scan([root])

    def test_file_total_package_and_directory_budgets_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = self.metadata(root)
            for name, value in [('MAX_METADATA', 4), ('MAX_RECORD', 4), ('MAX_TOTAL', 4),
                                ('MAX_PACKAGES', 0), ('MAX_ENTRIES', 0)]:
                with self.subTest(budget=name), mock.patch.object(components, name, value), self.assertRaises(components.u.Refusal):
                    components.scan([root])

    def test_private_noncanonical_or_duplicate_search_paths_and_present_zip_refuse(self):
        for value in ['/home/runner/private', '/tmp/private', '', '/usr/lib/../private', '/usr/lib//private',
                      '/usr/lib/python3.12/private path']:
            with self.subTest(path=value), self.assertRaises(components.u.Refusal): components.public_path(value)
        with self.assertRaises(components.u.Refusal): components.search_paths(('/usr/lib/python3.12', '/usr/lib/python3.12'))
        with mock.patch.object(components.os.path, 'lexists', return_value=True), \
             mock.patch.object(components, 'protected'), mock.patch.object(Path, 'is_dir', return_value=False), \
             self.assertRaisesRegex(components.u.Refusal, 'filesystem-system-python-directory'):
            components.search_paths(('/usr/lib/python312.zip',))

    def test_candidate_python_requires_exact_path_origin_bytes_size_and_typed_elf(self):
        image = dict(sha256='a'*64, size=64, elf=dict(bits=64, flags=0))
        parent = dict(inventory=dict(roots=dict(python=dict(image='/usr/bin/python3.12', origin='ubuntu', **image))))
        executable = Path('/usr/bin/python3.12'); components.join_python(parent, executable, image)
        for key, value in [('sha256', 'b'*64), ('size', 65), ('image', '/usr/local/bin/python3.12'),
                           ('origin', 'repository-build'), ('elf', dict(bits=64, flags=False))]:
            bad = copy.deepcopy(parent); bad['inventory']['roots']['python'][key] = value
            with self.subTest(key=key), self.assertRaises(components.u.Refusal): components.join_python(bad, executable, image)

    def test_host_guard_and_existing_output_precede_candidate_or_metadata_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report'; report.write_bytes(b'preserved')
            with mock.patch.object(components.u, 'hosted', side_effect=components.u.Refusal('hosted-ubuntu-required')), \
                 mock.patch.object(components.candidate, 'read') as read, mock.patch.object(components, 'scan') as scan, \
                 self.assertRaisesRegex(components.u.Refusal, 'hosted-ubuntu-required'):
                components.run(Path('unused'), report)
            read.assert_not_called(); scan.assert_not_called()
            with mock.patch.object(components.u, 'hosted'), mock.patch.object(components.candidate, 'read') as read, \
                 self.assertRaisesRegex(components.u.Refusal, 'new-system-python-component-report'):
                components.run(Path('unused'), report)
            read.assert_not_called(); self.assertEqual(report.read_bytes(), b'preserved')

    def test_nonisolated_or_virtual_environment_refuses_before_candidate_reads(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(components.u, 'hosted'), \
             mock.patch.object(components.sys, 'prefix', '/private/unqualified-python'), \
             mock.patch.object(components.candidate, 'read') as read, \
             self.assertRaisesRegex(components.u.Refusal, 'isolated-system-python-required'):
            components.run(Path('unused'), Path(directory) / 'report')
        read.assert_not_called()

    def test_failure_output_projects_only_a_fixed_category(self):
        with mock.patch.object(sys, 'argv', ['components', '--candidate', 'private-parent', '--report', 'private-report']), \
             mock.patch.object(components, 'run', side_effect=OSError('PRIVATE-CREDENTIAL-PATH')):
            with self.assertRaises(SystemExit), mock.patch.object(sys.stderr, 'write') as write:
                components.main()
            self.assertNotIn('PRIVATE-CREDENTIAL-PATH', ''.join(x.args[0] for x in write.call_args_list))

    def test_unprotected_owner_permissions_and_linked_ancestors_refuse(self):
        for mode, owner in [(stat.S_IFDIR | 0o775, 0), (stat.S_IFDIR | 0o755, 1000),
                            (stat.S_IFLNK | 0o755, 0)]:
            with self.subTest(mode=mode, owner=owner), \
                 mock.patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=mode, st_uid=owner)), \
                 self.assertRaisesRegex(components.u.Refusal, 'protected-system-python-metadata-path'):
                components.protected(Path('/usr/lib/public'))

    def synthetic_run(self, root, report, *, mutate_parent=False, mutate_image=False):
        # This is an owned host fixture for orchestration, never hosted Ubuntu
        # or real ELF/package qualification. Native validators have their own suites.
        head = '1' * 40
        image = dict(sha256='a' * 64, size=64, elf=dict(bits=64, flags=0))
        parent = dict(checkout=dict(clean=True, commit=head), inputs={'parent': 'b' * 64},
                      advisory_status='complete', qualified=False, findings=[{'advisory': 'retained'}],
                      inventory=dict(roots=dict(python=dict(image='/usr/bin/python3.12', origin='ubuntu', **image))))
        raw = json.dumps(parent).encode()
        source = root / 'candidate.json'; source.write_bytes(raw)
        reads = [(raw, parent), (raw + b' ' if mutate_parent else raw, parent)]
        images = [image, dict(image, size=65) if mutate_image else image]
        with ExitStack() as stack:
            replacements = [(components.u, 'hosted', mock.Mock()),
                            (components.sys, 'prefix', '/usr'), (components.sys, 'base_prefix', '/usr'),
                            (components.sys, 'executable', '/usr/bin/python3.12'),
                            (components.sys, 'version_info', (3, 12)),
                            (components.sys, 'flags', SimpleNamespace(isolated=1, ignore_environment=1, no_user_site=1)),
                            (components, 'source_inputs', lambda: {'collector': 'c' * 64}),
                            (components.candidate, 'source_inputs', lambda: parent['inputs']),
                            (components.candidate, 'validate_advisory_result', mock.Mock()),
                            (components, 'search_paths', lambda paths: [dict(path=str(root), kind='directory')]),
                            (components, 'public_path', lambda path: Path(path)),
                            (components, 'protected', mock.Mock())]
            for obj, name, replacement in replacements:
                stack.enter_context(mock.patch.object(obj, name, replacement))
            stack.enter_context(mock.patch.object(components.candidate, 'read', side_effect=reads))
            stack.enter_context(mock.patch.object(components.u, 'image_identity', side_effect=images))
            stack.enter_context(mock.patch.object(components.u, 'command', side_effect=lambda args: head if args[-1] == 'HEAD' else ''))
            return components.run(source, report)

    def test_complete_synthetic_collection_remains_unqualified_and_preserves_advisory_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.metadata(root, name='Scrapy', version='2.17.0')
            report = root / 'result.json'
            value = self.synthetic_run(root, report)
            self.assertEqual(value['packages'], {'scrapy': '2.17.0'})
            self.assertEqual(value['collection_status'], 'complete')
            self.assertEqual(value['advisory_findings_cleared'], 0)
            self.assertTrue(all(value[key] is False for key in components.FALSE_FLAGS))
            self.assertEqual(json.loads(report.read_text()), value)
            self.assertFalse(json.loads((root / 'candidate.json').read_text())['qualified'])
            self.assertEqual(os.stat(report).st_mode & 0o777, 0o600)
            self.assertNotIn('PRIVATE-DESCRIPTION', report.read_text())

    def test_parent_or_interpreter_changes_before_publication_leave_no_report(self):
        for field in ['mutate_parent', 'mutate_image']:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); self.metadata(root); report = root / 'result.json'
                with self.assertRaisesRegex(components.u.Refusal, 'unchanged-system-python-component-inputs'):
                    self.synthetic_run(root, report, **{field: True})
                self.assertFalse(report.exists())


if __name__ == '__main__':
    unittest.main()
