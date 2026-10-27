"""Owned-source extraction, host gating and exact proxy admission controls."""
import copy
from contextlib import contextmanager,ExitStack
from datetime import datetime,timedelta,timezone
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import sys
import stat
import tarfile
import tempfile
import shutil,subprocess
import unittest
from unittest.mock import patch,Mock
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/gateway-proxy'))
spec=importlib.util.spec_from_file_location('proxy_source_qualification_fixture',ROOT/'tools/gateway-proxy/qualify.py')
q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)
spec=importlib.util.spec_from_file_location('proxy_boundary_control',ROOT/'tools/gateway-proxy/boundary.py')
boundary=importlib.util.module_from_spec(spec);spec.loader.exec_module(boundary)
e=q.evidence


class GatewayProxyTests(unittest.TestCase):
    def test_service_source_vector_covers_actual_fixture_and_build_materials(self):
        # Import build helpers only in the fixture. A collector describing their
        # materials must not load their native compression libraries into itself.
        import media_engine,python_environment
        expected=set(media_engine.INPUTS)|set(python_environment.INPUTS)|{
            'hal/camera/src/api.rs','service/stream/src/lib.rs','service/stream/src/server.rs','service/stream/src/worker.rs',
            'gateway/deploy/strict_vpn.py','gateway/tests/revocation_reference.py',
            'gateway/tests/sandbox_probe.py','gateway/tests/fetch_mediamtx.py'}
        self.assertTrue(expected<=set(e.INPUTS))

    def test_personal_host_refuses_before_files_native_commands_or_network(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{},clear=True),patch.object(q.native,'command') as command,patch.object(q,'Work') as work:
            path=Path(d)/'absent'
            with self.assertRaisesRegex(q.native.Refusal,'hosted-ubuntu-required'):q.run(path)
            command.assert_not_called();work.assert_not_called();self.assertFalse(path.exists())

    def test_child_environment_retains_only_existing_public_host_markers(self):
        flags=dict(GITHUB_ACTIONS='fixture-existing-action-marker',RUNNER_ENVIRONMENT='fixture-existing-runner-marker',GH_TOKEN='synthetic-not-inherited',PRIVATE_VALUE='synthetic-not-inherited')
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,flags,clear=True):
            work=q.Work(Path(d))
            self.assertEqual(work.env['GITHUB_ACTIONS'],flags['GITHUB_ACTIONS']);self.assertEqual(work.env['RUNNER_ENVIRONMENT'],flags['RUNNER_ENVIRONMENT'])
            self.assertNotIn('GH_TOKEN',work.env);self.assertNotIn('PRIVATE_VALUE',work.env)

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

    def generated_release(self,archive):
        # Archive order is deliberately different from the signed build order.
        rows=[('Makefile',b'all: generated\n\t@test "$$(cat generated)" = released\ngenerated: input\n\t@exit 73\n',15),
              ('generated',b'released\n',20),('input',b'older input\n',10)]
        with tarfile.open(archive,'w') as source:
            directory=tarfile.TarInfo('nginx-1.30.6/');directory.type=tarfile.DIRTYPE;directory.mtime=5;source.addfile(directory)
            for name,raw,mtime in rows:
                member=tarfile.TarInfo('nginx-1.30.6/'+name);member.size=len(raw);member.mtime=mtime;source.addfile(member,io.BytesIO(raw))

    def test_extraction_preserves_signed_release_order_instead_of_archive_order(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);archive=root/'source.tar';self.generated_release(archive)
            source=q.extract(archive,root,'nginx-1.30.6')
            self.assertEqual(source.stat().st_mtime_ns,5_000_000_000)
            for name,mtime in [('Makefile',15),('generated',20),('input',10)]:
                self.assertEqual((source/name).stat().st_mtime_ns,mtime*1_000_000_000)
                self.assertEqual((source/name).stat().st_mode&0o777,0o600)
            self.assertEqual((source/'generated').read_bytes(),b'released\n')

    def test_gnu_make_keeps_generated_release_and_changed_input_control_requires_regeneration(self):
        make=None
        for command in filter(None,(shutil.which('gmake'),shutil.which('make'))):
            version=subprocess.run([command,'--version'],capture_output=True,timeout=10)
            if re.search(rb'GNU Make (?:[4-9]|[1-9][0-9]+)\.',version.stdout):make=command;break
        if make is None:self.skipTest('GNU Make4 timestamp witness unavailable')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);archive=root/'source.tar';self.generated_release(archive)
            source=q.extract(archive,root,'nginx-1.30.6')
            def run():return subprocess.run([make,'-s','all'],cwd=source,capture_output=True,timeout=10)
            self.assertEqual(run().returncode,0)
            # A real source edit must still request regeneration and fail.
            os.utime(source/'input',(30,30))
            self.assertNotEqual(run().returncode,0)

    def test_source_timestamps_outside_the_bounded_archive_contract_refuse(self):
        for mtime in (-1,4294967296,float('inf'),float('nan')):
            with self.subTest(mtime=mtime),tempfile.TemporaryDirectory() as d:
                root=Path(d);archive=root/'source.tar'
                with tarfile.open(archive,'w',format=tarfile.PAX_FORMAT) as source:
                    member=tarfile.TarInfo('nginx-1.30.6/file');member.pax_headers={'mtime':str(mtime)};member.size=1;source.addfile(member,io.BytesIO(b'x'))
                with self.assertRaisesRegex(q.native.Refusal,'bounded-proxy-source-timestamp'):
                    q.extract(archive,root,'nginx-1.30.6')

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

    def test_live_failure_context_never_exports_exception_messages_or_locals(self):
        private='PRIVATE_SYNTHETIC_ARGUMENT'
        try:boundary.log_policy(private)
        except q.native.Refusal as error:context=boundary.failure_context(error)
        self.assertTrue(context);self.assertNotIn(private,json.dumps(context))
        self.assertTrue(all(row['source']=='tools/gateway-proxy/boundary.py' and type(row['line']) is int for row in context))

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


class ProxyDescriptorSnapshotTests(unittest.TestCase):
    @contextmanager
    def fixture(self,snapshots,opened,info,*,polls=None):
        process=Mock(pid=123)
        process.poll.return_value=None
        if polls is not None:process.poll.side_effect=polls
        with ExitStack() as stack:
            listing=stack.enter_context(patch.object(Path,'iterdir',side_effect=[iter(paths) for paths in snapshots]))
            stack.enter_context(patch.object(boundary.os,'O_PATH',0x200000,create=True))
            stack.enter_context(patch.object(boundary.os,'stat',return_value=SimpleNamespace(st_mode=stat.S_IFCHR,st_rdev=9)))
            opening=stack.enter_context(patch.object(boundary.os,'open',side_effect=opened))
            stack.enter_context(patch.object(boundary.os,'fstat',side_effect=lambda fd:info[fd]))
            closing=stack.enter_context(patch.object(boundary.os,'close'))
            yield process,listing,opening,closing

    def descriptors(self):return [Path('/proc/123/fd/1'),Path('/proc/123/fd/2')]
    def null(self):return SimpleNamespace(st_mode=stat.S_IFCHR,st_rdev=9)
    def socket(self):return SimpleNamespace(st_mode=stat.S_IFSOCK,st_rdev=0)

    def test_disappearing_socket_requires_a_complete_new_snapshot_with_pinned_handles(self):
        paths=self.descriptors()
        with self.fixture([paths,paths],[10,FileNotFoundError(),11,12],
                          {10:self.null(),11:self.null(),12:self.socket()}) as (process,listing,opening,closing):
            boundary.inspect_log_destinations(process)
            self.assertEqual(listing.call_count,2)
            self.assertEqual([call.args[0] for call in closing.call_args_list],[10,12,11])
            self.assertTrue(all(call.args[1]==boundary.os.O_PATH|boundary.os.O_CLOEXEC for call in opening.call_args_list))

    def test_regular_file_refuses_even_in_a_snapshot_with_a_disappearing_descriptor(self):
        with self.fixture([self.descriptors()],[FileNotFoundError(),10],
                          {10:SimpleNamespace(st_mode=stat.S_IFREG,st_rdev=0)}) as (process,listing,opening,closing):
            with self.assertRaisesRegex(ValueError,'no-proxy-regular-log-destination'):
                boundary.inspect_log_destinations(process)
            self.assertEqual(listing.call_count,1);closing.assert_called_once_with(10)

    def test_absent_null_device_cannot_be_substituted_by_a_complete_socket_snapshot(self):
        with self.fixture([self.descriptors()],[10,11],{10:self.socket(),11:self.socket()}) as (process,listing,opening,closing):
            with self.assertRaisesRegex(ValueError,'actual-proxy-null-destination'):
                boundary.inspect_log_destinations(process)
            self.assertEqual(closing.call_count,2)

    def test_descriptor_churn_is_bounded_and_never_accepts_a_partial_snapshot(self):
        with self.fixture([[self.descriptors()[0]]]*3,[FileNotFoundError()]*3,{}) as (process,listing,opening,closing):
            with self.assertRaisesRegex(ValueError,'stable-proxy-fd-snapshot'):
                boundary.inspect_log_destinations(process)
            self.assertEqual(listing.call_count,3);closing.assert_not_called()

    def test_permission_failure_propagates_and_closes_earlier_pins(self):
        with self.fixture([self.descriptors()],[10,PermissionError()],{10:self.null()}) as (process,listing,opening,closing):
            with self.assertRaises(PermissionError):boundary.inspect_log_destinations(process)
            self.assertEqual(listing.call_count,1);closing.assert_called_once_with(10)

    def test_proxy_exit_before_or_during_inspection_refuses(self):
        for polls in ([1],[None,1]):
            with self.subTest(polls=polls),self.fixture([[self.descriptors()[0]]],[10],{10:self.null()},polls=polls) as (process,listing,opening,closing):
                with self.assertRaisesRegex(ValueError,'live-owned-proxy'):boundary.inspect_log_destinations(process)
                self.assertEqual(closing.call_count,len(polls)-1)

    def test_oversized_descriptor_inventory_refuses_before_opening(self):
        with self.fixture([[Path('/proc/123/fd/'+str(i)) for i in range(257)]],[],{}) as (process,listing,opening,closing):
            with self.assertRaisesRegex(ValueError,'bounded-proxy-fd-snapshot'):boundary.inspect_log_destinations(process)
            opening.assert_not_called();closing.assert_not_called()


if __name__=='__main__':unittest.main()
