"""Read-only admission, exact causal evidence and helper-dispatch controls."""
import copy,importlib.util,json,os,stat,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'))
import service_boot_mount_reference as witness

class MountOrderingTests(unittest.TestCase):
    def receipt(self):
        return dict(schema=1,scope='hosted-pid1-initialization-mount-order',qualified=True,
            checkout=dict(commit='a'*40,clean=True),inputs=witness.inputs(),manager_sha256='b'*64,
            cases=[dict(case=mode,start_exit=1 if mode=='managed' else 0,managed_active_before_provider_return=mode=='managed',
                managed_job_pending=mode=='private',consumer_active=mode=='private',managed_mount_active=mode=='private',owned_cleanup_joined=True)
                for mode in ('managed','private')],**dict.fromkeys(witness.FLAGS,False))

    def test_personal_host_refuses_before_source_reads_native_operations_or_allocation(self):
        with patch.object(witness.sys,'platform','darwin'),patch.object(Path,'read_text') as read,\
             patch.object(witness,'command') as command,patch.object(Path,'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError,'disposable-hosted-root-required'):witness.run(Path('/private/synthetic.json'))
            read.assert_not_called();command.assert_not_called();mkdir.assert_not_called()

    def test_owned_root_cannot_name_a_disk_foreign_path_symlink_or_shared_directory(self):
        for root in (Path('/dev/sda'),Path('/run/other'),Path('/tmp/nxrs-bm-'+'a'*16),Path('relative')):
            with patch.object(Path,'lstat') as meta,self.assertRaises(ValueError):witness.owned_root(root)
            meta.assert_not_called()
        root=Path('/run/nxrs-bm-'+'a'*16)
        for info in (SimpleNamespace(st_mode=stat.S_IFLNK|0o700,st_uid=0),SimpleNamespace(st_mode=stat.S_IFDIR|0o777,st_uid=0),SimpleNamespace(st_mode=stat.S_IFDIR|0o700,st_uid=1000)):
            with patch.object(Path,'lstat',return_value=info),self.assertRaises(ValueError):witness.owned_root(root)

    def test_receipt_requires_both_actual_job_transitions_cleanup_and_limited_scope(self):
        value=self.receipt();witness.validate(value)
        changes=[lambda v:v.update(schema=True),lambda v:v.update(full_gateway_boot_qualified=True),lambda v:v.update(physical_qualified=True),
            lambda v:v.update(cases=v['cases'][::-1]),lambda v:v['cases'][0].update(managed_job_pending=True),
            lambda v:v['cases'][0].update(start_exit=0),lambda v:v['cases'][1].update(consumer_active=False),
            lambda v:v['cases'][1].update(owned_cleanup_joined=False),lambda v:v['cases'][1].update(start_exit=False),
            lambda v:v.update(private_message='synthetic private'),lambda v:v['checkout'].update(private='synthetic')]
        for change in changes:
            v=copy.deepcopy(value);change(v)
            with self.assertRaises(ValueError):witness.validate(v)

    def test_provider_dispatch_uses_the_three_fixed_arguments(self):
        root=Path('/run/nxrs-bm-'+'a'*16)
        with patch.object(sys,'argv',['owned-helper','provider',str(root),'private']),patch.object(witness,'provider') as provider,patch.object(witness,'run') as run:
            witness.main();provider.assert_called_once_with(root,'private');run.assert_not_called()

    def test_refused_helper_cannot_write_diagnostics_before_ownership_admission(self):
        with patch.object(sys,'argv',['owned-helper','provider','/private/synthetic','private']),\
             patch.object(witness,'helper_admitted',False),patch.object(witness,'provider',side_effect=ValueError('synthetic private refusal')),\
             patch('builtins.open') as opened:
            with self.assertRaises(SystemExit) as result:witness.main()
            self.assertEqual(result.exception.code,2);opened.assert_not_called()

    def test_minimal_graph_keeps_provider_mount_and_bound_consumer_ordering(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'owned.json').write_text(json.dumps(dict(schema=1,mode='private')))
            with patch.object(witness,'mount_name',return_value='owned.mount'):
                rendered,provider,mount,consumer=witness.units(root)
            self.assertIn('After='+provider+' '+mount,rendered[consumer]);self.assertIn('BindsTo='+mount,rendered[consumer])
            self.assertIn('Requires='+provider,rendered[mount]);self.assertIn('After='+provider,rendered[mount])
            self.assertIn('DefaultDependencies=no',rendered[provider]);self.assertIn('Type=oneshot',rendered[provider])
            self.assertIn('What=tmpfs',rendered[mount]);self.assertIn('size=1048576',rendered[mount])

    def test_failure_coordinates_exclude_native_output_arguments_and_exception_text(self):
        error=None
        try:exec(compile("raise ValueError('synthetic private native output')",str(ROOT/'gateway/tests/service_boot_mount_reference.py'),'exec'))
        except ValueError as caught:error=caught
        rows=witness.locations(error);self.assertTrue(rows);self.assertLessEqual(len(rows),8)
        self.assertTrue(all(set(row)=={'source','line','error'} for row in rows));self.assertNotIn('private',json.dumps(rows))

    def test_workflow_triggers_every_bound_source_and_uploads_only_the_public_receipt(self):
        text=(ROOT/'.github/workflows/gateway-service-boot-order.yml').read_text()
        for name in witness.INPUTS:self.assertIn("- '"+name+"'",text)
        self.assertIn('initialization-order.json',text);self.assertNotIn('if: ${{ always() }}',text);self.assertNotIn('*.log',text)

if __name__=='__main__':unittest.main()
