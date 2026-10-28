"""Fail-closed audit response/diagnostic controls; real scanner remains mandatory."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import audit_signer as audit


def clean(expected=None):
    return {'dependencies':[dict(name=name,version=version,vulns=[]) for name,version in (expected or audit.EXPECTED).items()]}


def affected():
    return {'dependencies':[dict(name='cryptography',version='3.4.8',vulns=[dict(id='PYSEC-2023-11',aliases=['CVE-2023-23931'])])]}


class AuditTests(unittest.TestCase):
    def run_audit(self, current=None, negative=None, current_status=0, negative_status=1, target=False):
        replies=[(0,'pip-audit 2.10.1\n'),
                 (current_status,json.dumps(clean() if current is None else current)),
                 (negative_status,json.dumps(affected() if negative is None else negative))]
        output=io.StringIO()
        with patch.object(audit.process,'run',side_effect=replies) as runner, contextlib.redirect_stdout(output):
            audit.audit(Path('/private/scanner/python'),target)
        return runner.call_args_list,output.getvalue()

    def test_complete_hash_audit_and_expected_historical_refusal_are_required(self):
        calls,output=self.run_audit()
        self.assertEqual(len(calls),3)
        arguments=calls[1].args[0]
        self.assertIn('--disable-pip',arguments)
        self.assertIn('--require-hashes',arguments)
        self.assertNotIn('--no-deps',arguments)
        self.assertIn('--no-deps',calls[2].args[0])
        environment=calls[1].args[2]
        self.assertEqual(environment['PIP_CONFIG_FILE'],audit.os.devnull)
        self.assertEqual(set(environment),{'PATH','HOME','PYTHONNOUSERSITE','PIP_CONFIG_FILE','PIP_NO_INPUT','PIP_DISABLE_PIP_VERSION_CHECK'})
        self.assertIn('All 8 pinned operator packages passed',output)

    def test_complete_target_inventory_uses_its_own_requirements_and_no_skips(self):
        expected=json.loads((audit.ROOT/'security/esp32s3-boot-dependencies.json').read_text())['operator_versions']
        calls,output=self.run_audit(current=clean(expected),target=True)
        self.assertEqual(calls[1].args[0][-1],str(audit.ROOT/'tools/boot/target-requirements.txt'))
        self.assertIn('All '+str(len(expected))+' pinned operator packages passed',output)
        bad=clean(expected);bad['dependencies'].pop()
        with self.assertRaisesRegex(audit.Refusal,'^current-dependencies$'):self.run_audit(current=bad,target=True)

    def test_query_failures_malformed_json_and_missing_shapes_are_distinct(self):
        for status,category in ((0,'current-response'),(1,'current-query')):
            for output in ('not-json-private-data','{}','{"dependencies":[]}',
                           '{"dependencies":[{"name":"x","version":"1","vulns":[],"vulns":[]}]}'):
                with self.assertRaisesRegex(audit.Refusal,'^'+category+'$'):
                    audit.dependencies('current',status,output)

    def test_missing_extra_duplicate_and_changed_dependencies_are_refused(self):
        base=clean();cases=[]
        missing=copy.deepcopy(base);missing['dependencies'].pop();cases.append(missing)
        extra=copy.deepcopy(base);extra['dependencies'].append(dict(name='unreviewed',version='1',vulns=[]));cases.append(extra)
        duplicate=copy.deepcopy(base);duplicate['dependencies'][1]=duplicate['dependencies'][0];cases.append(duplicate)
        changed=copy.deepcopy(base);changed['dependencies'][0]['version']='unreviewed';cases.append(changed)
        for report in cases:
            with self.assertRaisesRegex(audit.Refusal,'^current-dependencies$'):self.run_audit(current=report)

    def test_vulnerabilities_skips_and_failure_status_cannot_be_reported_clean(self):
        bad=clean();bad['dependencies'][0]['vulns']=[dict(id='public-advisory')]
        with self.assertRaisesRegex(audit.Refusal,'^current-vulnerabilities$'):self.run_audit(current=bad)
        for reason in ('unsupported',''):
            bad=clean();bad['dependencies'][0]['skip_reason']=reason
            with self.assertRaisesRegex(audit.Refusal,'^current-skipped$'):self.run_audit(current=bad)
        with self.assertRaisesRegex(audit.Refusal,'^current-status$'):self.run_audit(current_status=1)

    def test_negative_control_requires_vulnerability_exit_and_exact_affected_identity(self):
        cases=[]
        for key,value in (('name','other-package'),('version','50.0.2'),('vulns',[]),('skip_reason','unsupported')):
            report=affected();report['dependencies'][0][key]=value;cases.append(report)
        for report in cases:
            with self.assertRaisesRegex(audit.Refusal,'^affected-control$'):self.run_audit(negative=report)
        for status in (0,2,3):
            with self.assertRaisesRegex(audit.Refusal,'^affected-control$'):self.run_audit(negative_status=status)

    def test_negative_control_requires_the_real_expected_historical_advisory(self):
        report=affected();report['dependencies'][0]['vulns']=[dict(id='unrelated',aliases=[])]
        with self.assertRaisesRegex(audit.Refusal,'^affected-advisory$'):self.run_audit(negative=report)
        report['dependencies'][0]['vulns']=[dict(id='alias-only',aliases=['GHSA-w7pp-m8wf-vj6r'])]
        self.run_audit(negative=report)

    def test_malformed_vulnerability_records_are_refused(self):
        for entry in ('not-an-advisory',dict(id=[],aliases=[]),dict(id='PYSEC-2023-11',aliases=None),
                      dict(id='PYSEC-2023-11',aliases=[{}])):
            report=affected();report['dependencies'][0]['vulns']=[entry]
            with self.assertRaisesRegex(audit.Refusal,'^affected-response$'):self.run_audit(negative=report)

    def test_failed_version_or_execution_never_reaches_current_or_negative_queries(self):
        with patch.object(audit.process,'run',return_value=(0,'other scanner')) as runner:
            with self.assertRaisesRegex(audit.Refusal,'^scanner-version$'):audit.audit(Path('/private/scanner'))
            self.assertEqual(runner.call_count,1)
        with patch.object(audit.process,'run',side_effect=TimeoutError('private-path-or-token')) as runner:
            with self.assertRaisesRegex(audit.Refusal,'^version-execution$'):audit.audit(Path('/private/scanner'))
            self.assertEqual(runner.call_count,1)

    def test_main_publishes_only_fixed_stage_category_and_bounded_status(self):
        for error,expected in ((audit.Refusal('current-query',1),'current-query status=1'),
                               (audit.Refusal('affected-control',999),'affected-control'),
                               (ValueError('private-path-or-token'),'operation')):
            stream=io.StringIO()
            with patch.object(sys,'argv',['audit_signer.py','--audit-python','private-scanner-path']), \
                 patch.object(audit,'audit',side_effect=error),contextlib.redirect_stderr(stream):
                self.assertEqual(audit.main(),1)
            self.assertEqual(stream.getvalue(),'Signing operator advisory qualification failed: '+expected+'\n')
            self.assertNotIn('private',stream.getvalue())


if __name__=='__main__':unittest.main()
