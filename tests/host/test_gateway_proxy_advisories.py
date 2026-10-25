"""An incomplete/changed upstream feed or stale proxy cannot become an admission pass."""
import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/gateway-proxy'))
import advisories as a


class ProxyAdvisoryTests(unittest.TestCase):
    def html(self,identity='CVE-2026-15176',fixed='1.31.7+, 1.30.6+',affected='0.1.29-1.31.6',severity='medium'):
        return f'<li><p>Owned fixture<br>Severity: {severity}<br><a>{identity}</a><br>Not vulnerable: {fixed}<br>Vulnerable: {affected}</p></li>'.encode()

    def pcre(self,identity='GHSA-r9hj-j2rw-4q3m',fixed='10.49'):
        return dict(ghsa_id=identity,cve_id='CVE-2026-103111',summary='Owned fixture',description='Metadata only',
                    vulnerabilities=[dict(package=dict(ecosystem='',name='PCRE2'),vulnerable_version_range='<=10.48',patched_versions=fixed,vulnerable_functions=[])],
                    severity='high',updated_at='2026-10-25T00:00:00Z',withdrawn_at=None)

    def fixture(self):
        nginx=a.nginx_articles(self.html()+self.html(identity='CVE-2009-4487',fixed='none',affected='all',severity='none'))
        pcre=a.pcre_articles(json.dumps([self.pcre()]).encode())
        policy=dict(schema=1,scope='hosted-minimal-native-camera-proxy',sources=dict(nginx=dict(version='1.30.6'),pcre2=dict(version='10.49')),
            advisory_review=dict(reviewed_at='2026-10-25T00:00:00+00:00',valid_until='2026-11-24T00:00:00+00:00',nginx=nginx,pcre2=pcre))
        return policy,nginx,pcre

    def test_fixed_stable_branch_overrides_broad_mainline_interval(self):
        row=a.nginx_articles(self.html())['CVE-2026-15176']
        for value in ['1.30.6','1.31.7','1.32.0']:self.assertFalse(a.nginx_affected(row,value))
        for value in ['1.24.0','1.30.5','1.31.6']:self.assertTrue(a.nginx_affected(row,value))
        row=a.nginx_articles(self.html(affected='1.31.2-1.31.6, 1.30.0-1.30.5'))['CVE-2026-15176']
        self.assertFalse(a.nginx_affected(row,'1.24.0'));self.assertTrue(a.nginx_affected(row,'1.30.5'))

    def test_universal_logging_behavior_is_retained_and_requires_runtime_evidence(self):
        policy,nginx,pcre=self.fixture();result=a.review(policy,nginx,pcre,datetime(2026,10,25,12,tzinfo=timezone.utc))
        self.assertEqual(result['nginx_unfixed'],['CVE-2009-4487']);self.assertTrue(result['universal_log_behavior_requires_runtime_control'])
        self.assertEqual(result['pcre2_unfixed'],[])

    def test_older_nginx_or_pcre_refuses_instead_of_suppressing_findings(self):
        policy,nginx,pcre=self.fixture()
        for component,value in [('nginx','1.30.5'),('pcre2','10.48')]:
            changed=copy.deepcopy(policy);changed['sources'][component]['version']=value
            with self.subTest(component=component),self.assertRaises(a.native.Refusal):a.review(changed,nginx,pcre,datetime(2026,10,25,12,tzinfo=timezone.utc))

    def test_removed_changed_new_and_empty_feeds_refuse_the_exact_review(self):
        policy,nginx,pcre=self.fixture();now=datetime(2026,10,25,12,tzinfo=timezone.utc)
        for n,p in [({},pcre),(nginx,{}),({**nginx,'CVE-2030-10000':next(iter(nginx.values()))},pcre),
                    (nginx,{**pcre,'GHSA-aaaa-bbbb-cccc':next(iter(pcre.values()))})]:
            with self.assertRaisesRegex(a.native.Refusal,'complete-current'):a.review(policy,n,p,now)
        changed=copy.deepcopy(nginx);changed['CVE-2026-15176']['text_sha256']='a'*64
        with self.assertRaisesRegex(a.native.Refusal,'complete-current'):a.review(policy,changed,pcre,now)

    def test_policy_expiry_and_schema_are_enforced(self):
        policy,nginx,pcre=self.fixture()
        with self.assertRaisesRegex(a.native.Refusal,'expired'):a.review(policy,nginx,pcre,datetime(2026,11,24,tzinfo=timezone.utc))
        policy['schema']=True
        with self.assertRaisesRegex(a.native.Refusal,'scope'):a.review(policy,nginx,pcre,datetime(2026,10,25,12,tzinfo=timezone.utc))

    def test_unknown_nginx_versions_duplicate_blocks_and_unparsed_cves_refuse(self):
        raw=self.html()
        for value in [b'',raw*2,raw+b'<p>CVE-2030-10000 outside a parsed article</p>',raw.replace(b'Not vulnerable: ',b'Missing field: ')]:
            with self.subTest(value=value[:50]),self.assertRaises(a.native.Refusal):a.nginx_articles(value)
        for value in ['1.30','1;command','v1.30.6','-1.0','1.30.6.1']:
            # PCRE's two-part versions are valid; reject only ambiguous formats.
            if value=='1.30':self.assertEqual(a.version(value),(1,30,0));continue
            with self.assertRaises(a.native.Refusal):a.version(value)
        row=a.nginx_articles(self.html(fixed='unrecognized'))['CVE-2026-15176']
        with self.assertRaises(a.native.Refusal):a.nginx_affected(row,'1.30.6')

    def test_windows_only_entry_is_parsed_explicitly_for_actual_linux_scope(self):
        row=a.nginx_articles(self.html(identity='CORE-2010-0121',fixed='0.8.33+, 0.7.65+',affected='nginx/Windows 0.7.52-0.8.32'))['CORE-2010-0121']
        self.assertFalse(a.nginx_affected(row,'0.8.30'))
        row['affected']='nginx/Windows malformed'
        with self.assertRaises(a.native.Refusal):a.nginx_affected(row,'1.30.6')

    def test_pcre_missing_fix_wrong_project_withdrawals_and_duplicates_refuse(self):
        row=self.pcre()
        values=[[],[row,row]]
        for key,value in [('withdrawn_at','2026-10-25T00:00:00Z'),('vulnerabilities',[])]:values.append([dict(row,**{key:value})])
        for kind in ['fix','project']:
            changed=copy.deepcopy(row)
            if kind=='fix':changed['vulnerabilities'][0]['patched_versions']=None
            else:changed['vulnerabilities'][0]['package']['name']='another project'
            values.append([changed])
        for rows in values:
            with self.subTest(rows=rows),self.assertRaises(a.native.Refusal):a.pcre_articles(json.dumps(rows).encode())
        raw=json.dumps([row]).encode();changed=copy.deepcopy(row);changed['description']='Upstream changed the advisory'
        self.assertNotEqual(a.pcre_articles(raw),a.pcre_articles(json.dumps([changed]).encode()))


if __name__=='__main__':unittest.main()
