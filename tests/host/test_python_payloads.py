"""Owned filesystem payload/race controls; no installed packages are inspected."""
import copy
from contextlib import ExitStack
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/native-packages'))
import python_payloads as payloads


class PythonPayloadTests(unittest.TestCase):
    def test_protection_diagnostic_uses_only_exact_reviewed_paths_and_fixed_booleans(self):
        requested = Path('/usr/lib/llvm-18/lib/python3/dist-packages/lldb')
        error = payloads.components.PathProtectionRefusal(requested, requested.parent,
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=1000))
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['payloads', '--candidate', 'c', '--metadata', 'm', '--report', 'r']), \
             mock.patch.object(payloads, 'run', side_effect=error), mock.patch('sys.stderr', output):
            with self.assertRaises(SystemExit) as result:payloads.main()
        self.assertEqual(result.exception.code, 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0].removeprefix('PYTHON_PAYLOAD_PATH_DIAGNOSTICS ')), dict(
            requested_path=str(requested), failed_ancestor=str(requested.parent),
            symbolic_link=False, root_owned=False, group_or_other_writable=True))
        self.assertIn('protected-system-python-metadata-path', lines[-1])
        self.assertNotIn('1000', output.getvalue())

    def test_unknown_private_or_unrelated_guard_paths_never_emit_diagnostics(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=1000)
        reviewed = Path('/usr/lib/llvm-18/lib/python3/dist-packages/lldb')
        for requested, ancestor in [(Path('/private/PRIVATE-OWNER'), Path('/private')),
                                    (Path('/usr/lib/UNREVIEWED'), Path('/usr/lib')),
                                    (reviewed, Path('/private/PRIVATE-OWNER'))]:
            error = payloads.components.PathProtectionRefusal(requested, ancestor, info)
            self.assertIsNone(payloads.protection_observation(error))
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['payloads', '--candidate', 'c', '--metadata', 'm', '--report', 'r']), \
                 mock.patch.object(payloads, 'run', side_effect=error), mock.patch('sys.stderr', output):
                with self.assertRaises(SystemExit):payloads.main()
            self.assertNotIn('DIAGNOSTICS', output.getvalue())
            self.assertNotIn('PRIVATE', output.getvalue())
            self.assertNotIn('UNREVIEWED', output.getvalue())

    def scan(self, paths, **kw):
        return payloads.scan(paths, owner=os.getuid(), **kw)

    def test_real_payloads_are_hashed_without_importing_or_exporting_their_contents(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); package = root / 'package'; package.mkdir()
            private = b'PRIVATE-CONTENT-MUST-NOT-ESCAPE\nraise RuntimeError("must not import")\n'
            (package / '__init__.py').write_bytes(private); (package / 'empty.py').write_bytes(b'')
            value = self.scan([root])
            self.assertEqual(value['payload_bytes'], len(private))
            self.assertEqual(value['entries'][str(package / '__init__.py')]['sha256'], hashlib.sha256(private).hexdigest())
            self.assertEqual(value['entries'][str(package / 'empty.py')]['size'], 0)
            self.assertNotIn('PRIVATE-CONTENT', json.dumps(value))
            self.assertEqual(value, self.scan([root]))

    def test_nested_startup_directories_are_covered_once(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); nested = root / 'lib-dynload'; nested.mkdir(); (nested / 'module.so').write_bytes(b'one')
            value = self.scan([nested, root])
            self.assertEqual(value['roots'], [str(root)]); self.assertEqual(value['payload_bytes'], 3)
            self.assertEqual(len(value['entries']), 3)

    def test_internal_links_record_targets_without_duplicate_reads_or_recursive_walks(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); package = root / 'package'; package.mkdir(); (package / 'm.py').write_bytes(b'code')
            (root / 'alias').symlink_to('package'); (package / 'again').symlink_to('../package')
            (root / 'file.py').symlink_to('package/m.py')
            value = self.scan([root])
            self.assertEqual(value['payload_bytes'], 4)
            self.assertEqual(value['entries'][str(root / 'file.py')]['resolved'], str(package / 'm.py'))
            self.assertEqual(value['entries'][str(package / 'again')]['kind'], 'link')

    def test_outside_and_broken_links_refuse(self):
        with tempfile.TemporaryDirectory() as name:
            outer = Path(name).resolve(); root = outer / 'root'; root.mkdir(); (outer / 'secret').write_bytes(b'PRIVATE')
            link = root / 'link'; link.symlink_to('../secret')
            with self.assertRaisesRegex(payloads.u.Refusal, 'python-payload-link-escape'):self.scan([root])
            link.unlink(); link.symlink_to('missing')
            with self.assertRaisesRegex(payloads.UnreviewedLinks, 'python-payload-link-target-unavailable'):
                self.scan([root])

    def test_reviewed_external_directory_is_scanned_once_without_adjacent_access(self):
        with tempfile.TemporaryDirectory() as name:
            outer=Path(name).resolve(); root=outer/'startup';root.mkdir()
            data=outer/'data';data.mkdir();(data/'module.py').write_bytes(b'public')
            (outer/'PRIVATE-ADJACENT').write_bytes(b'must not read')
            first=root/'a';second=root/'b';first.symlink_to(data);second.symlink_to(data)
            (data/'self').symlink_to(data)
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_DIRECTORIES',{first:data,second:data}), \
                 mock.patch.object(payloads.components,'protected') as protected:
                value=self.scan([root])
            self.assertEqual(value['external_directories'],[str(data)])
            self.assertEqual(value['payload_bytes'],6)
            self.assertEqual(set(value['entries']),{str(root),str(first),str(second),str(data),str(data/'module.py'),str(data/'self')})
            self.assertEqual(protected.call_args_list,[mock.call(data),mock.call(data)])
            self.assertNotIn('PRIVATE',json.dumps(value))

    def test_reviewed_external_directory_cannot_grant_adjacent_or_unreviewed_targets(self):
        with tempfile.TemporaryDirectory() as name:
            outer=Path(name).resolve();root=outer/'startup';root.mkdir();data=outer/'data';data.mkdir()
            private=outer/'PRIVATE';private.write_bytes(b'must not read');(data/'escape').symlink_to(private)
            link=root/'data';link.symlink_to(data)
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_DIRECTORIES',{link:data}), \
                 mock.patch.object(payloads.components,'protected'), \
                 mock.patch.object(payloads.os,'read',side_effect=AssertionError('must not read external bytes')):
                with self.assertRaises(payloads.UnreviewedLinks) as caught:self.scan([root])
            self.assertEqual(len(caught.exception.observations),1)
            self.assertNotIn('PRIVATE',json.dumps(caught.exception.observations))
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_DIRECTORIES',{link:outer}):
                with self.assertRaisesRegex(payloads.UnreviewedLinks,'python-payload-link-escape'):self.scan([root])

    def test_nested_reviewed_directories_do_not_duplicate_files(self):
        with tempfile.TemporaryDirectory() as name:
            outer=Path(name).resolve();root=outer/'startup';root.mkdir();data=outer/'data';data.mkdir()
            nested=data/'nested';nested.mkdir();(nested/'m.py').write_bytes(b'one')
            first=root/'a';second=root/'b';first.symlink_to(nested);second.symlink_to(data)
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_DIRECTORIES',{first:nested,second:data}), \
                 mock.patch.object(payloads.components,'protected'):
                value=self.scan([root])
            self.assertEqual(value['payload_bytes'],3)
            self.assertEqual(value['external_directories'],[str(data),str(nested)])

    def test_reviewed_directory_requires_protection_and_directory_type(self):
        with tempfile.TemporaryDirectory() as name:
            outer=Path(name).resolve();root=outer/'startup';root.mkdir();data=outer/'data';data.mkdir()
            link=root/'data';link.symlink_to(data)
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_DIRECTORIES',{link:data}), \
                 mock.patch.object(payloads.components,'protected'):
                data.chmod(0o777)
                with self.assertRaisesRegex(payloads.u.Refusal,'protected-python-payload-owner-mode'):self.scan([root])
                data.chmod(0o755);data.rmdir();data.write_bytes(b'not a directory')
                with self.assertRaises(NotADirectoryError):self.scan([root])

    def test_reviewed_directory_ancestor_guard_precedes_external_walk(self):
        with tempfile.TemporaryDirectory() as name:
            outer=Path(name).resolve();root=outer/'startup';root.mkdir();data=outer/'data';data.mkdir()
            link=root/'data';link.symlink_to(data);original=os.scandir;walked=[]
            def scanning(fd):walked.append(os.fstat(fd).st_ino);return original(fd)
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_DIRECTORIES',{link:data}), \
                 mock.patch.object(payloads.components,'protected',side_effect=payloads.u.Refusal('protected-ancestor')), \
                 mock.patch.object(payloads.os,'scandir',side_effect=scanning):
                with self.assertRaisesRegex(payloads.u.Refusal,'protected-ancestor'):self.scan([root])
            self.assertNotIn(data.stat().st_ino,walked)

    def test_replaced_external_directory_cannot_publish_old_contents(self):
        with tempfile.TemporaryDirectory() as name:
            outer=Path(name).resolve();root=outer/'startup';root.mkdir();data=outer/'data';data.mkdir()
            (data/'m.py').write_bytes(b'old');link=root/'data';link.symlink_to(data)
            original=os.scandir;changed=False;inode=data.stat().st_ino
            class Children:
                def __init__(self,fd):self.value=original(fd);self.target=os.fstat(fd).st_ino==inode
                def __enter__(self):return self.value.__enter__()
                def __exit__(self,*args):
                    nonlocal changed
                    value=self.value.__exit__(*args)
                    if self.target and not changed:
                        changed=True;data.rename(outer/'retired');data.mkdir();(data/'m.py').write_bytes(b'new')
                    return value
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_DIRECTORIES',{link:data}), \
                 mock.patch.object(payloads.components,'protected'),mock.patch.object(payloads.os,'scandir',side_effect=Children):
                with self.assertRaisesRegex(payloads.u.Refusal,'stable-python-payload-directory'):self.scan([root])
            self.assertTrue(changed)

    def test_large_file_limit_is_specific_to_reviewed_external_file_and_total_budget(self):
        with tempfile.TemporaryDirectory() as name:
            outer=Path(name).resolve();root=outer/'startup';root.mkdir();target=outer/'large';target.write_bytes(b'abc')
            link=root/'large';link.symlink_to(target)
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_LINKS',{link:target}), \
                 mock.patch.object(payloads.components,'protected'),mock.patch.object(payloads,'MAX_FILE',1):
                with mock.patch.object(payloads,'REVIEWED_FILE_LIMITS',{target:3}):
                    self.assertEqual(self.scan([root])['payload_bytes'],3)
                    with mock.patch.object(payloads,'MAX_TOTAL',2):
                        with self.assertRaisesRegex(payloads.u.Refusal,'python-payload-total-budget'):self.scan([root])
                for limits in ({},{outer/'other':3}):
                    with mock.patch.object(payloads,'REVIEWED_FILE_LIMITS',limits):
                        with self.assertRaisesRegex(payloads.u.Refusal,'bounded-regular-python-payload'):self.scan([root])
                (root/'ordinary').write_bytes(b'abc')
                with mock.patch.object(payloads,'REVIEWED_FILE_LIMITS',{root/'ordinary':3,target:3}):
                    with self.assertRaisesRegex(payloads.u.Refusal,'bounded-regular-python-payload'):self.scan([root])

    def test_missing_reviewed_target_still_refuses_and_has_no_resolved_path_claim(self):
        with tempfile.TemporaryDirectory() as name:
            outer=Path(name).resolve();root=outer/'startup';root.mkdir();missing=outer/'PRIVATE-MISSING'
            link=root/'link';link.symlink_to(missing)
            with mock.patch.object(payloads,'REVIEWED_EXTERNAL_LINKS',{link:missing}):
                with self.assertRaisesRegex(payloads.UnreviewedLinks,'python-payload-link-target-unavailable') as caught:
                    self.scan([root])
            row=caught.exception.observations[0]
            self.assertEqual(row['resolution'],'missing');self.assertNotIn('resolved_sha256',row)
            self.assertNotIn('PRIVATE',json.dumps(row))

    def test_cli_missing_link_diagnostic_does_not_export_target_or_invent_resolution(self):
        row=payloads.link_observation(Path('/usr/lib/python3/dist-packages/lldb/missing.so'),b'/private/PRIVATE-TARGET')
        error=io.StringIO()
        with mock.patch.object(sys,'argv',['payloads','--candidate','c','--metadata','m','--report','r']), \
             mock.patch.object(payloads,'run',side_effect=payloads.UnreviewedLinks([row])),mock.patch('sys.stderr',error):
            with self.assertRaises(SystemExit) as status:payloads.main()
        text=error.getvalue();self.assertEqual(status.exception.code,1)
        self.assertIn('python-payload-link-target-unavailable',text)
        self.assertIn('"resolution":"missing"',text);self.assertNotIn('resolved_sha256',text)
        self.assertNotIn('PRIVATE',text);self.assertNotIn('/private/',text)

    def test_all_bounded_unreviewed_links_are_observed_without_reading_external_bytes(self):
        with tempfile.TemporaryDirectory() as name:
            outer = Path(name).resolve(); root = outer / 'startup'; root.mkdir()
            target = outer / 'PRIVATE'; target.write_bytes(b'PRIVATE CONTENT')
            (root/'first').symlink_to(target); (root/'second').symlink_to(target)
            with mock.patch.object(payloads.os, 'read', side_effect=AssertionError('external bytes must not be read')):
                with self.assertRaises(payloads.UnreviewedLinks) as caught:self.scan([root])
            rows = caught.exception.observations
            self.assertEqual(len(rows), 2)
            self.assertEqual(set(rows[0]), {'source_sha256','source_bytes','target_sha256','target_bytes','resolved_sha256'})
            self.assertNotIn('PRIVATE', json.dumps(rows));self.assertNotIn(str(outer), json.dumps(rows))
            for i in range(31):(root/('extra'+str(i))).symlink_to(target)
            with self.assertRaisesRegex(payloads.u.Refusal, 'unreviewed-python-links-budget'):self.scan([root])

    def test_cli_link_diagnostics_export_only_public_source_and_target_hashes(self):
        observation = payloads.link_observation(Path('/usr/lib/python3.12/public.py'),
            b'/private/PRIVATE-OWNER/private.py',Path('/private/PRIVATE-OWNER/private.py'))
        error = io.StringIO()
        with mock.patch.object(sys,'argv',['payloads','--candidate','c','--metadata','m','--report','r']), \
             mock.patch.object(payloads,'run',side_effect=payloads.UnreviewedLinks([observation])), \
             mock.patch('sys.stderr',error):
            with self.assertRaises(SystemExit) as status:payloads.main()
        self.assertEqual(status.exception.code, 1)
        text=error.getvalue();self.assertIn('/usr/lib/python3.12/public.py',text)
        self.assertNotIn('PRIVATE',text);self.assertNotIn('/private/',text)
        self.assertIn('python-payload-link-escape',text)

    def test_link_text_cannot_export_private_names_when_final_target_is_internal(self):
        with tempfile.TemporaryDirectory() as name:
            outer = Path(name).resolve(); root = outer / 'public'; root.mkdir()
            (outer / 'PRIVATE-BUILD-OWNER').mkdir(); (root / 'payload.py').write_bytes(b'public code')
            target = '../PRIVATE-BUILD-OWNER/../public/payload.py'
            (root / 'alias.py').symlink_to(target)
            value = self.scan([root]); link = value['entries'][str(root / 'alias.py')]
            self.assertNotIn('PRIVATE-BUILD-OWNER', json.dumps(value))
            self.assertNotIn('target', link)
            self.assertEqual(link['target_sha256'], hashlib.sha256(os.fsencode(target)).hexdigest())
            self.assertEqual(link['target_bytes'], len(os.fsencode(target)))
            self.assertEqual(link['resolved'], str(root / 'payload.py'))

    def test_exact_reviewed_external_startup_hook_is_hashed_without_traversing_its_directory(self):
        with tempfile.TemporaryDirectory() as name:
            outer = Path(name).resolve(); root = outer / 'startup'; root.mkdir()
            config = outer / 'config'; config.mkdir(); hook = config / 'sitecustomize.py'
            secret = b'PRIVATE STARTUP HOOK\n'; hook.write_bytes(secret)
            (config / 'unrelated-private-file').write_bytes(b'must not read')
            link = root / 'sitecustomize.py'; link.symlink_to(hook)
            with mock.patch.object(payloads, 'REVIEWED_EXTERNAL_LINKS', {link:hook}), \
                 mock.patch.object(payloads.components, 'protected') as protected:
                value = self.scan([root])
            self.assertEqual(value['external_files'], [str(hook)])
            self.assertEqual(value['entries'][str(hook)]['sha256'], hashlib.sha256(secret).hexdigest())
            self.assertEqual(value['payload_bytes'], len(secret))
            self.assertEqual(set(value['entries']), {str(root),str(link),str(hook)})
            self.assertNotIn('PRIVATE', json.dumps(value))
            self.assertEqual(protected.call_args_list, [mock.call(hook),mock.call(hook)])

    def test_external_review_is_specific_to_both_link_and_canonical_target(self):
        with tempfile.TemporaryDirectory() as name:
            outer = Path(name).resolve(); root = outer / 'startup'; root.mkdir()
            hook = outer / 'sitecustomize.py'; hook.write_bytes(b'x')
            link = root / 'sitecustomize.py'; link.symlink_to(hook)
            for review in ({root/'other.py':hook}, {link:outer/'other.py'}):
                with self.subTest(review=review), mock.patch.object(payloads, 'REVIEWED_EXTERNAL_LINKS', review):
                    with self.assertRaisesRegex(payloads.u.Refusal, 'python-payload-link-escape'):
                        self.scan([root])
            alias = outer / 'alias.py'; alias.symlink_to(hook)
            link.unlink(); link.symlink_to(alias)
            with mock.patch.object(payloads, 'REVIEWED_EXTERNAL_LINKS', {link:alias}):
                with self.assertRaisesRegex(payloads.u.Refusal, 'python-payload-link-escape'):
                    self.scan([root])

    def test_reviewed_external_file_and_parent_still_require_protection_and_regular_file(self):
        with tempfile.TemporaryDirectory() as name:
            outer = Path(name).resolve(); root = outer / 'startup'; root.mkdir()
            config = outer / 'config'; config.mkdir(); hook = config / 'sitecustomize.py'; hook.write_bytes(b'x')
            link = root / 'sitecustomize.py'; link.symlink_to(hook)
            with mock.patch.object(payloads, 'REVIEWED_EXTERNAL_LINKS', {link:hook}), \
                 mock.patch.object(payloads.components, 'protected'):
                for selected in (hook, config):
                    previous = selected.stat().st_mode & 0o777; selected.chmod(0o777)
                    try:
                        with self.assertRaisesRegex(payloads.u.Refusal, 'protected-python-payload-owner-mode'):
                            self.scan([root])
                    finally:selected.chmod(previous)
                hook.unlink(); os.mkfifo(hook)
                with self.assertRaisesRegex(payloads.u.Refusal, 'bounded-regular-python-payload'):
                    self.scan([root])

    def test_external_ancestor_guard_refuses_before_opening_the_external_parent(self):
        with tempfile.TemporaryDirectory() as name:
            outer = Path(name).resolve(); root = outer / 'startup'; root.mkdir()
            config = outer / 'config'; config.mkdir(); hook = config / 'sitecustomize.py'; hook.write_bytes(b'x')
            link = root / 'sitecustomize.py'; link.symlink_to(hook)
            original = os.open; opened = []
            def opening(path, *args, **kw):
                opened.append(path); return original(path, *args, **kw)
            with mock.patch.object(payloads, 'REVIEWED_EXTERNAL_LINKS', {link:hook}), \
                 mock.patch.object(payloads.components, 'protected', side_effect=payloads.u.Refusal('protected-ancestor')), \
                 mock.patch.object(payloads.os, 'open', side_effect=opening):
                with self.assertRaisesRegex(payloads.u.Refusal, 'protected-ancestor'):self.scan([root])
            self.assertNotIn(config, opened)

    def test_replaced_external_parent_cannot_publish_the_old_file_observation(self):
        with tempfile.TemporaryDirectory() as name:
            outer = Path(name).resolve(); root = outer / 'startup'; root.mkdir()
            config = outer / 'config'; config.mkdir(); hook = config / 'sitecustomize.py'; hook.write_bytes(b'x')
            link = root / 'sitecustomize.py'; link.symlink_to(hook)
            original = os.read; changed = False
            def reading(fd, count):
                nonlocal changed
                raw = original(fd, count)
                if not raw and not changed:
                    changed = True; config.rename(outer/'retired'); config.mkdir(); hook.write_bytes(b'x')
                return raw
            with mock.patch.object(payloads, 'REVIEWED_EXTERNAL_LINKS', {link:hook}), \
                 mock.patch.object(payloads.components, 'protected'), \
                 mock.patch.object(payloads.os, 'read', side_effect=reading):
                with self.assertRaisesRegex(payloads.u.Refusal, 'stable-external-python-payload-parent'):
                    self.scan([root])
            self.assertTrue(changed)

    def test_root_alias_duplicate_relative_and_empty_roots_refuse(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); alias = root / 'alias'; alias.symlink_to(root)
            for paths in ([], [Path('relative')], [root, root], [alias]):
                with self.subTest(paths=paths), self.assertRaises(payloads.u.Refusal):self.scan(paths)

    def test_fifo_refuses_without_reading_or_waiting(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); os.mkfifo(root / 'pipe')
            with mock.patch.object(payloads.os, 'read', side_effect=AssertionError('must not read special files')):
                with self.assertRaisesRegex(payloads.u.Refusal, 'regular-python-payload-entry-required'):self.scan([root])

    def test_wrong_owner_and_writable_directory_or_file_refuse(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); path = root / 'module.py'; path.write_bytes(b'x')
            with self.assertRaises(payloads.u.Refusal):payloads.scan([root], owner=os.getuid() + 1)
            for selected in (root, path):
                previous = selected.stat().st_mode & 0o777; selected.chmod(0o777 if selected == root else 0o666)
                try:
                    with self.assertRaisesRegex(payloads.u.Refusal, 'protected-python-payload-owner-mode'):self.scan([root])
                finally:selected.chmod(previous)

    def test_file_growth_during_read_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); path = root / 'module.py'; path.write_bytes(b'x' * 131072)
            original = os.read; changed = False
            def reading(fd, count):
                nonlocal changed
                raw = original(fd, count)
                if raw and not changed:
                    changed = True
                    with path.open('ab') as out:out.write(b'changed')
                return raw
            with mock.patch.object(payloads.os, 'read', side_effect=reading):
                with self.assertRaises(payloads.u.Refusal):self.scan([root])
            self.assertTrue(changed)

    def test_replaced_file_after_read_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); path = root / 'module.py'; path.write_bytes(b'old')
            original = os.read; changed = False
            def reading(fd, count):
                nonlocal changed
                raw = original(fd, count)
                if not raw and not changed:
                    changed = True; path.rename(root / 'retired'); path.write_bytes(b'new')
                return raw
            with mock.patch.object(payloads.os, 'read', side_effect=reading):
                with self.assertRaisesRegex(payloads.u.Refusal, 'stable-python-payload-file'):self.scan([root])

    def test_link_replacement_during_resolution_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); (root / 'a').write_bytes(b'a'); (root / 'b').write_bytes(b'b')
            link = root / 'link'; link.symlink_to('a'); original = os.readlink; changed = False
            def reading(path, *, dir_fd=None):
                nonlocal changed
                value = original(path, dir_fd=dir_fd)
                if dir_fd is not None and not changed:
                    changed = True; link.unlink(); link.symlink_to('b')
                return value
            with mock.patch.object(payloads.os, 'readlink', side_effect=reading):
                with self.assertRaisesRegex(payloads.u.Refusal, 'stable-python-payload-link'):self.scan([root])

    def test_entry_file_total_depth_and_deadline_bounds_refuse(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); (root / 'a').write_bytes(b'ab'); (root / 'b').write_bytes(b'cd')
            for constant, value in [('MAX_ENTRIES', 1), ('MAX_FILE', 1), ('MAX_TOTAL', 3)]:
                with self.subTest(constant=constant), mock.patch.object(payloads, constant, value):
                    with self.assertRaises(payloads.u.Refusal):self.scan([root])
            (root / 'nested').mkdir()
            with mock.patch.object(payloads, 'MAX_DEPTH', 0):
                with self.assertRaisesRegex(payloads.u.Refusal, 'python-payload-depth-budget'):self.scan([root])
            with self.assertRaisesRegex(payloads.u.Refusal, 'python-payload-deadline'):self.scan([root], deadline=0)

    def test_directory_addition_after_enumeration_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); (root / 'old.py').write_bytes(b'old')
            original = os.scandir; changed = False
            class Children:
                def __init__(self, fd):self.value=original(fd)
                def __enter__(self):return self.value.__enter__()
                def __exit__(self, *args):
                    nonlocal changed
                    result=self.value.__exit__(*args)
                    if not changed:changed=True; (root / 'new.py').write_bytes(b'new')
                    return result
            with mock.patch.object(payloads.os, 'scandir', side_effect=Children):
                with self.assertRaisesRegex(payloads.u.Refusal, 'stable-python-payload-directory'):self.scan([root])

    def test_ambiguous_path_names_and_cyclic_links_refuse(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); path=root/'private\nname.py'; path.write_bytes(b'not exported')
            with self.assertRaisesRegex(payloads.u.Refusal, 'python-payload-entry-name'):self.scan([root])
            path.unlink(); (root/'a').symlink_to('b'); (root/'b').symlink_to('a')
            with self.assertRaises((RuntimeError, OSError)):self.scan([root])

    def test_all_explicit_descriptors_close_after_refusal(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); (root / 'file').write_bytes(b'ab')
            opening = os.open; closing = os.close; opened = []; closed = []
            def acquire(*args, **kw):
                fd = opening(*args, **kw); opened.append(fd); return fd
            def release(fd):closed.append(fd); return closing(fd)
            with mock.patch.object(payloads.os, 'open', side_effect=acquire), mock.patch.object(payloads.os, 'close', side_effect=release), mock.patch.object(payloads, 'MAX_FILE', 1):
                with self.assertRaises(payloads.u.Refusal):self.scan([root])
            self.assertCountEqual(opened, closed)

    def test_host_guard_precedes_reads_commands_and_report_creation(self):
        with tempfile.TemporaryDirectory() as name, mock.patch.object(payloads.u, 'hosted', side_effect=payloads.u.Refusal('host-only')), mock.patch.object(payloads.components.candidate, 'read') as read, mock.patch.object(payloads.u, 'command') as command:
            root = Path(name)
            with self.assertRaisesRegex(payloads.u.Refusal, 'host-only'):payloads.run(root / 'candidate', root / 'metadata', root / 'report')
            read.assert_not_called(); command.assert_not_called(); self.assertEqual(list(root.iterdir()), [])

    def execute(self, root, change=None):
        package = root / 'owned-1.dist-info'; package.mkdir(); (package / 'METADATA').write_bytes(b'Name: owned\nVersion: 1\n\nPRIVATE DESCRIPTION\n')
        (root / 'owned.py').write_bytes(b'PRIVATE PAYLOAD\n')
        observed = payloads.components.scan([root]); image = dict(sha256='b'*64, size=256, elf={'bits':64})
        head = 'a'*40; inputs = {'candidate-source':'c'*64}; own_inputs = {'payload-source':'d'*64}; meta_inputs = {'metadata-source':'e'*64}
        parent = dict(checkout=dict(clean=True, commit=head), inputs=inputs, advisory_status='complete',
                      inventory=dict(roots=dict(python=dict(origin='ubuntu', image='/usr/bin/python3.12', **image))))
        candidate = root / 'candidate.json'; candidate.write_text(json.dumps(parent)); raw = candidate.read_bytes()
        metadata = dict(schema=3, scope=payloads.components.SCOPE, checkout=parent['checkout'], inputs=meta_inputs,
                        collection_status='complete', candidate_report_sha256=payloads.u.digest(raw), python_image=image,
                        startup_search_paths=[dict(path=str(root), kind='directory')], isolated=True,
                        advisory_findings_cleared=0, **observed, **dict.fromkeys(payloads.components.FALSE_FLAGS, False))
        if change == 'metadata-parent':metadata['candidate_report_sha256']='f'*64
        if change == 'metadata-isolation':metadata['isolated']=False
        if change == 'metadata-image':metadata['python_image']=dict(image, sha256='f'*64)
        if change == 'metadata-claims':metadata['component_absence_proved']=True
        if change == 'metadata-schema':metadata['schema']=3.0
        meta_path = root / 'metadata.json'; meta_path.write_text(json.dumps(metadata)); report = root / 'report.json'
        # Parent receipts are outside the production startup directories. Move
        # them to another owned fixture directory before the actual file scans.
        with tempfile.TemporaryDirectory() as parents:
            cpath = Path(parents) / 'candidate'; mpath = Path(parents) / 'metadata'
            candidate.rename(cpath); meta_path.rename(mpath)
            real_scan = payloads.scan; calls = 0
            def scan(paths, **kw):
                nonlocal calls
                calls += 1
                if change == 'payload-changed' and calls == 2:(root / 'owned.py').write_bytes(b'changed')
                return real_scan(paths, owner=os.getuid(), **kw)
            with ExitStack() as stack:
                for obj, field, replacement in [
                    (payloads.u, 'hosted', mock.Mock()), (payloads.u, 'image_identity', mock.Mock(return_value=image)),
                    (payloads.u, 'command', mock.Mock(side_effect=lambda args:head if 'rev-parse' in args else '')),
                    (payloads.components.candidate, 'source_inputs', mock.Mock(return_value=inputs)),
                    (payloads.components.candidate, 'validate_advisory_result', mock.Mock()),
                    (payloads, 'source_inputs', mock.Mock(return_value=own_inputs)),
                    (payloads.components, 'source_inputs', mock.Mock(return_value=meta_inputs)),
                    (payloads.components, 'search_paths', mock.Mock(return_value=metadata['startup_search_paths'])),
                    (payloads, 'scan', mock.Mock(side_effect=scan))]:stack.enter_context(mock.patch.object(obj, field, replacement))
                for field, value in [('prefix','/usr'), ('base_prefix','/usr'), ('executable','/usr/bin/python3.12')]:stack.enter_context(mock.patch.object(payloads.sys, field, value))
                stack.enter_context(mock.patch.object(payloads.sys, 'flags', SimpleNamespace(isolated=1, ignore_environment=1, no_user_site=1)))
                if change == 'report-budget':stack.enter_context(mock.patch.object(payloads, 'MAX_REPORT', 1))
                if change:
                    with self.assertRaises(payloads.u.Refusal):payloads.run(cpath, mpath, report)
                    self.assertFalse(report.exists()); return
                result = payloads.run(cpath, mpath, report)
                self.assertEqual(report.stat().st_mode & 0o777, 0o600)
                self.assertTrue(all(result[k] is False for k in payloads.FALSE_FLAGS))
                self.assertEqual(result['advisory_findings_cleared'], 0)
                self.assertNotIn('PRIVATE', report.read_text())
                self.assertEqual(result['collection_status'], 'complete')

    def test_synthetic_joined_collection_keeps_all_authentication_and_advisory_claims_false(self):
        with tempfile.TemporaryDirectory() as name:self.execute(Path(name).resolve())

    def test_changed_parents_isolation_image_and_payload_cannot_publish_report(self):
        for change in ('metadata-parent', 'metadata-isolation', 'metadata-image', 'metadata-claims', 'metadata-schema', 'payload-changed', 'report-budget'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as name:self.execute(Path(name).resolve(), change)

    def test_cli_never_exports_exception_text(self):
        error = io.StringIO()
        with mock.patch.object(sys, 'argv', ['payloads', '--candidate','c','--metadata','m','--report','r']), mock.patch.object(payloads, 'run', side_effect=OSError('PRIVATE-PATH-AND-CONTENT')), mock.patch('sys.stderr', error):
            with self.assertRaises(SystemExit) as status:payloads.main()
        self.assertEqual(status.exception.code, 1)
        self.assertNotIn('PRIVATE', error.getvalue()); self.assertIn('(OSError)', error.getvalue())


if __name__ == '__main__':unittest.main()
