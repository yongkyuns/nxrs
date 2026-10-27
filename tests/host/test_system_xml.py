"""Read-only controls; actual Linux/root/loader execution belongs to hosted CI."""
import copy,hashlib,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'));sys.path.insert(0,str(ROOT/'gateway/tools'))
import expat as build
import system_xml_evidence as evidence

class SystemXmlTests(unittest.TestCase):
    def pin(self):return json.loads((ROOT/evidence.POLICY).read_bytes())

    def test_fresh_process_loads_the_signed_source_builder_and_reviewed_sibling_without_operations(self):
        import subprocess
        code = """import importlib.util, pathlib, subprocess, sys, urllib.request
root=pathlib.Path(ROOT_VALUE)
sys.path.insert(0,str(root/'tools/native-packages'))
import expat
def refused(*args,**kwargs):raise AssertionError('unexpected native/network/allocation operation')
subprocess.run=refused;subprocess.Popen=refused;urllib.request.urlopen=refused;pathlib.Path.mkdir=refused
tools=expat.source_tools()
assert tools.ROOT==root
assert pathlib.Path(tools.advisories.__file__).resolve()==root/'tools/gateway-proxy/advisories.py'
assert pathlib.Path(tools.payloads.__file__).resolve()==root/'tools/native-packages/payloads.py'
assert callable(tools.signed_source) and callable(tools.Work)
print('PASS: fresh reviewed signed-source imports only')
""".replace('ROOT_VALUE',repr(str(ROOT)))
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,'-I','-B','-c',code],cwd=directory,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'PASS: fresh reviewed signed-source imports only')


    def test_personal_host_refuses_before_policy_reads_native_commands_network_or_mutation(self):
        with patch.object(build.sys,'platform','darwin'),patch.object(Path,'read_bytes') as read,patch.object(Path,'mkdir') as mkdir,patch.object(build.ubuntu,'command') as command:
            with self.assertRaises(ValueError):build.run(Path('/private/synthetic'))
            for call in (read,mkdir,command):call.assert_not_called()

    def test_policy_cannot_select_an_unreviewed_signer_source_baseline_or_closure_override(self):
        self.assertEqual(build.policy(self.pin()),self.pin())
        changes=[lambda v:v.update(upstream_advisory_closure=True),lambda v:v.update(deployment_permitted=True),
                 lambda v:v['source'].update(version='2.8.5'),lambda v:v['source']['key'].update(fingerprint='A'*40),
                 lambda v:v['source']['key'].update(sha256='448d83487dabc57baa254e961eed48d0530da0c7d0f8f963b85a29a49cf8ca79'),
                 lambda v:v['source']['key'].update(url='https://blog.hartwork.org/3176_EF7D_B236_7F1F_CA4F__306B_1F9B_0E90_9AF3_7285.asc'),
                 lambda v:v['source']['key'].update(url='https://private.invalid/key'),lambda v:v['source'].update(private='value'),
                 lambda v:v['vendor_baseline'].update(sha256='a'*64),lambda v:v['upstream_issue'].update(reference='https://private.invalid/issue'),
                 lambda v:v.update(matched_distribution_advisories=[]),lambda v:v.update(unresolved_published_advisories=[])]
        for change in changes:
            value=self.pin();change(value)
            with self.subTest(value=value),self.assertRaises(ValueError):build.policy(value)

    def test_work_directory_must_be_a_new_named_child_of_the_owned_runner_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();target=root/'owned-build'
            with patch.dict(build.os.environ,{'RUNNER_TEMP':str(root)}):
                self.assertEqual(build.work_location(target),target)
                for path in (Path('relative'),root.parent/'foreign',root/'bad name',root/'.hidden'):
                    with self.subTest(path=path),self.assertRaises(ValueError):build.work_location(path)
                target.mkdir()
                with self.assertRaises(ValueError):build.work_location(target)

    def test_suite_requires_actual_nonzero_upstream_checks_and_no_skips(self):
        raw=b'PASS: runtests\n# TOTAL: 1\n# PASS: 1\n# SKIP: 0\n# XFAIL: 0\n# FAIL: 0\n# XPASS: 0\n# ERROR: 0\n'
        self.assertEqual(build.test_summary(raw,b'100%: Checks: 1234, Failed: 0\n')['checks'],1234)
        for output,log in [(raw.replace(b'# SKIP: 0',b'# SKIP: 1'),b'100%: Checks: 1234, Failed: 0\n'),
                           (raw,b'100%: Checks: 0, Failed: 0\n'),(raw,b'99%: Checks: 1234, Failed: 1\n'),
                           (raw+b'# TOTAL: 1\n',b'100%: Checks: 1234, Failed: 0\n')]:
            with self.assertRaises(ValueError):build.test_summary(output,log)

    def test_symbol_and_soname_controls_preserve_every_vendor_export(self):
        rows=b'\n'.join((name+' T 10 1').encode() for name in sorted(evidence.REQUIRED))
        self.assertEqual(set(build.exports(rows)),evidence.REQUIRED)
        self.assertEqual(build.soname(b'(SONAME) [libexpat.so.1]'),'libexpat.so.1')
        for raw in (b'XML_private@@OTHER T 10 1',rows+b'\n'+rows,b'XML_Parse T 10 1'):
            with self.assertRaises(ValueError):build.exports(raw)
        for raw in (b'(SONAME) [libexpat.so.2]',b'(SONAME) [libexpat.so.1]\n(SONAME) [libexpat.so.1]'):
            with self.assertRaises(ValueError):build.soname(raw)

    def test_library_discovery_uses_the_vendor_subdirectory_and_refuses_alias_only_or_ambiguous_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wrong=root/'.libs';wrong.mkdir();(wrong/'libexpat.so.1.13.0').write_bytes(b'wrong build location')
            with self.assertRaisesRegex(ValueError,'one-system-xml-built-image'):build.built_library(root)
            output=root/'lib/.libs';output.mkdir(parents=True);image=output/'libexpat.so.1.13.0'
            image.symlink_to(wrong/'libexpat.so.1.13.0')
            with self.assertRaisesRegex(ValueError,'one-system-xml-built-image'):build.built_library(root)
            image.unlink();image.write_bytes(b'owned built library');(output/'libexpat.so.1').symlink_to(image.name)
            self.assertEqual(build.built_library(root),image)
            extra=output/'libexpat.so.1.12.0';extra.write_bytes(b'unexpected second revision')
            with self.assertRaisesRegex(ValueError,'one-system-xml-built-image'):build.built_library(root)
            image.unlink()
            with self.assertRaisesRegex(ValueError,'one-system-xml-built-image'):build.built_library(root)

    def test_fix_references_require_security_sections_and_supported_release(self):
        raw=b'Release 2.9.0 now\n Security fixes:\n CVE-2026-102633 CVE-2026-77214\n Bug fixes:\n next\nRelease 2.8.5 earlier\n Security fixes:\n CVE-2026-93990\n'
        expected={'CVE-2026-102633':'2.9.0','CVE-2026-77214':'2.9.0','CVE-2026-93990':'2.8.5'}
        self.assertEqual(build.published_fixes(raw,self.pin()['documented_fix_releases']),expected)
        with self.assertRaises(ValueError):build.published_fixes(raw,self.pin()['matched_distribution_advisories'])
        for value in (raw.replace(b'Release 2.9.0',b'Release 2.9.1'),raw.replace(b'Security fixes:',b'Bug fixes:'),raw.replace(b'CVE-2026-93990',b'other')):
            with self.assertRaises(ValueError):build.published_fixes(value,self.pin()['documented_fix_releases'])

    def test_upstream_unresolved_items_are_recorded_without_becoming_clean_or_confirmed_counts(self):
        pin=self.pin()['upstream_issue'];v=dict(number=1160,html_url=pin['reference'],state='open',updated_at='2026-10-26T00:00:00Z',body='- [ ] non-public pending\n- [x] fixed\n')
        result=build.upstream_status(json.dumps(v).encode(),pin)
        self.assertEqual(result['unchecked_items'],1);self.assertTrue(result['unchecked_items_are_not_vulnerability_count'])
        self.assertNotIn('body',result);self.assertEqual(result['body_sha256'],hashlib.sha256(v['body'].encode()).hexdigest())
        with self.assertRaises(ValueError):build.upstream_status(json.dumps({**v,'html_url':'https://private.invalid'}).encode(),pin)

    def test_failed_selection_rename_joins_cleanup_and_preserves_the_original_failure(self):
        calls=[]
        def execute(args,phase,**kwargs):
            calls.append((args,phase,kwargs))
            if phase.endswith('-rename'):raise ValueError('synthetic-private-rename-failure')
            return 0,b''
        with patch.object(build.secrets,'token_hex',return_value='0'*16),patch.object(build.os.path,'lexists',return_value=False),self.assertRaisesRegex(ValueError,'synthetic-private-rename-failure'):
            build.redirect(SimpleNamespace(execute=execute),Path('/owned/candidate'),'owned-select')
        self.assertEqual([phase for _,phase,_ in calls],['owned-select-link','owned-select-rename','owned-select-unlink'])
        self.assertEqual(calls[-1][0],['sudo','-n','/usr/bin/rm','-f','--',build.LINK.with_name('.expat-qualification-'+'0'*16)])
        self.assertEqual(calls[-1][2],dict(accepted=(0,1)))

    def receipt(self):
        pin=self.pin();sha='a'*64
        return dict(schema=1,scope=evidence.SCOPE,qualified=True,verification_status='complete',checkout=dict(commit='b'*40,clean=True),inputs=evidence.inputs(ROOT),
                    source=pin['source'],source_signature=dict(signer=pin['source']['key']['fingerprint'],sha256=pin['source']['sha256'],changed_archive_refused=True),
                    vendor=dict(name='libexpat1',version='2.6.1-2ubuntu0.6',source='expat',source_version='2.6.1-2ubuntu0.6',sha256=pin['vendor_baseline']['sha256'],header_sha256=sha,header_source_version='2.6.1-2ubuntu0.6'),
                    soname='libexpat.so.1',library='lib/libexpat.so.1.13.0',library_sha256=sha,old_exports=sorted(evidence.REQUIRED),new_exports=sorted(evidence.REQUIRED),configuration_sha256=sha,
                    upstream_tests=dict(total=1,passed=1,skipped=0,failed=0,errors=0,checks=1234),published_fixes=pin['documented_fix_releases'],unresolved_published_advisories=pin['unresolved_published_advisories'],
                    upstream_reports=dict(reference=pin['upstream_issue']['reference'],state='open',updated_at='2026-10-26T00:00:00Z',body_sha256=sha,unchecked_items=1,unchecked_items_are_not_vulnerability_count=True),
                    python=dict(schema=1,version='expat_2.9.0',exact_loaded_image=True,image_sha256=sha,elementtree=True,fragmented_namespace_utf8=True,malformed_document_refused=True),
                    private_probe=True,selected_probe=True,wrong_image_refusals=2,rollback_control_executed=True,**dict.fromkeys(evidence.FLAGS,False))

    def test_component_receipt_cannot_claim_native_closure_skip_actual_python_or_lose_vendor_abi(self):
        value=self.receipt();evidence.validate(value,ROOT,'b'*40)
        changes=[lambda v:v.update(full_native_advisory_closure=True),lambda v:v.update(upstream_advisory_closure=True),
                 lambda v:v.update(deployment_permitted=True),lambda v:v.update(upstream_advisory_gate_qualified=True),lambda v:v.update(unresolved_published_advisories=[]),
                 lambda v:v.update(old_exports=sorted(evidence.REQUIRED|{'XML_RequiredVendorExtra'})),
                 lambda v:v['python'].update(exact_loaded_image=False),lambda v:v['python'].update(image_sha256='c'*64),
                 lambda v:v.update(rollback_control_executed=False),lambda v:v.update(wrong_image_refusals=True),
                 lambda v:v['vendor'].update(source_version='unreviewed'),lambda v:v.update(private_path='/private/synthetic')]
        for change in changes:
            changed=copy.deepcopy(value);change(changed)
            with self.assertRaises(ValueError):evidence.validate(changed,ROOT,'b'*40)

    def test_receipt_read_refuses_symlink_and_aliases_before_they_can_supply_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'proof';path.write_bytes(b'public');path.chmod(0o644)
            link=root/'alias';link.symlink_to(path)
            with self.assertRaises(OSError):evidence.private_read(link,64)
            original=evidence.os.fstat
            def root_owned(fd):
                row=original(fd)
                return SimpleNamespace(**{name:0 if name=='st_uid' else getattr(row,name) for name in
                    ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_gid','st_nlink')})
            with patch.object(evidence.os,'fstat',side_effect=root_owned):self.assertEqual(evidence.private_read(path,64),b'public')
            os.link(path,root/'hardlink')
            with patch.object(evidence.os,'fstat',side_effect=root_owned),self.assertRaises(ValueError):evidence.private_read(path,64)

    def test_every_executed_source_is_an_explicit_workflow_input_and_only_json_is_uploaded(self):
        text=(ROOT/'.github/workflows/gateway-system-xml.yml').read_text()
        for name in evidence.INPUTS:self.assertIn("- '"+name+"'",text)
        self.assertIn('qualification.json',text);self.assertNotIn('if: ${{ always() }}',text)
        self.assertIn('actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9',text)
        self.assertNotIn('**/*.log',text)

    def test_duplicate_receipt_fields_and_private_failure_messages_cannot_be_published(self):
        with self.assertRaises(ValueError):evidence.parse(b'{"schema":1,"schema":1}')
        error=None
        try:exec(compile("raise ValueError('synthetic private failure message')",str(ROOT/'tools/native-packages/expat.py'),'exec'))
        except ValueError as caught:error=caught
        rows=build.public_failure_locations(error)
        self.assertTrue(rows);self.assertLessEqual(len(rows),8)
        self.assertTrue(all(set(row)=={'source','line','error'} and not row['source'].startswith('/') for row in rows))
        self.assertNotIn('private',json.dumps(rows))

if __name__=='__main__':unittest.main()
