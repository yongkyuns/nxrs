"""Synthetic receipt, ordinary ABI-parser and owned-link failure controls."""
import contextlib,copy,hashlib,io,json,os
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import pcre
e=pcre.evidence

class SystemRegexTests(unittest.TestCase):
    def test_http_diagnostics_only_export_status_and_owned_source_coordinates(self):
        self.assertEqual(e.inputs(ROOT)['gateway/tests/failure_location.py'],hashlib.sha256((ROOT/'gateway/tests/failure_location.py').read_bytes()).hexdigest())
        error=HTTPError('https://private.invalid/?secret=hidden',403,'private response detail',{'Secret':'hidden'},None)
        try:exec(compile('raise error',str(ROOT/'tools/gateway-proxy/qualify.py'),'exec'),{'error':error})
        except HTTPError as caught:result=pcre.failure_diagnostics(caught)
        self.assertEqual(result,dict(http_status=403,locations=[dict(source='tools/gateway-proxy/qualify.py',line=1,error='HTTPError')]))
        for secret in ('private','hidden','https','Secret'):self.assertNotIn(secret,json.dumps(result))

    def test_invalid_http_status_and_non_http_errors_cannot_export_arbitrary_attributes(self):
        for status in (True,99,600,'403',None):
            with self.subTest(status=status):
                error=HTTPError('private-url',status,'private-message',{},None)
                self.assertIsNone(pcre.failure_diagnostics(error)['http_status'])
        self.assertEqual(pcre.failure_diagnostics(RuntimeError('private detail')),dict(http_status=None,locations=[]))

    def test_cli_still_fails_and_suppresses_http_url_headers_and_body(self):
        error=HTTPError('https://private.invalid/?token=hidden',503,'private body',{'Secret':'hidden'},None)
        out,err=io.StringIO(),io.StringIO()
        with patch.object(sys,'argv',['pcre.py','--work','/not-used']),patch.object(pcre,'run',side_effect=error),\
             contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),self.assertRaises(SystemExit) as exit:
            pcre.main()
        self.assertEqual(exit.exception.code,1)
        self.assertIn('Shared system regex qualification refused (HTTPError).',err.getvalue())
        result=json.loads(out.getvalue().removeprefix('PCRE FAILURE DIAGNOSTICS '))
        self.assertEqual(result['http_status'],503);self.assertLessEqual(len(result['locations']),8)
        for secret in ('private.invalid','token=','hidden','private body','Secret'):self.assertNotIn(secret,out.getvalue()+err.getvalue())

    def fixture(self):
        policy=json.loads((ROOT/'security/gateway-proxy-dependencies.json').read_bytes());pin=policy['sources']['pcre2']
        return dict(schema=1,scope=e.SCOPE,qualified=True,verification_status='complete',checkout=dict(commit='a'*40,clean=True),inputs=e.inputs(ROOT),
            source=pin,source_signature=dict(signer=pin['key']['fingerprint'],sha256=pin['sha256'],changed_archive_refused=True),
            upstream_advisories=dict(articles=len(policy['advisory_review']['pcre2']),feed_sha256=hashlib.sha256(e.encoded(policy['advisory_review']['pcre2'])).hexdigest(),unfixed=[]),
            affected_source_version_refused=True,upstream_tests=dict(total=4,passed=4,skipped=0,failed=0,errors=0),jit_enabled=True,loaded_library_probe_passed=True,
            old_exports=sorted(e.REQUIRED),new_exports=policy['shared_pcre2_abi']['default_exports'],version_script_sha256=policy['shared_pcre2_abi']['sha256'],soname='libpcre2-8.so.0',library='lib/libpcre2-8.so.0.16.0',
            library_sha256='b'*64,old_library_sha256='c'*64,configuration_sha256='d'*64,consumer_names=['cryptsetup','service_manager'],**dict.fromkeys(e.FLAGS,False))

    def test_personal_host_refuses_before_creating_files_or_running_native_tools(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True),patch.object(pcre.ubuntu,'command') as command:
            path=Path(directory)/'new'
            with self.assertRaisesRegex(pcre.ubuntu.Refusal,'hosted-ubuntu-required'):pcre.run(path)
            self.assertFalse(path.exists());command.assert_not_called()

    def test_export_superset_and_soname_are_required(self):
        raw=''.join(name+' T 0010 20\n' for name in sorted(e.REQUIRED)).encode();old=pcre.exports(raw)
        pcre.compatible(old,old);self.assertEqual(pcre.soname(b'(SONAME) Library soname: [libpcre2-8.so.0]'),'libpcre2-8.so.0')
        for data in [raw+raw,raw.replace(b'pcre2_compile_8',b'pcre2_compile_8@@UNREVIEWED'),b'']:
            with self.assertRaises(pcre.ubuntu.Refusal):pcre.exports(data)
        with self.assertRaises(pcre.ubuntu.Refusal):pcre.compatible(old,old[1:])
        for raw in [b'',b'(SONAME) Library soname: [libpcre2-8.so.1]',b'(SONAME) [libpcre2-8.so.0]\n(SONAME) [libpcre2-8.so.0]']:
            with self.assertRaises(pcre.ubuntu.Refusal):pcre.soname(raw)

    def test_default_versions_serve_unversioned_callers_without_discarding_requirements(self):
        plain=sorted(e.REQUIRED);default=[name+'@@PCRE2_10.47' for name in plain]
        pcre.compatible(plain,default)
        self.assertEqual(pcre.exports(('\n'.join(name+' T 0010 20' for name in default)+'\n').encode()),default)
        with self.assertRaises(pcre.ubuntu.Refusal):pcre.compatible(plain,[name+'@PCRE2_10.47' for name in plain])
        with self.assertRaises(pcre.ubuntu.Refusal):pcre.compatible(default,[name+'@@PCRE2_10.48' for name in plain])
        with self.assertRaises(pcre.ubuntu.Refusal):pcre.compatible(plain,plain+default)
        pcre.compatible([name+'@PCRE2_10.47' for name in plain],default)

    def test_every_upstream_suite_must_pass_without_skips(self):
        raw=b'PASS: pcre2posix_test\nPASS: pcre2_jit_test\nPASS: RunTest\nPASS: RunGrepTest\n# TOTAL: 4\n# PASS: 4\n# SKIP: 0\n# XFAIL: 0\n# FAIL: 0\n# XPASS: 0\n# ERROR: 0\n'
        self.assertEqual(pcre.test_summary(raw)['passed'],4)
        for value in [raw.replace(b'PASS: RunTest\n',b''),raw.replace(b'# SKIP: 0',b'# SKIP: 1'),raw.replace(b'# FAIL: 0',b'# FAIL: 1'),b'']:
            with self.assertRaises(pcre.ubuntu.Refusal):pcre.test_summary(value)

    def test_changed_sources_signatures_abi_execution_and_scope_cannot_be_admitted(self):
        value=self.fixture();e.validate(value,ROOT,'a'*40)
        changes=[('unclassified_private_data','not publishable'),('schema',True),('qualified',False),('verification_status','pending'),('inputs',{}),
                 ('source_signature',{}),('upstream_advisories',{}),('old_exports',[]),('new_exports',[]),
                 ('jit_enabled',False),('loaded_library_probe_passed',False),('affected_source_version_refused',False),
                 ('version_script_sha256','0'*64),('library','../outside.so'),('consumer_names',['service_manager']),('library_sha256','invalid')]
        for key,item in changes:
            changed=copy.deepcopy(value);changed[key]=item
            with self.subTest(field=key),self.assertRaises((ValueError,KeyError)):e.validate(changed,ROOT,'a'*40)
        for flag in e.FLAGS:
            changed=copy.deepcopy(value);changed[flag]=True
            with self.subTest(flag=flag),self.assertRaises(ValueError):e.validate(changed,ROOT,'a'*40)

    def test_root_owned_report_reader_rejects_user_owned_files_and_final_symlinks(self):
        if os.getuid()==0:self.skipTest('unprivileged descriptor-owner control')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'receipt';path.write_bytes(b'public fixture');path.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'root-owned'):e.private_read(path,1024)
            link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(OSError):e.private_read(link,1024)

    def test_atomic_link_replacement_and_failed_rename_preserve_original(self):
        for failed in [False,True]:
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as directory:
                link=Path(directory)/'selected';link.symlink_to('original.so')
                class Work:
                    def execute(self,args,phase,**kwargs):
                        operation=args[2]
                        if operation.endswith('/ln'):Path(args[-1]).symlink_to(args[-2])
                        elif operation.endswith('/mv'):
                            if failed:raise RuntimeError('owned injected rename refusal')
                            Path(args[-2]).replace(args[-1])
                        elif operation.endswith('/rm'):Path(args[-1]).unlink()
                        else:raise AssertionError('unexpected test operation')
                with patch.object(pcre,'LINK',link):
                    if failed:
                        with self.assertRaises(RuntimeError):pcre.redirect(Work(),Path('replacement.so'),'owned')
                    else:pcre.redirect(Work(),Path('replacement.so'),'owned')
                self.assertEqual(link.readlink(),Path('original.so' if failed else 'replacement.so'))
                self.assertEqual([p.name for p in Path(directory).iterdir()],['selected'])

if __name__=='__main__':unittest.main()
