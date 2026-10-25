"""Native payload, source extraction and official feed refusal controls."""
import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import native_python as native


class NativePythonTests(unittest.TestCase):
    def record(self):
        return dict(dataType='CVE_RECORD', cveMetadata=dict(cveId='CVE-2026-35189', state='PUBLISHED'),
            containers=dict(cna=dict(affected=[dict(vendor='OpenSSL', product='OpenSSL', defaultStatus='unaffected', versions=[
                dict(version='4.0.0', lessThan='4.0.3', status='affected', versionType='semver'),
                dict(version='3.5.0', lessThan='3.5.9', status='affected', versionType='semver')])])) )

    def test_current_and_affected_openssl_ranges_and_boundaries(self):
        for version, count in [('4.0.0',1), ('4.0.2',1), ('4.0.3',0), ('3.5.4',1), ('3.5.9',0)]:
            self.assertEqual(len(native.openssl_findings(self.record(), version, 'CVE-2026-35189')), count)
        value=self.record();value['containers']['cna']['affected'][0]['versions'][0].pop('lessThan')
        self.assertTrue(native.openssl_findings(value,'4.0.3','CVE-2026-35189'))

    def test_identity_schema_and_uninterpretable_ranges_refuse(self):
        for change in ['identity','product','status','range','future']:
            value=self.record();p=value['containers']['cna']['affected'][0]
            if change=='identity':value['cveMetadata']['cveId']='CVE-2026-99999'
            if change=='product':p['product']='other'
            if change=='status':p['defaultStatus']='unknown'
            if change=='range':p['versions'][0]['lessThan']='4.0.3-pre'
            if change=='future':p['versions'][0]['changes']=[]
            with self.subTest(change=change),self.assertRaises(native.Refusal):native.openssl_findings(value,'4.0.3','CVE-2026-35189')

    def ffmpeg(self):
        return ''.join('<h2>FFmpeg '+b+'</h2><h3>'+b+'.2</h3><pre>CVE-2026-30999 CVE-2026-8461</pre><h3>'+b+'</h3><pre>CVE-2025-12343</pre>' for b in ['9.0','8.1','8.0','7.1','6.1','5.1']).encode()

    def test_ffmpeg_gate_requires_every_later_fix_on_actual_branch(self):
        self.assertEqual(native.ffmpeg_findings(self.ffmpeg(),'8.1.1'),['CVE-2026-30999','CVE-2026-8461'])
        self.assertEqual(native.ffmpeg_findings(self.ffmpeg(),'8.1.2'),[])
        for raw, selected in [(b'<html>network login</html>','8.1.2'),(self.ffmpeg(),'10.0.0'),
                              (self.ffmpeg()+b'<h2>FFmpeg 8.1</h2>','8.1.2'),
                              (self.ffmpeg().replace(b'CVE-',b'MISSING-'),'8.1.2')]:
            with self.assertRaises(native.Refusal):native.ffmpeg_findings(raw,selected)

    def test_tar_rejects_escape_duplicate_and_device_entries(self):
        for kind in ['escape','duplicate','device','symlink']:
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w:gz') as out:
                name='root/../outside' if kind=='escape' else 'root/item'
                item=tarfile.TarInfo(name);item.size=0
                if kind=='device':item.type=tarfile.CHRTYPE
                if kind=='symlink':item.type=tarfile.SYMTYPE;item.linkname='../outside'
                out.addfile(item)
                if kind=='duplicate':out.addfile(item)
            with tempfile.TemporaryDirectory() as directory,self.assertRaises(native.Refusal):
                native.extract(stream.getvalue(),Path(directory),'root')

    def test_replacement_wheels_use_direct_hashed_paths_without_index_fallback(self):
        value=native.install_requirements({'aiortc':'1.15.0','av':'19.0.2'},
            {'aiortc':['a'*64],'av':['b'*64]}, {'aiortc':{'filename':'aiortc-1.15.0-py3-none-any.whl'}},Path('/owned/wheels'))
        self.assertIn('aiortc @ file:///owned/wheels/aiortc-1.15.0-py3-none-any.whl --hash=sha256:'+'a'*64,value)
        self.assertIn('av==19.0.2 --hash=sha256:'+'b'*64,value)
        self.assertNotIn('aiortc==',value)

    def test_existing_output_is_refused_before_advisory_or_media_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'public.json';report.write_bytes(b'keep')
            with mock.patch.object(native,'audit_native') as audit,self.assertRaises(native.Refusal):
                native.qualify(report,Path(sys.executable))
            audit.assert_not_called();self.assertEqual(report.read_bytes(),b'keep')

    def test_existing_build_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory,mock.patch.object(native.sys,'version_info',(3,12)),\
             mock.patch.object(native,'fetch') as fetch,self.assertRaises(native.Refusal):
            native.prepare(Path(directory))
        fetch.assert_not_called()

    def test_native_record_mismatch_is_refused(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'native.so';path.write_bytes(b'altered native library')
            class Entry:
                hash=SimpleNamespace(mode='sha256',value='wrong')
                def __str__(self):return 'native.so'
            distribution=SimpleNamespace(files=[Entry()],locate_file=lambda item:path)
            with mock.patch.object(native.sys,'prefix',directory),mock.patch.object(native.metadata,'distribution',return_value=distribution),self.assertRaisesRegex(native.Refusal,'native-payload-integrity'):
                native.installed_files()




class NativeAdmissionTests(unittest.TestCase):
    def evidence(self):
        recipe=json.loads(native.packages.bounded(native.ROOT/'gateway/tests/native-python.json'))
        return dict(schema=1,scope='admitted-pinned-native-python-environment',qualified=True,inputs=native.inputs(),
            qualification_sha256='a'*64,checkout='a'*40,srtp_self_tests=True,decoded_h264_frames=6,joined_cleanup=True,native_http_parser=True,
            observed=dict(ffmpeg='9.0.2',srtp='2.7.0',srtp_openssl='4.0.3',cryptography_openssl='4.0.3'),
            source_archives={n:p['sha256'] for n,p in [('openssl',recipe['openssl']),('libsrtp',recipe['libsrtp']),*recipe['python_sources'].items()]},
            aiortc_backport=recipe['aiortc_backport']['commit'],native_payload_sha256={n:{'public.so':'a'*64} for n in ['av','pylibsrtp','cryptography']},
            actual_python_package_advisories=dict(packages=26,findings=[]),
            advisories=dict(scope='FFmpeg selected-branch published fixes and OpenSSL 4.0 official CVE records',complete_native_advisory_coverage=False,
                ffmpeg=dict(url=native.FFMPEG,sha256='a'*64,findings=[],affected_control=dict(version='8.1.1',executed=False,advisory_ids=['CVE-2026-8461','CVE-2026-30999'])),
                openssl=dict(url=native.OPENSSL,sha256='a'*64,findings=[],records={'CVE-2026-35189':'a'*64},affected_control=dict(version='3.5.4',executed=False,advisory_ids=['CVE-2026-35189']))),
            built_wheels={n:dict(sha256='a'*64) for n in ['aiortc','pylibsrtp']},native_advisory_closure=False,os_advisory_closure=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)

    def test_native_admission_refuses_missing_changed_or_unexecuted_evidence(self):
        valid=self.evidence();native.validate_evidence(valid)
        changes=[('inputs',{}),('qualified',False),('srtp_self_tests',False),('joined_cleanup',False),
                 ('decoded_h264_frames',0),('decoded_h264_frames',6.0),('qualification_sha256','missing'),
                 ('source_archives',{}),('aiortc_backport','a'*40),('native_payload_sha256',{}),
                 ('built_wheels',{}),('physical_qualified',True),('native_advisory_closure',True),('advisories',{})]
        for key,wrong in changes:
            value=copy.deepcopy(valid);value[key]=wrong
            with self.subTest(key=key),self.assertRaises((native.Refusal,KeyError)):native.validate_evidence(value)
        for component in ['ffmpeg','openssl']:
            value=copy.deepcopy(valid);value['advisories'][component]['affected_control']['advisory_ids']=[]
            with self.assertRaises(native.Refusal):native.validate_evidence(value)

    def test_native_runtime_refuses_when_no_qualification_was_written(self):
        with tempfile.TemporaryDirectory() as directory,mock.patch.object(native.sys,'prefix',directory),self.assertRaises(FileNotFoundError):
            native.runtime_evidence()

    def test_preparing_an_existing_external_runtime_refuses_before_downloading(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory)/'new-work';runtime=Path(directory)/'existing';runtime.mkdir()
            with mock.patch.object(native.sys,'version_info',(3,12)),mock.patch.object(native.subprocess,'check_output',side_effect=['a'*40,b'']),\
                 mock.patch.object(native,'fetch') as fetch,self.assertRaisesRegex(native.Refusal,'new-runtime-directory'):
                native.prepare(work,runtime)
            fetch.assert_not_called();self.assertFalse(work.exists())

    def test_public_runtime_permissions_do_not_follow_interpreter_symlinks(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);runtime=base/'runtime';runtime.mkdir(mode=0o700)
            private=base/'outside';private.write_bytes(b'outside');private.chmod(0o600)
            (runtime/'interpreter').symlink_to(private)
            executable=runtime/'executable';executable.write_bytes(b'public code');executable.chmod(0o700)
            receipt=runtime/'receipt';receipt.write_bytes(b'public hashes');receipt.chmod(0o600)
            native.public_runtime(runtime)
            self.assertEqual(private.stat().st_mode&0o777,0o600)
            self.assertEqual(executable.stat().st_mode&0o777,0o755)
            self.assertEqual(receipt.stat().st_mode&0o777,0o644)
            self.assertEqual(runtime.stat().st_mode&0o777,0o755)

if __name__=='__main__':unittest.main()
