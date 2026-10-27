"""Fail-closed package collection, advisory and installed-environment controls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import python_environment as environment


class PythonEnvironmentTests(unittest.TestCase):
    def encoded(self,items):return json.dumps(dict(dependencies=items)).encode()
    def item(self,**values):return dict(name='aiohttp',version='3.14.5',vulns=[],**values)

    def test_scanner_requires_complete_exact_unique_unskipped_package_inventory(self):
        good=self.item();expected={'aiohttp':'3.14.5'}
        self.assertEqual(environment.response(0,self.encoded([good]),expected),[])
        for status,items in [(0,[]),(0,[good,good]),(0,[dict(good,skip_reason='lookup failed')]),
                             (0,[dict(good,version='3.12.15')]),(1,[good]),
                             (0,[dict(good,vulns=[dict(id='GHSA-cq5v-8q36-5273')])])]:
            with self.subTest(status=status,items=items),self.assertRaises(environment.Refusal):
                environment.response(status,self.encoded(items),expected)
        with self.assertRaises(environment.Refusal):environment.parse(b'{"dependencies":[],"dependencies":[]}')

    def test_actual_advisory_findings_and_failure_status_are_both_required_for_control(self):
        item=dict(name='aiohttp',version='3.12.15',vulns=[dict(id='GHSA-cq5v-8q36-5273')])
        self.assertEqual(environment.response(1,self.encoded([item]),{'aiohttp':'3.12.15'},affected=True),['GHSA-cq5v-8q36-5273'])
        aliased=dict(item,vulns=[dict(id='PYSEC-2026-3545',aliases=['GHSA-cq5v-8q36-5273'])])
        self.assertIn('GHSA-cq5v-8q36-5273',environment.response(1,self.encoded([aliased]),{'aiohttp':'3.12.15'},affected=True))
        for status,changed in [(0,item),(1,dict(item,vulns=[])),(1,dict(item,vulns=[dict(id='unknown')]))]:
            with self.assertRaises(environment.Refusal):environment.response(status,self.encoded([changed]),{'aiohttp':'3.12.15'},affected=True)

    def test_requirements_cannot_omit_hashes_add_packages_or_escape_includes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'gateway/tests/requirements.txt';path.parent.mkdir(parents=True)
            config=root/'gateway/tests/python-environment.json'
            config.write_text(json.dumps(dict(schema=1,python='3.12',scanner='2.10.1',groups=dict(core={'jsonschema':'4.25.1'}))))
            correct='jsonschema==4.25.1 --hash=sha256:'+'a'*64+'\n'
            with mock.patch.object(environment,'ROOT',root),mock.patch.object(environment,'CONFIG',config):
                path.write_text(correct);self.assertEqual(environment.lock('core')[1],{'jsonschema':'4.25.1'})
                for wrong in ['jsonschema==4.25.1\n',correct+correct,correct+'other==1 --hash=sha256:'+'b'*64+'\n',
                              '-r /outside/requirements.txt\n','-r requirements.txt\n']:
                    path.write_text(wrong)
                    with self.assertRaises(environment.Refusal):environment.lock('core')

    def distribution(self,name,version='4.25.1',record='public wheel metadata'):
        return SimpleNamespace(metadata={'Name':name},version=version,read_text=lambda name:record)

    def test_inventory_refuses_extra_missing_duplicate_or_changed_installed_packages(self):
        good=self.distribution('jsonschema');bootstrap=self.distribution('pip','25.0.1')
        with mock.patch.object(environment,'lock',return_value=({}, {'jsonschema':'4.25.1'},None,None)),\
             mock.patch.object(environment,'inputs',return_value={}),\
             mock.patch.object(environment.sys,'version_info',(3,12)),\
             mock.patch.object(environment.sys,'platform','linux'):
            with mock.patch.object(environment.metadata,'distributions',return_value=[good,bootstrap]):
                report=environment.installed('core');self.assertEqual(report['packages'],{'jsonschema':'4.25.1'})
                self.assertFalse(report['native_advisory_closure']);self.assertEqual(report['bootstrap_packages'],{'pip':'25.0.1'})
                for key,wrong in [('packages',{}),('installed_record_sha256',{}),('physical_qualified',True),('python','3.14.0'),('inputs',{'missing':'0'*64})]:
                    changed=copy.deepcopy(report);changed[key]=wrong
                    with self.assertRaises(environment.Refusal):environment.validate(changed,'core')
            for distributions in [[],[good,good],[good,self.distribution('unexpected')],
                                  [self.distribution('jsonschema','0.1')],[self.distribution('jsonschema',record=None)]]:
                with mock.patch.object(environment.metadata,'distributions',return_value=distributions),self.assertRaises(environment.Refusal):environment.installed('core')

    def test_existing_report_refuses_before_scanner_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'report';path.write_bytes(b'keep')
            with mock.patch.object(sys,'argv',['environment','audit','--audit-python',sys.executable,'--report',str(path)]),\
                 mock.patch.object(environment,'request') as request,self.assertRaises(SystemExit):environment.main()
            request.assert_not_called();self.assertEqual(path.read_bytes(),b'keep')

    def test_scanner_deadline_returns_a_fixed_category(self):
        with tempfile.TemporaryDirectory() as directory,\
             mock.patch.object(environment.subprocess,'run',side_effect=subprocess.TimeoutExpired('private argument',1)),\
             self.assertRaisesRegex(environment.Refusal,'^audit-deadline$'):
            environment.request(sys.executable,[],Path(directory),1)


@unittest.skipUnless(os.environ.get('NXRS_TEST_PYTHON_ENVIRONMENT'),'requires isolated locked application environment')
class ActualPythonEnvironmentTests(unittest.TestCase):
    def test_real_frozen_installed_package_inventory(self):
        report=environment.installed(os.environ['NXRS_TEST_PYTHON_ENVIRONMENT'])
        self.assertTrue(report['qualified']);self.assertFalse(report['native_advisory_closure'])
        self.assertEqual(set(report['packages']),set(report['installed_record_sha256']))

if __name__=='__main__':unittest.main()
