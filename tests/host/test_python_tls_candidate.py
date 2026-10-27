"""Offline extraction, identity and evidence refusals; native TLS belongs to hosted CI."""
import copy
import hashlib
import io
import json
import lzma
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT / 'tools/native-packages'))
import python_tls as t
import source_payloads as s


class PythonTlsCandidateTests(unittest.TestCase):
    def archive(self, entries):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w') as archive:
            for name,kind in entries:
                row=tarfile.TarInfo(name);row.type=kind;row.mode=0o777
                if kind==tarfile.REGTYPE:row.size=3
                if kind in (tarfile.SYMTYPE,tarfile.LNKTYPE):row.linkname='/outside'
                archive.addfile(row,io.BytesIO(b'abc') if row.size else None)
        return lzma.compress(data.getvalue())

    def unpack(self, directory, raw):
        p=Path(directory)/'source.tar.xz';p.write_bytes(raw)
        identity=dict(size=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        t.unpack(p,identity,Path(directory)/'unpacked')

    def test_safe_regular_archive_preserves_only_owner_execution(self):
        with tempfile.TemporaryDirectory() as name:
            self.unpack(name,self.archive([('tree',tarfile.DIRTYPE),('tree/file',tarfile.REGTYPE)]))
            p=Path(name)/'unpacked/tree/file'
            self.assertEqual(p.read_bytes(),b'abc');self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o700)

    def test_traversal_absolute_links_special_and_duplicate_members_refuse_before_extraction(self):
        cases=[[('../escape',tarfile.REGTYPE)],[('/escape',tarfile.REGTYPE)],
               [('tree/link',tarfile.SYMTYPE)],[('tree/link',tarfile.LNKTYPE)],
               [('tree/pipe',tarfile.FIFOTYPE)],[('tree/file',tarfile.REGTYPE)]*2,
               [('tree//file',tarfile.REGTYPE)]]
        for entries in cases:
            with self.subTest(entries=entries),tempfile.TemporaryDirectory() as name:
                with self.assertRaises(ValueError):self.unpack(name,self.archive(entries))
                self.assertFalse((Path(name)/'unpacked').exists())

    def test_decompression_budget_refuses_before_writes(self):
        with tempfile.TemporaryDirectory() as name,patch.object(t,'MAX_TAR',1024):
            with self.assertRaisesRegex(ValueError,'decompression-budget'):
                self.unpack(name,self.archive([('tree/file',tarfile.REGTYPE)]))
            self.assertFalse((Path(name)/'unpacked').exists())

    def test_changed_source_identity_refuses(self):
        with tempfile.TemporaryDirectory() as name:
            p=Path(name)/'source';raw=self.archive([('tree/file',tarfile.REGTYPE)]);p.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'authenticated-source-bytes'):
                t.unpack(p,dict(size=len(raw),sha256='0'*64),Path(name)/'unpacked')

    def test_patch_and_recipe_are_the_exact_reviewed_backport(self):
        r=t.recipe();self.assertEqual(r['source_version'],s.CASES[r['case']]['version'])
        self.assertEqual(r['fixture']['size'],5062)
        self.assertIn(b'SSL_CTX_set_verify(self->ctx, mode, _ssl_verify_callback)',(ROOT/t.PATCH).read_bytes())

    def test_changed_backport_or_upstream_identity_refuses(self):
        original=t.parse((ROOT/t.RECIPE).read_bytes())
        for field,value in [('merge','0'*40),('module_patch_path','elsewhere'),('header_git_blob','0'*40),('module_patch_sha256','0'*64)]:
            row=copy.deepcopy(original);row['upstream'][field]=value
            with self.subTest(field=field),patch.object(t,'parse',return_value=row),self.assertRaises(ValueError):t.recipe()

    def witness(self, phase='patched'):
        return dict(phase=phase,ordinary_chain_progress=2,certificate_cases=[dict(tls=v,mode=m,outcome='certificate-rejected-28' if phase=='patched' else 'accepted-affected-baseline') for v in ('TLSv1.2','TLSv1.3') for m in ('required','optional')],openssl_version='OpenSSL 3.0.13 30 Jan 2024',python_version='3.12.3',python_sha256='a'*64,module_sha256='b'*64,libraries={'libssl.so.3':'c'*64,'libcrypto.so.3':'d'*64},private_api_module_parse_passed=True,private_api_module_sha256='e'*64,owned_extension_directory_configured=True,extension_directory_config_sha256='f'*64)

    def test_complete_baseline_and_patched_matrices_are_distinct(self):
        for phase in ('baseline','patched'):self.assertEqual(t.validate_witness(self.witness(phase),phase)['phase'],phase)
        with self.assertRaises(ValueError):t.validate_witness(self.witness('baseline'),'patched')

    def test_missing_duplicate_wrong_mode_or_wrong_refusal_cannot_admit_callback(self):
        changes=[]
        for field,value in [('ordinary_chain_progress',True),('ordinary_chain_progress',1),('python_version','3.12.4'),('module_sha256','bad'),('libraries',{}),('certificate_cases',[]),('private_api_module_parse_passed',False)]:
            v=self.witness();v[field]=value;changes.append(v)
        v=self.witness();v['certificate_cases'][0]['outcome']='certificate-rejected-10';changes.append(v)
        v=self.witness();v['certificate_cases'][0]['mode']='none';changes.append(v)
        v=self.witness();v['certificate_cases'][0]=v['certificate_cases'][1];changes.append(v)
        for v in changes:
            with self.subTest(v=v),self.assertRaises(ValueError):t.validate_witness(v,'patched')

    def test_personal_host_refused_before_paths_inputs_network_or_build(self):
        with tempfile.TemporaryDirectory() as name:
            work=Path(name)/'work';report=Path(name)/'report'
            with patch.object(s.u,'hosted',side_effect=s.u.Refusal('hosted-ubuntu-required')),patch.object(t,'recipe') as recipe,patch('urllib.request.urlopen') as network:
                with self.assertRaisesRegex(ValueError,'hosted-ubuntu-required'):t.run(work,report)
                recipe.assert_not_called();network.assert_not_called()
            self.assertFalse(work.exists());self.assertFalse(report.exists())

    def test_source_failure_cleans_private_work_and_publishes_no_secret_or_success(self):
        with tempfile.TemporaryDirectory() as name:
            work=Path(name)/'work';report=Path(name)/'report'
            def command(args):return 'a'*40 if 'rev-parse' in args else ''
            with patch.object(s.u,'hosted'),patch.object(s.u,'command',side_effect=command),patch.object(s,'inputs',return_value={}),patch.object(s,'run',side_effect=RuntimeError('/private/fixture-key must stay private')):
                with self.assertRaises(RuntimeError):t.run(work,report)
            result=json.loads(report.read_bytes());self.assertFalse(result['qualified'])
            self.assertEqual(result['failure_category'],'RuntimeError')
            self.assertTrue(result['private_build_and_fixture_cleanup_joined']);self.assertFalse(work.exists())
            for name in t.FLAGS:self.assertIs(result[name],False)
            self.assertNotIn(b'fixture-key',report.read_bytes())

    def test_escaped_duplicate_json_keys_refuse(self):
        with self.assertRaises(ValueError):t.parse(b'{"qualified":true,"quali\\u0066ied":false}')

    def test_fixture_path_type_size_and_duplicate_guards_run_before_private_writes(self):
        raw=b'controlled archive';expected=dict(size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),git_blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest())
        def entries():
            rows=[zipfile.ZipInfo('distrusted/')]
            for n,size in [('distrusted_keycert.pem',5493),('distrusted_intermediate.pem',1241),('distrusted_root.pem',810)]:
                row=zipfile.ZipInfo('distrusted/'+n);row.file_size=size;row.flag_bits=1;row.external_attr=(stat.S_IFREG|0o600)<<16;rows.append(row)
            return rows
        for change in ('traversal','symlink','size','duplicate'):
            rows=entries()
            if change=='traversal':rows[1].filename='../distrusted_keycert.pem'
            if change=='symlink':rows[1].external_attr=(stat.S_IFLNK|0o600)<<16
            if change=='size':rows[1].file_size+=1
            if change=='duplicate':rows[1].filename=rows[2].filename
            archive=MagicMock();archive.__enter__.return_value=archive;archive.infolist.return_value=rows
            archive.read.side_effect=lambda row,pwd:b'x'*row.file_size
            with tempfile.TemporaryDirectory() as name,patch('zipfile.ZipFile',return_value=archive):
                directory=Path(name)/'certificates'
                with self.subTest(change=change),self.assertRaises(ValueError):t.fixture(raw,expected,directory)
                self.assertFalse(directory.exists())

    def test_fixture_wrong_hash_or_git_blob_refuses_before_private_files(self):
        for expected in [dict(size=3,sha256='0'*64,git_blob='0'*40),dict(size=3,sha256=hashlib.sha256(b'abc').hexdigest(),git_blob='0'*40)]:
            with tempfile.TemporaryDirectory() as name,self.assertRaises(ValueError):
                t.fixture(b'abc',expected,Path(name)/'certificates')

    def test_build_failure_exports_only_fixed_tokens_and_numeric_locations(self):
        raw=b'private-key=/private/never-export\nmake: *** [Makefile:1234: /private/never-export] Error 1\nfatal error: /private/never-export: No such file or directory\n'
        result=t.build_failure('baseline-build',2,raw)
        self.assertEqual(result['generated_makefile_line_mentions'],[1234])
        self.assertEqual(result['categories'],['missing-build-file','compiler-error'])
        self.assertFalse(result['raw_output_exported']);self.assertNotIn('private',json.dumps(result))
        self.assertNotIn('never-export',json.dumps(result))
        for phase,status in [('/private/never-export',1),('quilt-82',1),('baseline-build',True),('baseline-build',256)]:
            with self.assertRaises(ValueError):t.build_failure(phase,status,raw)

    def test_build_locations_and_identifiers_must_match_authenticated_public_source_allowlist(self):
        raw=b'Modules/_ssl.c:123:45: error: unrecognized identifier \'SSL_CTX_set_verify\'\n/private/token.c:3:4: error: \'private_token\'\n../escape.c:1:2: error\n'
        result=t.build_failure('baseline-build',2,raw,{'Modules/_ssl.c'},{'SSL_CTX_set_verify'})
        self.assertEqual(result['public_source_locations'],[dict(file='Modules/_ssl.c',line=123,column=45)])
        self.assertEqual(result['quoted_public_source_identifiers'],['SSL_CTX_set_verify'])
        self.assertNotIn('private',json.dumps(result));self.assertNotIn('escape',json.dumps(result))

    def test_error_locations_cannot_be_displaced_by_earlier_warnings(self):
        raw=b'Modules/_decimal/libmpdec/io.c:365:14: warning: bounded warning\n'*12
        raw+=b'Include/internal/pycore_gc.h:84:19: error: controlled compiler refusal\n'
        result=t.build_failure('baseline-build',2,raw,{'Modules/_decimal/libmpdec/io.c','Include/internal/pycore_gc.h'})
        self.assertEqual(result['public_source_locations'],[dict(file='Include/internal/pycore_gc.h',line=84,column=19)])

    def test_private_api_build_context_requires_one_exact_upstream_template(self):
        for count in (0,1,2):
            with tempfile.TemporaryDirectory() as name:
                tree=Path(name);(tree/'Modules').mkdir();p=tree/'Modules/Setup.stdlib.in'
                before='other upstream module\n'+'@MODULE_PYEXPAT_TRUE@pyexpat pyexpat.c\n'*count;p.write_text(before)
                if count==1:
                    value=t.module_context(tree);self.assertEqual(value['definitions'],['Py_BUILD_CORE_MODULE'])
                    self.assertEqual(p.read_text(),'other upstream module\n@MODULE_PYEXPAT_TRUE@pyexpat pyexpat.c -DPy_BUILD_CORE_MODULE\n')
                else:
                    with self.assertRaises(ValueError):t.module_context(tree)
                    self.assertEqual(p.read_text(),before)

    def test_witness_failure_uses_closed_module_names_and_never_copies_exception_text(self):
        for name,expected in [('_ssl','_ssl'),('/private/secret/module',None)]:
            error=ModuleNotFoundError('/private/never-export',name=name)
            value=t.validate_witness_failure(t.witness_failure(error,'baseline'),'baseline')
            self.assertEqual(value['missing_module'],expected);self.assertNotIn('private',json.dumps(value))
            self.assertFalse(value['qualified'])
            changed=dict(value,qualified=True)
            with self.assertRaises(ValueError):t.validate_witness_failure(changed,'baseline')
        value=t.witness_failure(t.Refusal('/private/never-export'),'baseline')
        self.assertIsNone(value['refusal']);self.assertNotIn('never-export',json.dumps(value))

    def test_extension_directory_is_an_explicit_owned_resource(self):
        with tempfile.TemporaryDirectory() as name:
            source=Path(name);(source/'build/lib.fixture-3.12').mkdir(parents=True)
            p=source/'pybuilddir.txt';p.write_text('build/lib.fixture-3.12\n')
            directory,raw=t.extension_directory(source)
            self.assertEqual(directory,(source/'build/lib.fixture-3.12').resolve());self.assertEqual(raw,p.read_bytes())
            for path in ('/private/never-export','../escape','build/../escape','build/lib.fixture\n/private/never-export','build/lib.missing'):
                p.write_text(path)
                with self.subTest(path=path),self.assertRaises(ValueError):t.extension_directory(source)

    def test_isolated_bootstrap_admits_only_owned_build_resource_before_script_imports(self):
        with tempfile.TemporaryDirectory() as name:
            source=Path(name);extensions=source/'build/lib.fixture-3.12';extensions.mkdir(parents=True)
            selector=source/'pybuilddir.txt';selector.write_text('build/lib.fixture-3.12\n')
            (extensions/'owned_probe.py').write_text('VALUE = 42\n')
            script=source/'witness.py';script.write_text('import owned_probe,sys\nassert sys.argv[1:] == ["witness"]\nprint(owned_probe.VALUE)\n')
            command=[sys.executable,'-I','-c',t.WITNESS_BOOTSTRAP,str(source),str(script),'witness']
            result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
            self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,b'42\n')
            selector.write_text('../escape')
            refused=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
            self.assertNotEqual(refused.returncode,0);self.assertEqual(refused.stdout,b'')


if __name__=='__main__':unittest.main()
