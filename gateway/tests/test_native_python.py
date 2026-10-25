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


if __name__=='__main__':unittest.main()
