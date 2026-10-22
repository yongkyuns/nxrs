"""Native advisory/report boundaries; no live network, SDK or storage operation."""
import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import backup_engine as engine


class NativeBoundaryTests(unittest.TestCase):
    def report(self,trace=None):
        config=dict(scanner_name='govulncheck',scanner_version='v1.9.0',scan_mode='binary',
                    scan_level='symbol',db=engine.DATABASE,db_last_modified='2026-10-21T00:27:29Z')
        rows=[dict(config=config),dict(SBOM=dict(go_version='go1.27.3',modules=[]))]
        if trace is not None:rows.append(dict(finding=dict(osv='GO-2026-5932',trace=trace)))
        return rows
    def encoded(self,rows):return '\n'.join(json.dumps(v) for v in rows).encode()
    def evaluate(self,rows):
        return engine.advisory_report(self.encoded(rows),'binary',dict(version='v1.9.0'))

    def test_successful_json_process_status_cannot_admit_an_affected_imported_package(self):
        for first in [dict(module='example.org/native',version='v1.0.0',package='example.org/native/parser'),
                      dict(module='example.org/native',version='v1.0.0',package='example.org/native/parser',function='Parse')]:
            with self.assertRaises(engine.Findings) as failure:self.evaluate(self.report([first]))
            self.assertEqual(failure.exception.report['affected_ids'],['GO-2026-5932'])
            self.assertEqual(failure.exception.report['imported_or_called'],1)

    def test_unimported_module_is_retained_in_evidence_without_an_ignore_rule(self):
        report=self.evaluate(self.report([dict(module='golang.org/x/crypto',version='v0.57.0')]))
        self.assertEqual(report['imported_or_called'],0)
        self.assertEqual(report['unimported_module_advisories'],[
            dict(id='GO-2026-5932',module='golang.org/x/crypto',version='v0.57.0')])

    def test_incomplete_wrong_mode_or_unpinned_scanner_is_not_a_clean_scan(self):
        rows=self.report()
        for modified in [[],rows[:1],rows[1:],rows+[rows[0]],
                         [dict(config=dict(rows[0]['config'],scan_mode='source')),rows[1]],
                         [dict(config=dict(rows[0]['config'],scanner_version='v0.0.0')),rows[1]],
                         [dict(config=dict(rows[0]['config'],db='https://other.invalid')),rows[1]]]:
            with self.assertRaises(engine.Failure):self.evaluate(modified)

    def test_truncated_json_and_malformed_finding_refuse_a_clean_scan(self):
        with self.assertRaises(ValueError):engine.advisory_report(b'{','binary',dict(version='v1.9.0'))
        rows=self.report([dict(module='example.org/native')]);rows[-1]['finding']['osv']='unrecognized'
        with self.assertRaises(engine.Failure):self.evaluate(rows)
        rows=self.report([])
        with self.assertRaises(engine.Failure):self.evaluate(rows)

    def test_upstream_main_module_advisory_cannot_hide_in_development_build_metadata(self):
        rows=[dict(path='example.org/other',vulns=[])]
        self.assertEqual(engine.main_module_report(json.dumps(rows).encode())['matching_entries'],0)
        rows.append(dict(path='github.com/restic/restic',vulns=[dict(id='GO-2026-0000')]))
        with self.assertRaises(engine.Failure):engine.main_module_report(json.dumps(rows).encode())
        for wrong in [{},[],[dict(path='github.com/restic/restic')]]:
            with self.assertRaises(engine.Failure):engine.main_module_report(json.dumps(wrong).encode())

    def test_archive_paths_cannot_escape_owned_build_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);payload=io.BytesIO()
            with tarfile.open(fileobj=payload,mode='w:gz') as archive:
                info=tarfile.TarInfo('../escaped');info.size=1;archive.addfile(info,io.BytesIO(b'x'))
            with self.assertRaises(tarfile.FilterError):engine.unpack(payload.getvalue(),root/'owned')
            self.assertFalse((root/'escaped').exists())

    def test_tampered_public_cache_is_not_silently_downloaded_or_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);sha=engine.digest(b'expected-public-input')
            (root/(sha+'.archive')).write_bytes(b'tampered')
            with mock.patch.object(engine,'download') as fetch:
                with self.assertRaises(engine.Failure):
                    engine.archive(dict(sha256=sha,url='https://example.invalid/input'),root,4096)
                fetch.assert_not_called()

    def test_existing_outputs_refuse_before_any_network_or_child(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'engine'
            for suffix in ['', '.cdx.json','.advisories.json','.provenance.json']:
                existing=Path(str(output)+suffix);existing.write_bytes(b'keep')
                with mock.patch.object(engine,'download') as fetch,mock.patch.object(engine,'command') as child:
                    with self.assertRaises(engine.Failure):engine.build('linux_amd64',output)
                    fetch.assert_not_called();child.assert_not_called()
                self.assertEqual(existing.read_bytes(),b'keep');existing.unlink()

    def test_inventory_refuses_replaced_module_or_unreviewed_native_settings(self):
        info=dict(GoVersion='go1.27.3',Main=dict(Path='github.com/restic/restic'),
                  Settings=[dict(Key=k,Value=v) for k,v in dict(CGO_ENABLED='0',GOOS='linux',GOARCH='amd64',
                    **{'-tags':engine.TAGS,'-trimpath':'true'}).items()],
                  Deps=[dict(Path='example.org/module',Version='v1.0.0',Sum='h1:public-module-identity')])
        recipe=dict(toolchains=dict(linux_amd64=dict(version='go1.27.3')),source=dict(version='0.19.1'))
        bom=engine.inventory(info,recipe,'linux_amd64','0'*64)
        self.assertEqual(bom['components'][0]['hashes'],[dict(alg='SHA-256',content='0'*64)])
        changed=copy.deepcopy(info);changed['Deps'][0]['Replace']=dict(Path='/unreviewed')
        with self.assertRaises(engine.Failure):engine.inventory(changed,recipe,'linux_amd64','0'*64)
        changed=copy.deepcopy(info);changed['Settings'][0]['Value']='1'
        with self.assertRaises(engine.Failure):engine.inventory(changed,recipe,'linux_amd64','0'*64)

if __name__=='__main__':unittest.main()
