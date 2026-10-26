"""Host controls for the owned Linux witness; these do not qualify Linux/root."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'))
import proxy_pair_reference as q
import proxy_pair_gdb as debug

class Tracee:
    def __init__(self,events,exit_code=1,kill_joins=True):
        self.events=iter(events);self.pid=1234;self.calls=[];self.path=None;self.code=exit_code;self.kill_joins=kill_joins
    def execute(self,command,**kwargs):
        self.calls.append(command)
        if command in ('run','continue'):
            try:self.path,text=next(self.events);return text
            except StopIteration:self.pid=0;return 'exited'
        if command=='kill' and self.kill_joins:self.pid=0
        return ''
    def selected_inferior(self):return SimpleNamespace(pid=self.pid)
    def parse_and_eval(self,name):return self.code if name=='$_exitcode' else 0 if name=='$rax' else 7

ENTRY="Catchpoint 1 (call to syscall 'fsync'), 0x0 in fsync ()"
RETURN="Catchpoint 1 (returned from syscall 'fsync'), 0x0 in fsync ()"

class PairWitnessTests(unittest.TestCase):
    def test_command_failure_reports_fixed_operation_and_withholds_private_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            work=object.__new__(q.Work);work.root=Path(temporary);work.deadline=q.time.monotonic()+30;work.env={}
            work.runner=SimpleNamespace(execute=lambda *args,**kwargs:(1,b'synthetic private paths and credential data'))
            with self.assertRaisesRegex(ValueError,'^owned-pair-before-selection-initial-install-status$'):
                work.command(['owned-fixture'],phase='before-selection-initial-install')
            with patch.object(work.runner,'execute') as execute:
                for phase in ('private/path','bad phase','x'*65,''):
                    with self.subTest(phase=phase),self.assertRaisesRegex(ValueError,'fixed-pair-command-phase'):
                        work.command(['owned-fixture'],phase=phase)
                execute.assert_not_called()

    def test_expected_native_refusal_remains_distinct_from_command_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            work=object.__new__(q.Work);work.root=Path(temporary);work.deadline=q.time.monotonic()+30;work.env={}
            work.runner=SimpleNamespace(execute=lambda *args,**kwargs:(1,b'fixed synthetic refusal'))
            status,_=work.command(['owned-fixture'],accepted=(1,),phase='stale-refusal')
            self.assertEqual(status,1)

    def test_personal_host_refuses_before_source_reads_commands_or_writes(self):
        with patch.dict(os.environ,{},clear=True),patch.object(q,'Work') as work,patch.object(Path,'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError,'hosted-ubuntu-required'):q.run(SimpleNamespace())
            work.assert_not_called();mkdir.assert_not_called()
        with patch.dict(os.environ,{},clear=True),patch.object(Path,'read_bytes') as read:
            with self.assertRaises(AssertionError):debug.main()
            read.assert_not_called()

    def drive(self,case,events,*,code=1,kill_joins=True):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);store=root/'store';store.mkdir();(store/'versions').mkdir()
        stage=store/'versions/.stage-1234-0';stage.mkdir();(stage/'approval.json').write_text('public')
        target='b'*64;selected=target if case in ('after-selection','late-sync-error') else 'a'*64
        (store/'active.json').write_text(json.dumps(dict(approval_sha256=selected)))
        (store/'.install.lock').write_bytes(b'')
        paths={'stage':stage/'approval.json','pending':store/'.active.pending','root':store,'other':root/'other'}
        trace=Tracee([(paths[path],text) for path,text in events],code,kill_joins)
        plan=dict(store=str(store),target=target,case=case,marker=str(root/'marker'),contender=['fixture-owned-installer'])
        return root,trace,plan

    def test_actual_stop_driver_ignores_unrelated_sync_and_syscall_returns(self):
        root,trace,plan=self.drive('partial-stage',[('root',ENTRY),('root',RETURN),('stage',ENTRY)])
        with patch.object(debug.os,'readlink',side_effect=lambda _:str(trace.path)):
            debug.drive(trace,plan)
        value=json.loads((root/'marker').read_bytes())
        self.assertEqual(value['stage_members'],['approval.json']);self.assertFalse(value['selected_target'])
        self.assertTrue(value['joined']);self.assertEqual(trace.calls.count('kill'),1)

    def test_before_selection_checks_competing_actual_command_while_writer_stopped(self):
        root,trace,plan=self.drive('before-selection',[('pending',ENTRY)])
        with patch.object(debug.os,'readlink',side_effect=lambda _:str(trace.path)),patch.object(debug.subprocess,'run',return_value=SimpleNamespace(returncode=1,stderr=b'Proxy installation refused (install-lock).')) as command:
            debug.drive(trace,plan)
        command.assert_called_once_with(['fixture-owned-installer'],capture_output=True,timeout=10)
        self.assertTrue(json.loads((root/'marker').read_bytes())['concurrent_writer_refused'])

    def test_late_error_changes_only_syscall_result_and_requires_failed_joined_tracee(self):
        root,trace,plan=self.drive('late-sync-error',[('root',ENTRY),('root',RETURN)])
        with patch.object(debug.os,'readlink',side_effect=lambda _:str(trace.path)):debug.drive(trace,plan)
        self.assertNotIn('set $orig_rax = -1',trace.calls);self.assertIn('set $rax = -5',trace.calls)
        self.assertNotIn('kill',trace.calls);self.assertTrue(json.loads((root/'marker').read_bytes())['selected_target'])

    def test_unjoined_tracee_wrong_exit_or_unexpected_stop_cannot_write_success(self):
        cases=[('after-selection',[('root',ENTRY)],1,False),('late-sync-error',[('root',ENTRY),('root',RETURN)],0,True),
               ('late-sync-error',[('root',ENTRY),('root','Program received signal SIGSEGV')],1,True),
               ('partial-stage',[('other','Program received signal SIGSEGV')],1,True)]
        for case,events,code,joins in cases:
            with self.subTest(case=case,events=events,code=code):
                root,trace,plan=self.drive(case,events,code=code,kill_joins=joins)
                with patch.object(debug.os,'readlink',side_effect=lambda _:str(trace.path)),self.assertRaises(AssertionError):debug.drive(trace,plan)
                self.assertFalse((root/'marker').exists())

    def receipt(self):
        return dict(schema=1,scope=q.SCOPE,qualified=True,checkout=dict(commit='a'*40,clean=True),inputs={'public.py':'c'*64},
            pairs={key:dict(approval_sha256='d'*64,qualification_sha256='e'*64,binary_sha256=digest*64)
                   for key,digest in [('original','a'),('fixture_variant','b')]},
            interruptions={case:True for case in q.CASES},recovered_pairs=4,switches=16,reader_successes=2,reader_refusals=1,
            reader_overlapped_switches=True,concurrent_writer_refused=True,
            refusals=dict(stale=1,changed_version=1,changed_selector=1,missing_selector=1,symlink_selector=1,hardlink_selector=1,writable_selector=1),
            https_tests=12,https_skips=0,retained_original_pair=True,retained_original_launcher_checked=True,cleanup_joined=True,
            executables={key:'f'*64 for key in ('launcher','installer','gdb')},**{key:False for key in q.LIMITS})

    def test_receipt_requires_all_crashes_real_reads_https_source_and_scope_limits(self):
        value=self.receipt();q.validate(value,'a'*40,{'public.py':'c'*64})
        for key,replacement in [('schema',True),('recovered_pairs',True),('https_tests',8),('https_skips',1),('switches',0),
                ('reader_successes',0),('reader_overlapped_switches',False),('concurrent_writer_refused',False),('cleanup_joined',False),
                ('retained_original_pair',False),('retained_original_launcher_checked',False),('inputs',{}),('interruptions',{}),('fips_validated',True),('deployment_permitted',True)]:
            changed=copy.deepcopy(value);changed[key]=replacement
            with self.subTest(key=key),self.assertRaises(ValueError):q.validate(changed,'a'*40,{'public.py':'c'*64})
        changed=copy.deepcopy(value);changed['refusals']['stale']=True
        with self.assertRaises(ValueError):q.validate(changed,'a'*40,{'public.py':'c'*64})

    def test_sources_and_workflow_trigger_cover_installer_debugger_and_witness(self):
        from fnmatch import fnmatchcase
        names={'gateway/proxy/src/install.rs','gateway/proxy/src/bin/install.rs','gateway/proxy/src/lib.rs','gateway/tests/proxy_pair.py',
            'gateway/tests/proxy_pair_reference.py','gateway/tests/proxy_pair_gdb.py','tests/host/test_proxy_pair_reference.py','.github/workflows/gateway-proxy-pair.yml'}
        self.assertTrue(names<=set(q.evidence.INPUTS))
        spec=importlib.util.spec_from_file_location('pair_workflow_policy',ROOT/'tools/check-security.py')
        policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
        text=(ROOT/'.github/workflows/gateway-proxy-pair.yml').read_text()
        filters=policy.identity_path_filters(text,'pull_request')
        self.assertTrue(all(any(fnmatchcase(name,p) for p in filters) for name in q.evidence.INPUTS))
        self.assertIn('proxy-pair-original-review.json',text);self.assertIn('proxy-pair-fixture-variant-qualification.json',text)
        self.assertNotIn('path: /run/',text)


    def test_cleanup_refuses_to_remove_resources_while_observer_is_still_live(self):
        work=object.__new__(q.Work);work.deadline=0;work.units=set()
        # A small hashable fixture stands for the still-live owned observer.
        class Live:
            def join(self,**kwargs):pass
            def is_alive(self):return True
        class Stop:
            def set(self):pass
        work.observers={(Live(),Stop())}
        with self.assertRaisesRegex(ValueError,'owned-reader-retired-before-removal'):work.close()
        self.assertEqual(len(work.observers),1)

    def test_debugger_service_must_join_before_its_owned_unit_is_forgotten(self):
        work=object.__new__(q.Work);unit='nxrs-pair-fixture.service';work.units={unit}
        state={key:'' for key in q.lifetime.PROPERTIES}
        state.update(LoadState='loaded',ActiveState='deactivating',SubState='stop-sigterm',Result='success',
            MainPID='1234',ControlPID='0',Job='1',ControlGroup='/system.slice/'+unit,ExecMainStartTimestampMonotonic='1')
        raw='\n'.join(key+'='+value for key,value in state.items()).encode()
        work.command=lambda *args,**kwargs:(0,raw)
        with self.assertRaisesRegex(ValueError,'owned-debugger-unit-quiescent'):work.retire(unit)
        self.assertEqual(work.units,{unit})


    def test_joined_documents_cannot_promote_fixture_bytes_to_publisher_or_release_approval(self):
        from hashlib import sha256
        def encoded(value):return (json.dumps(value,sort_keys=True)+'\n').encode()
        parent_proof=encoded(dict(scope='synthetic-host-document-control'))
        original=dict(binary_sha256='a'*64,qualification_sha256=sha256(parent_proof).hexdigest(),libraries={'ssl':'c'*64})
        original_raw=encoded(original)
        parent=dict(binary_sha256='a'*64,qualification_sha256=sha256(parent_proof).hexdigest(),approval_sha256=sha256(original_raw).hexdigest())
        variant=dict(schema=1,scope='owned-nonloaded-trailer-proxy-pair-fixture',parent=parent,binary_sha256='b'*64,
            trailer_sha256=sha256(q.VARIANT).hexdigest(),transformation='append-fixed-nonloaded-fixture-trailer',
            publisher_signature_covers_fixture_trailer=False,https_boundaries=dict(tests=4,failures=0,skips=0,log_discard_qualified=True,log_configuration_refusals=2),
            **{key:False for key in q.LIMITS})
        def documents(value):
            raw=encoded(value);approval={**original,'binary_sha256':'b'*64,'qualification_sha256':sha256(raw).hexdigest()}
            approval_raw=encoded(approval)
            receipt={'pairs':{'original':parent,'fixture_variant':dict(binary_sha256='b'*64,qualification_sha256=sha256(raw).hexdigest(),approval_sha256=sha256(approval_raw).hexdigest())}}
            return receipt,{'original-review':original_raw,'original-qualification':parent_proof,'fixture-variant-review':approval_raw,'fixture-variant-qualification':raw}
        receipt,docs=documents(variant);q.validate_documents(receipt,docs)
        for key,replacement in [('publisher_signature_covers_fixture_trailer',True),('deployment_permitted',True),
                                ('parent',{}),('transformation','publisher-signed-release')]:
            changed={**variant,key:replacement};receipt,docs=documents(changed)
            # Recompute all document hashes: this must be a semantic refusal,
            # not merely a changed-byte refusal before the scope is inspected.
            with self.subTest(key=key),self.assertRaises(ValueError):q.validate_documents(receipt,docs)

if __name__=='__main__':unittest.main()
