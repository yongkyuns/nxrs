"""Media-specific artifact admission controls; no network or child process."""
import copy
from datetime import date
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import media_engine as engine


class MediaBoundaryTests(unittest.TestCase):
    def test_existing_file_symlink_fifo_and_sidecars_refuse_before_work(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'mediamtx'
            for suffix,kind in [('', 'file'),('', 'link'),('', 'fifo'),('.cdx.json','file'),
                                ('.advisories.json','file'),('.provenance.json','file')]:
                path=Path(str(output)+suffix)
                if kind=='file':path.write_bytes(b'keep')
                elif kind=='link':path.symlink_to(Path(directory)/'missing')
                else:os.mkfifo(path)
                with mock.patch.object(engine,'download') as fetch,mock.patch.object(engine,'command') as child:
                    with self.assertRaises(engine.Failure):engine.build('linux_amd64',output)
                    fetch.assert_not_called();child.assert_not_called()
                path.unlink()

    def tar(self,names):
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w:gz') as out:
            for name,kind in names:
                entry=tarfile.TarInfo(name)
                if kind=='link':entry.type=tarfile.SYMTYPE;entry.linkname='/outside'
                else:entry.size=1
                out.addfile(entry,io.BytesIO(b'x') if kind!='link' else None)
        return raw.getvalue()

    def test_archive_rejects_escape_alias_link_and_duplicates_before_extraction(self):
        for names in [[('../escape','file')],[('/root/file','file')],[('root/../escape','file')],
                      [('root/./file','file')],[('root/link','link')],[('root/file','file')]*2]:
            with self.subTest(names=names),tempfile.TemporaryDirectory() as directory:
                output=Path(directory)/'owned'
                with self.assertRaises(engine.Failure):engine.unpack_source(self.tar(names),output,'root')
                self.assertFalse(output.exists())

    def test_embedded_asset_is_exact_unique_and_content_bound(self):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as out:out.writestr('dist/hls.min.js',b'public browser asset')
        pin=dict(sha256=engine.digest(raw.getvalue()),entry='dist/hls.min.js',content_sha256=engine.digest(b'public browser asset'))
        self.assertEqual(engine.asset_bytes(raw.getvalue(),pin),b'public browser asset')
        for changed in [dict(pin,sha256='0'*64),dict(pin,content_sha256='0'*64),dict(pin,entry='other')]:
            with self.assertRaises(engine.Failure):engine.asset_bytes(raw.getvalue(),changed)

    def advisory(self):
        return dict(id='GO-2026-4989',affected=[dict(package=dict(ecosystem='Go',name=engine.MODULE),
            ranges=[dict(type='SEMVER',events=[dict(introduced='1.15.0'),dict(fixed='1.18.0')])])])

    def test_main_module_development_metadata_cannot_hide_advisories(self):
        advisory=self.advisory();raw=json.dumps(advisory).encode()
        recipe=dict(source=dict(version='1.21.2'),main_module_review=dict(owner='maintainers',
            issue='https://github.com/yongkyuns/nxrs/issues/38',expires='2026-11-23',
            advisories=[dict(id=advisory['id'],sha256=engine.digest(raw))]))
        index=json.dumps([dict(path=engine.MODULE,vulns=[dict(id=advisory['id'])])]).encode()
        fetch=lambda url,limit:raw
        self.assertFalse(engine.main_module_report(index,recipe,fetch,date(2026,10,24))['affected'])
        for changed_index,changed_recipe,changed_raw,today in [
            (index,recipe,raw,date(2026,11,23)),(b'[]',recipe,raw,date(2026,10,24)),
            (index,recipe,raw+b' ',date(2026,10,24)),
            (index,dict(recipe,source=dict(version='1.16.0')),raw,date(2026,10,24)),
            (json.dumps([dict(path=engine.MODULE,vulns=[dict(id='GO-2026-9999')])]).encode(),recipe,raw,date(2026,10,24))]:
            with self.assertRaises(engine.Failure):engine.main_module_report(changed_index,changed_recipe,lambda u,n:changed_raw,today)
        self.assertTrue(engine.affected_version(advisory,'1.15.0'))
        self.assertFalse(engine.affected_version(advisory,'1.18.0'))
        advisory['affected'][0]['ranges'][0]['events']=[dict(introduced='0'),dict(last_affected='1.18.0')]
        with self.assertRaises(engine.Failure):engine.affected_version(advisory,'1.21.2')

    def test_complete_inventory_refuses_omission_replacement_and_native_settings(self):
        modules=[dict(Path='example.org/module',Version='v1.0.0',Sum='h1:public-identity')]
        info=dict(GoVersion='go1.27.3',Path=engine.MODULE,Main=dict(Path=engine.MODULE,Version='(devel)'),
            Settings=[dict(Key=k,Value=v) for k,v in dict(CGO_ENABLED='0',GOOS='linux',GOARCH='amd64',**{'-trimpath':'true'}).items()],Deps=modules)
        recipe=dict(go_version='go1.27.3',version='1.21.2-nxrs.1',asset=dict(version='v1.7.3',content_sha256='0'*64),
            artifacts=dict(linux_amd64=dict(module_count=1,modules_sha256=engine.module_digest([list(modules[0].values())]))))
        self.assertEqual(len(engine.inventory(info,recipe,'linux_amd64','0'*64)['components']),4)
        for change in ['missing','replacement','duplicate','settings','malformed','toolchain']:
            changed=copy.deepcopy(info)
            if change=='missing':changed['Deps']=[]
            if change=='replacement':changed['Deps'][0]['Replace']=dict(Path='/local')
            if change=='duplicate':changed['Deps']*=2
            if change=='settings':changed['Settings'][0]['Value']='1'
            if change=='malformed':changed['Deps']=[None]
            if change=='toolchain':changed['GoVersion']='go1.27.2'
            with self.subTest(change=change),self.assertRaises(engine.Failure):engine.inventory(changed,recipe,'linux_amd64','0'*64)

    def test_scan_only_control_requires_one_exact_affected_executable(self):
        raw=self.tar([('mediamtx','file')]);self.assertEqual(engine.negative_binary(raw,engine.digest(b'x')),b'x')
        for data,sha in [(raw,'0'*64),(self.tar([('mediamtx','file')]*2),engine.digest(b'x')),
                         (self.tar([('mediamtx','link')]),engine.digest(b'x'))]:
            with self.assertRaises(engine.Failure):engine.negative_binary(data,sha)

if __name__=='__main__':unittest.main()
