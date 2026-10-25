"""Owned-source extraction, host gating and exact proxy admission controls."""
import copy
from datetime import datetime,timedelta,timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/gateway-proxy'))
import qualify as q
spec=importlib.util.spec_from_file_location('proxy_boundary_control',ROOT/'tools/gateway-proxy/boundary.py')
boundary=importlib.util.module_from_spec(spec);spec.loader.exec_module(boundary)
e=q.evidence


class GatewayProxyTests(unittest.TestCase):
    def test_personal_host_refuses_before_files_native_commands_or_network(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{},clear=True),patch.object(q.native,'command') as command,patch.object(q,'Work') as work:
            path=Path(d)/'absent'
            with self.assertRaisesRegex(q.native.Refusal,'hosted-ubuntu-required'):q.run(path)
            command.assert_not_called();work.assert_not_called();self.assertFalse(path.exists())

    def test_exact_official_source_urls_keys_and_archive_roots_are_required(self):
        policy=json.loads((ROOT/q.advisories.POLICY).read_bytes());q.validate_sources(policy)
        for key,value in [('url','https://example.invalid/source.tar.gz'),('root','../nginx-1.30.6'),('sha256','bad')]:
            changed=copy.deepcopy(policy);changed['sources']['nginx'][key]=value
            with self.subTest(key=key),self.assertRaises(q.native.Refusal):q.validate_sources(changed)
        changed=copy.deepcopy(policy);changed['sources']['nginx']['key']['fingerprint']='A'*40
        with self.assertRaises(q.native.Refusal):q.validate_sources(changed)

    def archive(self,path,entries):
        with tarfile.open(path,'w') as archive:
            for name,kind in entries:
                info=tarfile.TarInfo(name);info.type=kind;info.mode=0o755 if kind==tarfile.DIRTYPE else 0o644
                if kind==tarfile.REGTYPE:info.size=4;archive.addfile(info,io.BytesIO(b'own!'))
                else:info.linkname='outside';archive.addfile(info)

    def test_source_extraction_writes_only_owned_regular_files_and_directories(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);archive=root/'source.tar';out=root/'out';out.mkdir()
            self.archive(archive,[('nginx-1.30.6/',tarfile.DIRTYPE),('nginx-1.30.6/src/a.c',tarfile.REGTYPE)])
            target=q.extract(archive,out,'nginx-1.30.6');self.assertEqual((target/'src/a.c').read_bytes(),b'own!')
            self.assertEqual((target/'src/a.c').stat().st_mode&0o777,0o600)
            with self.assertRaises(q.native.Refusal):q.extract(archive,out,'nginx-1.30.6')

    def test_link_traversal_duplicate_wrong_root_and_oversized_source_refuse(self):
        cases=[[('nginx-1.30.6/a',tarfile.SYMTYPE)],[('nginx-1.30.6/a',tarfile.LNKTYPE)],
               [('../outside',tarfile.REGTYPE)],[('/nginx-1.30.6/a',tarfile.REGTYPE)],[('other/a',tarfile.REGTYPE)],
               [('nginx-1.30.6/a',tarfile.REGTYPE)]*2,[('nginx-1.30.6/../outside',tarfile.REGTYPE)],
               [('nginx-1.30.6//a',tarfile.REGTYPE)]]
        for entries in cases:
            with self.subTest(entries=entries),tempfile.TemporaryDirectory() as d:
                root=Path(d);archive=root/'source.tar';out=root/'out';out.mkdir();self.archive(archive,entries)
                with self.assertRaises(q.native.Refusal):q.extract(archive,out,'nginx-1.30.6')
                self.assertFalse((root/'outside').exists())
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);archive=root/'source.tar'
            with tarfile.open(archive,'w') as a:
                info=tarfile.TarInfo('nginx-1.30.6/oversized');info.size=128*1024*1024+1;a.fileobj.write(info.tobuf())
            out=root/'out';out.mkdir()
            with self.assertRaisesRegex(q.native.Refusal,'extraction-budget'):q.extract(archive,out,'nginx-1.30.6')

    def test_http_upstream_core_is_distinct_from_excluded_stream_and_filter_modules(self):
        names=['ngx_core_module','ngx_http_ssl_module','ngx_http_proxy_module','ngx_http_rewrite_module',
               'ngx_http_limit_req_module','ngx_http_limit_conn_module','ngx_http_upstream_module']
        def text(values):return ''.join('    &'+n+',\n' for n in values)
        self.assertEqual(q.resolved_modules(text(names)),names)
        for bad in ['ngx_stream_module','ngx_mail_module','ngx_http_v2_module','ngx_http_v3_filter_module',
                    'ngx_http_gzip_filter_module','ngx_http_charset_filter_module','ngx_http_upstream_zone_module']:
            with self.subTest(module=bad),self.assertRaisesRegex(q.native.Refusal,'no-unselected'):q.resolved_modules(text(names+[bad]))
        with self.assertRaisesRegex(q.native.Refusal,'required-proxy'):q.resolved_modules(text(names+names))

    def test_generated_log_policy_refuses_both_changed_and_extra_destinations(self):
        text='http {\n    access_log off;\n}\nerror_log /dev/null emerg;\nssl_session_tickets off;\n'
        boundary.log_policy(text)
        for changed in [text.replace('/dev/null','/tmp/owned.log'),text.replace('access_log off;','access_log /tmp/owned.log;'),
                        text+'error_log syslog:server=127.0.0.1;\n',text.replace('ssl_session_tickets off;','ssl_session_tickets on;')]:
            with self.assertRaises(q.native.Refusal):boundary.log_policy(changed)

    def receipt(self):
        policy=json.loads((ROOT/q.advisories.POLICY).read_bytes());now=datetime.now(timezone.utc)
        policy['advisory_review']['reviewed_at']=(now-timedelta(hours=1)).isoformat();policy['advisory_review']['valid_until']=(now+timedelta(days=1)).isoformat()
        review=q.advisories.review(policy,policy['advisory_review']['nginx'],policy['advisory_review']['pcre2'])
        value=dict(schema=1,scope=e.SCOPE,qualified=True,verification_status='complete',checkout=dict(clean=True,commit='a'*40),inputs={'owned':'b'*64},sources=policy['sources'],
            https_boundary_tests=4,https_boundary_failures=0,https_boundary_skips=0,pcre2_probe_passed=True,pcre2_jit=False,pcre2_version='10.49',
            advisory_review=review,log_discard_qualified=True,log_configuration_refusals=2,source_version_refusals=2,changed_signature_refusals=2,
            source_signatures={n:dict(signer=v['key']['fingerprint'],sha256=v['sha256'],changed_archive_refused=True) for n,v in policy['sources'].items()},
            binary_sha256='c'*64,static_pcre_archive_sha256='d'*64,configuration_sha256='e'*64,link_makefile_sha256='f'*64,
            libraries={'/usr/lib/owned-libc.so':'c'*64},full_native_advisory_closure=False,bootstrap_environment_qualified=False,
            signature_key_revocation_lifecycle_qualified=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)
        return policy,value

    def test_exact_runtime_receipt_admits_only_current_sources_and_scope(self):
        policy,value=self.receipt()
        with patch.object(Path,'read_bytes',return_value=json.dumps(policy).encode()),patch.object(e,'digest',return_value='c'*64):
            proof=e.admit(value,ROOT,'a'*40,{'owned':'b'*64});self.assertEqual(proof['binary_sha256'],'c'*64)
            for field,changed in [('schema',True),('qualified',False),('verification_status','pending'),('inputs',{}),('https_boundary_tests',0),
                                  ('https_boundary_skips',1),('pcre2_jit',True),('log_discard_qualified',False),('log_configuration_refusals',0),
                                  ('changed_signature_refusals',0),('source_version_refusals',0),('libraries',{}),('physical_qualified',True),('binary_sha256','bad')]:
                invalid=copy.deepcopy(value);invalid[field]=changed
                with self.subTest(field=field),self.assertRaises(ValueError):e.admit(invalid,ROOT,'a'*40,{'owned':'b'*64})
            bad=copy.deepcopy(value);bad['advisory_review']['pcre2_articles']=0
            with self.assertRaises(ValueError):e.admit(bad,ROOT,'a'*40,{'owned':'b'*64})
            bad=copy.deepcopy(value);bad['source_signatures']['nginx']['signer']='F'*40
            with self.assertRaises(ValueError):e.admit(bad,ROOT,'a'*40,{'owned':'b'*64})
            for name in ['/usr/lib/../../etc/owned','/tmp/outside','/usr/lib/libpcre2-8.so']:
                bad=copy.deepcopy(value);bad['libraries']={name:'c'*64}
                with self.subTest(name=name),self.assertRaises(ValueError):e.admit(bad,ROOT,'a'*40,{'owned':'b'*64})
        policy['advisory_review']['valid_until']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        with patch.object(Path,'read_bytes',return_value=json.dumps(policy).encode()),patch.object(e,'digest',return_value='c'*64),self.assertRaisesRegex(ValueError,'review-window'):
            e.admit(value,ROOT,'a'*40,{'owned':'b'*64})

    def test_receipt_symlinks_fifo_and_duplicate_fields_never_admit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);receipt=root/'receipt';receipt.write_text('{"schema":1,"schema":1}');link=root/'link';link.symlink_to(receipt)
            with self.assertRaises(ValueError):e.read(receipt)
            with self.assertRaises(OSError):e.read(link)
            fifo=root/'fifo';os.mkfifo(fifo,0o600)
            with self.assertRaisesRegex(ValueError,'regular-proxy'):e.read(fifo)


if __name__=='__main__':unittest.main()
