"""Committed guest-source identity, privacy, normalization and refusal controls."""
import copy,os,subprocess,sys,tarfile,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import guest_source as g

class GuestSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.git('init','-q');(self.repo/'app').mkdir();(self.repo/'app/main.py').write_text('print("owned source")\n')
        (self.repo/'tools').mkdir();(self.repo/'tools/run').write_text('#!/bin/sh\nexit 0\n');(self.repo/'tools/run').chmod(0o755)
        (self.repo/'.gitignore').write_text('private/\n');self.commit()

    def git(self,*args):
        return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','-C',str(self.repo),*args],stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git('add','-A');self.git('-c','user.name=Source Fixture','-c','user.email=source@example.invalid','commit','-qm','owned source')
        self.head=self.git('rev-parse','HEAD')

    def snapshot(self,name='source.tar'):return g.snapshot(self.repo,self.root/name,self.head)

    def test_exact_committed_blobs_exclude_git_credentials_and_ignored_private_state(self):
        marker='PRIVATE_SYNTHETIC_MARKER_ONLY'
        (self.repo/'private').mkdir();(self.repo/'private/key').write_text(marker)
        with (self.repo/'.git/config').open('a') as output:output.write('\n[fixture]\n secret = '+marker+'\n')
        manifest=self.snapshot();raw=(self.root/'source.tar').read_bytes()
        self.assertEqual(set(manifest['entries']),{'.gitignore','app/main.py','tools/run'})
        self.assertNotIn(marker.encode(),raw);self.assertFalse(manifest['git_configuration_copied']);self.assertFalse(manifest['gitlink_contents_copied'])
        self.assertEqual(manifest['head'],self.head);g.verify_archive(self.root/'source.tar',manifest)

    def test_archive_is_reproducible_across_working_tree_mtime_changes(self):
        first=self.snapshot();os.utime(self.repo/'app/main.py',(1,1));second=self.snapshot('second.tar')
        self.assertEqual(first,second);self.assertEqual((self.root/'source.tar').read_bytes(),(self.root/'second.tar').read_bytes())

    def test_dirty_source_and_hidden_tracked_changes_refuse_without_partial_archive(self):
        path=self.repo/'app/main.py';path.write_text('changed source\n')
        with self.assertRaisesRegex(g.p.u.Refusal,'clean-source-snapshot'):self.snapshot()
        self.assertFalse((self.root/'source.tar').exists())
        self.git('update-index','--assume-unchanged','app/main.py')
        self.assertEqual(self.git('status','--porcelain'),'')
        with self.assertRaisesRegex(g.p.u.Refusal,'exact-committed-source-blob'):self.snapshot()
        self.assertFalse((self.root/'source.tar').exists())

    def test_internal_symlinks_are_bound_and_gitlinks_are_explicitly_not_copied(self):
        (self.repo/'external').mkdir();(self.repo/'external/apps').symlink_to('../sdk/apps')
        (self.repo/'sdk/apps').mkdir(parents=True)
        self.git('update-index','--add','--cacheinfo','160000,'+self.head+',sdk/apps')
        self.commit();manifest=self.snapshot()
        self.assertEqual(set(manifest['gitlinks']),{'sdk/apps'})
        self.assertEqual(manifest['entries']['external/apps']['mode'],'120000')
        destination=self.root/'view';destination.mkdir()
        with tarfile.open(self.root/'source.tar') as archive:archive.extractall(destination,filter='data')
        self.assertEqual(os.readlink(destination/'external/apps'),'../sdk/apps')
        self.assertFalse((destination/'sdk/apps').exists())
        g.verify_tree(destination,manifest,(os.getuid(),os.getgid()))

    def test_absolute_escaping_or_git_metadata_links_refuse_before_copying_targets(self):
        for target in ('/outside/private-key','../outside-private-key','.git/config'):
            link=self.repo/'alias';link.unlink(missing_ok=True);link.symlink_to(target);self.commit()
            with self.subTest(target=target),self.assertRaises(g.p.u.Refusal):self.snapshot()
            self.assertFalse((self.root/'source.tar').exists())

    def test_lexically_internal_symlink_chain_cannot_resolve_outside_the_source_root(self):
        (self.repo/'sub/deep').mkdir(parents=True);(self.repo/'stay').mkdir()
        (self.repo/'sub/link').symlink_to('../stay')
        (self.repo/'sub/deep/alias').symlink_to('../link/../../outside-private')
        self.commit()
        # Both individual link targets pass the lexical check. Their actual
        # composition leaves the root and must fail before any target is read.
        g.target('sub/link',b'../stay');g.target('sub/deep/alias',b'../link/../../outside-private')
        with self.assertRaisesRegex(g.p.u.Refusal,'resolved-source-link-escapes-root'):self.snapshot()
        self.assertFalse((self.root/'source.tar').exists())

    def test_changed_archive_and_extra_changed_or_writable_staged_source_refuse(self):
        manifest=self.snapshot();archive=self.root/'source.tar';original=archive.read_bytes()
        archive.write_bytes(original[:-1]+bytes([original[-1]^1]))
        with self.assertRaisesRegex(g.p.u.Refusal,'unchanged-source-archive'):g.verify_archive(archive,manifest)
        archive.write_bytes(original);destination=self.root/'view';destination.mkdir()
        with tarfile.open(archive) as source:source.extractall(destination,filter='data')
        owner=(os.getuid(),os.getgid());g.verify_tree(destination,manifest,owner)
        (destination/'extra').write_text('extra')
        with self.assertRaisesRegex(g.p.u.Refusal,'no-extra-staged-source'):g.verify_tree(destination,manifest,owner)
        (destination/'extra').unlink();(destination/'app').chmod(0o777)
        with self.assertRaisesRegex(g.p.u.Refusal,'protected-staged-source-directory'):g.verify_tree(destination,manifest,owner)
        (destination/'app').chmod(0o755);(destination/'app/main.py').write_text('changed')
        with self.assertRaisesRegex(g.p.u.Refusal,'unchanged-staged-source'):g.verify_tree(destination,manifest,owner)

    def test_privileged_guest_staging_refuses_personal_host_before_native_operations(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(g.p.u.Refusal,'hosted-ubuntu-required'):g.stage_guest(None,Path('/absent'),{})

if __name__=='__main__':unittest.main()
