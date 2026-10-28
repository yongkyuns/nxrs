"""Distribution-native audit controls; metadata-only, no personal-host scan."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('ubuntu_native',ROOT/'tools/native-packages/ubuntu.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


def elf_image(payload=b'',kind=3):
    # Synthetic header, never executed. No CPU/loader qualification is implied.
    header=bytearray(64);header[:7]=b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI',header,16,kind,62,1)
    struct.pack_into('<H',header,52,64)
    return bytes(header)+payload


class UbuntuNativeTests(unittest.TestCase):
    def record(self,identity='UBUNTU-CVE-2025-59734',events=None):
        return dict(id=identity,modified='2026-10-13T02:51:31.488925277Z',affected=[dict(package=dict(ecosystem=audit.ECOSYSTEM,name='ffmpeg'),ranges=[dict(type='ECOSYSTEM',events=events or [dict(introduced='0')])])])

    def test_canonical_record_digest_ignores_object_order_but_preserves_typed_content(self):
        value={'z':['unicode \u00e9',False,0,1.0],'a':{'y':2,'x':1}}
        reordered={'a':{'x':1,'y':2},'z':['unicode \u00e9',False,0,1.0]}
        self.assertEqual(audit.canonical_record_digest(value),audit.canonical_record_digest(reordered))
        for changed in [dict(value,z=list(reversed(value['z']))),dict(value,z=['unicode \u00e9',0,0,1.0]),
                        dict(value,z=['unicode \u00e9',False,0,1]),dict(value,a={'x':1,'y':3})]:
            self.assertNotEqual(audit.canonical_record_digest(value),audit.canonical_record_digest(changed))
        for invalid in [float('nan'),float('inf'),float('-inf'),object()]:
            with self.assertRaisesRegex(audit.Refusal,'canonical-advisory-json'):audit.canonical_record_digest(invalid)

    def test_image_hash_size_and_header_share_one_stream_even_with_short_reads(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'image';raw=elf_image(b'public synthetic payload'*100,kind=2);path.write_bytes(raw)
            read=os.read;opened=os.open;descriptors=[]
            def opening(*args):
                fd=opened(*args);descriptors.append(fd);return fd
            with mock.patch.object(audit.os,'read',side_effect=lambda fd,n:read(fd,min(n,7))),\
                 mock.patch.object(audit.os,'open',side_effect=opening) as opens:
                value=audit.image_identity(path)
            opens.assert_called_once()
            self.assertEqual(value,dict(sha256=audit.digest(raw),size=len(raw),elf=dict(bits=64,
                byte_order='little',machine=62,type=2,osabi=0,abi_version=0,flags=0)))
            with self.assertRaises(OSError):os.fstat(descriptors[0])

    def test_nonqualified_elf_headers_refuse_including_powerpc_and_arm(self):
        original=elf_image()
        changes=[(0,b'NOPE'),(4,b'\x00'),(4,b'\x01'),(5,b'\x02'),(6,b'\x00'),
                 (7,b'\x09'),(8,b'\x01'),(16,b'\x01\x00'),(16,b'\x04\x00'),
                 (18,b'\x15\x00'),(18,b'\xb7\x00'),(18,b'\x00\x00'),
                 (20,b'\x00\x00\x00\x00'),(52,b'\x34\x00')]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'image'
            for offset,replacement in changes:
                raw=bytearray(original);raw[offset:offset+len(replacement)]=replacement;path.write_bytes(raw)
                with self.subTest(offset=offset,replacement=replacement),self.assertRaises(audit.Refusal):audit.image_identity(path)
            for size in (0,4,16,63):
                path.write_bytes(original[:size])
                with self.subTest(size=size),self.assertRaises(audit.Refusal):audit.image_identity(path)
            raw=bytearray(original);raw[7]=3;path.write_bytes(raw)
            self.assertEqual(audit.image_identity(path)['elf']['osabi'],3)

    def test_image_refuses_symlinks_directories_fifos_and_oversized_files_before_reading(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'image';path.write_bytes(elf_image())
            link=root/'link';link.symlink_to(path)
            fifo=root/'fifo';os.mkfifo(fifo)
            for selected in (link,root,fifo):
                with self.subTest(selected=selected.name),mock.patch.object(audit.os,'read') as read,\
                     self.assertRaises((OSError,audit.Refusal)):
                    try:audit.image_identity(selected)
                    finally:read.assert_not_called()
            with mock.patch.object(audit,'MAX_IMAGE',63),mock.patch.object(audit.os,'read') as read,\
                 self.assertRaisesRegex(audit.Refusal,'native-image-size-budget'):
                try:audit.image_identity(path)
                finally:read.assert_not_called()

    def test_image_replacement_rewrite_growth_and_truncation_during_read_refuse_and_close_fd(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'image';original=elf_image(b'public synthetic image');read=os.read;opened=os.open
            for operation in ('replace','rewrite','grow','truncate'):
                path.write_bytes(original);before=path.stat();descriptors=[];changed=False
                def opening(*args):
                    fd=opened(*args);descriptors.append(fd);return fd
                def reading(fd,n):
                    nonlocal changed
                    chunk=read(fd,min(n,16))
                    if not changed:
                        changed=True
                        if operation=='replace':
                            other=Path(d)/'replacement';other.write_bytes(original);other.replace(path)
                        elif operation=='rewrite':
                            path.write_bytes(elf_image(b'public synthetic IMAGE'))
                            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
                        elif operation=='grow':
                            with path.open('ab') as target:target.write(b'extra')
                        else:path.write_bytes(original[:64])
                    return chunk
                with self.subTest(operation=operation),mock.patch.object(audit.os,'open',side_effect=opening),\
                     mock.patch.object(audit.os,'read',side_effect=reading),self.assertRaises(audit.Refusal):audit.image_identity(path)
                self.assertEqual(len(descriptors),1)
                with self.assertRaises(OSError):os.fstat(descriptors[0])

    def test_wrong_root_machine_refuses_before_loader_and_package_queries(self):
        with tempfile.TemporaryDirectory() as d:
            roots={name:Path(d)/name for name in [*audit.SYSTEM_ROOTS,*audit.REPOSITORY_ROOTS]}
            for path in roots.values():path.write_bytes(elf_image())
            wrong=bytearray(elf_image());struct.pack_into('<H',wrong,18,21);roots['ingest'].write_bytes(wrong)
            with mock.patch.object(audit,'dependencies') as loader,mock.patch.object(audit,'owner') as owner,\
                 self.assertRaisesRegex(audit.Refusal,'qualified-amd64-elf-header'):
                try:audit.inventory(roots)
                finally:loader.assert_not_called();owner.assert_not_called()

    def test_personal_host_refuses_before_commands_requests_or_output(self):
        with tempfile.TemporaryDirectory() as d,mock.patch.dict(os.environ,{},clear=True),mock.patch.object(audit,'execute') as execute,mock.patch.object(audit,'fetch') as fetch:
            path=Path(d)/'report'
            with self.assertRaises(audit.Refusal):audit.run(path)
            execute.assert_not_called();fetch.assert_not_called();self.assertFalse(path.exists())

    def test_actual_ingest_and_backup_are_required_before_any_loader_or_package_query(self):
        self.assertEqual(audit.REPOSITORY_ROOTS['ingest'],'nxrs-camera-ingest')
        self.assertEqual(audit.REPOSITORY_ROOTS['backup'],'nxrs-activity-backup')
        roots={name:Path('/owned')/name for name in [*audit.SYSTEM_ROOTS,*audit.REPOSITORY_ROOTS]}
        for name in ('ingest','backup'):
            missing=dict(roots);missing.pop(name)
            with self.subTest(name=name),mock.patch.object(audit,'dependencies') as loader,\
                 mock.patch.object(audit,'owner') as owner,\
                 self.assertRaisesRegex(audit.Refusal,'complete-native-root-set'):
                try:audit.inventory(missing)
                finally:loader.assert_not_called();owner.assert_not_called()
            with self.subTest(missing_file=name),mock.patch.object(Path,'is_file',lambda p:p!=roots[name]),\
                 mock.patch.object(audit,'dependencies') as loader,\
                 self.assertRaisesRegex(audit.Refusal,'complete-native-root-set'):
                try:audit.inventory(roots)
                finally:loader.assert_not_called()

    def test_debian_range_endpoints_and_unfixed_ranges(self):
        order={'0':0,'7:6.1.1-3ubuntu5':1,'7:6.1.1-3ubuntu5.1':2,'8:1.0':3}
        def compare(a,op,b):return {'lt':order[a]<order[b],'le':order[a]<=order[b],'ge':order[a]>=order[b]}[op]
        fixed=self.record(events=[dict(introduced='0'),dict(fixed='7:6.1.1-3ubuntu5.1')])
        self.assertTrue(audit.affected(fixed,'ffmpeg','7:6.1.1-3ubuntu5',compare))
        self.assertFalse(audit.affected(fixed,'ffmpeg','7:6.1.1-3ubuntu5.1',compare))
        self.assertFalse(audit.affected(fixed,'ffmpeg','8:1.0',compare))
        last=self.record(events=[dict(introduced='0'),dict(last_affected='7:6.1.1-3ubuntu5.1')])
        self.assertTrue(audit.affected(last,'ffmpeg','7:6.1.1-3ubuntu5.1',compare))
        self.assertTrue(audit.affected(self.record(),'ffmpeg','8:1.0',compare))

    @unittest.skipUnless(sys.platform=='linux' and Path('/usr/bin/dpkg').exists(),'actual Debian comparator requires Linux dpkg')
    def test_actual_dpkg_comparison_preserves_epochs_and_revision_order(self):
        self.assertTrue(audit.compare('1:1.0','ge','9.0'))
        self.assertTrue(audit.compare('7:6.1.1-3ubuntu5','lt','7:6.1.1-3ubuntu5.1'))
        self.assertTrue(audit.compare('1.0~rc1','lt','1.0'))

    def test_wrong_release_source_withdrawn_and_unknown_ranges_refuse(self):
        import copy
        for kind in ['release','source','withdrawal-invalid','type','open','unknown']:
            value=copy.deepcopy(self.record());item=value['affected'][0]
            if kind=='release':item['package']['ecosystem']='Ubuntu:22.04:LTS'
            if kind=='source':item['package']['name']='other'
            if kind=='withdrawal-invalid':value['withdrawn']='private invalid data'
            if kind=='type':item['ranges'][0]['type']='SEMVER'
            if kind=='open':item['ranges'][0]['events']=[dict(fixed='1.0')]
            if kind=='unknown':item['ranges'][0]['events']=[dict(introduced='0'),dict(unrecognized='1.0')]
            with self.subTest(kind=kind),self.assertRaises(audit.Refusal):audit.affected(value,'ffmpeg','7:6.1.1-3ubuntu5')

    def test_batch_must_be_complete_and_have_no_service_errors(self):
        for value in [dict(results=[]),dict(results=[dict(error='lookup failed')]),dict(results=[dict(vulns=[dict(id='unrecognized')])])]:
            with mock.patch.object(audit,'fetch',return_value=(value,'a'*64)),self.assertRaises(audit.Refusal):audit.query({'ffmpeg':'7:6.1.1-3ubuntu5'})

    def test_pagination_collects_every_finding_and_binds_full_records(self):
        ids=['UBUNTU-CVE-2025-59734','UBUNTU-CVE-2024-32228']
        def fetch(endpoint,value=None):
            if endpoint=='querybatch':return dict(results=[dict(vulns=[dict(id=ids[0])],next_page_token='next')]),'a'*64
            if endpoint=='query':
                self.assertEqual(value['page_token'],'next');return dict(vulns=[dict(id=ids[1])]),'b'*64
            return self.record(endpoint.split('/')[1]),'c'*64
        with mock.patch.object(audit,'fetch',side_effect=fetch):
            findings,receipts,withdrawn=audit.query({'ffmpeg':'7:6.1.1-3ubuntu5'})
        self.assertEqual({v['advisory'] for v in findings},set(ids));self.assertEqual(receipts,['a'*64,'b'*64])
        self.assertTrue(all(v['record_sha256']=='c'*64 for v in findings))
        self.assertTrue(all(v['canonical_record_sha256']==audit.canonical_record_digest(self.record(v['advisory'])) for v in findings))

    def test_duplicate_pages_or_mismatched_records_do_not_become_a_clean_scan(self):
        duplicate=dict(results=[dict(vulns=[dict(id='UBUNTU-CVE-2025-59734'),dict(id='UBUNTU-CVE-2025-59734')])])
        with mock.patch.object(audit,'fetch',return_value=(duplicate,'a'*64)),self.assertRaises(audit.Refusal):audit.query({'ffmpeg':'7:6.1.1-3ubuntu5'})
        def fetch(endpoint,value=None):
            if endpoint=='querybatch':return dict(results=[dict(vulns=[dict(id='UBUNTU-CVE-2025-59734')])]),'a'*64
            return self.record('UBUNTU-CVE-2024-32228'),'b'*64
        with mock.patch.object(audit,'fetch',side_effect=fetch),self.assertRaises(audit.Refusal):audit.query({'ffmpeg':'7:6.1.1-3ubuntu5'})

    def test_official_withdrawals_are_retained_without_becoming_active_findings(self):
        identity='UBUNTU-CVE-2025-59734'
        def fetch(endpoint,value=None):
            if endpoint=='querybatch':return dict(results=[dict(vulns=[dict(id=identity)])]),'a'*64
            return dict(id=identity,modified='2026-10-13T02:51:31Z',withdrawn='2026-10-14T00:00:00Z',affected=[]),'b'*64
        with mock.patch.object(audit,'fetch',side_effect=fetch):
            findings,receipts,withdrawn=audit.query({'ffmpeg':'7:6.1.1-3ubuntu5'})
        self.assertEqual(findings,[]);self.assertEqual(len(withdrawn),1)
        self.assertEqual(withdrawn[0]['advisory'],identity);self.assertEqual(withdrawn[0]['record_sha256'],'b'*64)
        self.assertEqual(withdrawn[0]['canonical_record_sha256'],audit.canonical_record_digest(fetch('vulns/'+identity)[0]))
        value=self.record();value['withdrawn']=None
        self.assertTrue(audit.affected(value,'ffmpeg','7:6.1.1-3ubuntu5'))

    def test_failed_advisory_snapshot_is_never_published_as_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'public.json';value=dict(qualified=False,advisory_status='pending',findings=None)
            identity=audit.save_report(path,value)
            value.update(advisory_status='failed',failure_category='lookup')
            audit.save_report(path,value,identity)
            result=json.loads(path.read_bytes())
            self.assertFalse(result['qualified']);self.assertIsNone(result['findings'])
            self.assertEqual(result['advisory_status'],'failed')
            with self.assertRaises(FileExistsError):audit.save_report(path,value)

    def test_loader_missing_dependency_or_unknown_output_refuses(self):
        for output in ['libbad.so => not found\n','unrecognized native-loader output\n']:
            with mock.patch.object(audit,'command',return_value=output),self.assertRaises(audit.Refusal):audit.dependencies(Path('/owned/binary'))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'public.so';path.write_bytes(b'public native bytes')
            output='linux-vdso.so.1 (0x1234)\nlibpublic.so => '+str(path)+' (0x1234)\n'
            with mock.patch.object(audit,'command',return_value=output):self.assertEqual(audit.dependencies(Path('/owned/binary')),{path.resolve()})

    def test_usrmerge_owner_lookup_accepts_sbin_binary_alias(self):
        canonical=Path('/usr/sbin/cryptsetup')
        def execute(args,**kwargs):
            if args[-1]=='/usr/sbin/cryptsetup':return 1,b''
            self.assertEqual(args[-1],'/sbin/cryptsetup')
            return 0,b'cryptsetup-bin: /sbin/cryptsetup\n'
        with mock.patch.object(audit,'execute',side_effect=execute),mock.patch.object(Path,'resolve',return_value=canonical):
            self.assertEqual(audit.owner(canonical),'cryptsetup-bin')

    def test_source_package_mapping_keeps_epoch_and_binary_revision(self):
        raw='libavcodec60\t7:6.1.1-3ubuntu5\tamd64\tffmpeg\t7:6.1.1-3ubuntu5\tinstalled'
        with mock.patch.object(audit,'command',return_value=raw):value=audit.package('libavcodec60:amd64')
        self.assertEqual(value['source'],'ffmpeg');self.assertEqual(value['source_version'],'7:6.1.1-3ubuntu5')
        self.assertIn('@7%3A6.1.1-3ubuntu5?',value['purl'])
        with self.assertRaises(audit.Refusal):audit.parse(b'{"results":[],"results":[]}')


class NativeRefreshTests(unittest.TestCase):
    def module(self):
        sys.path.insert(0,str(ROOT/'tools/native-packages'))
        spec=importlib.util.spec_from_file_location('native_refresh',ROOT/'tools/native-packages/refresh.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

    def baseline(self):
        roots={k:{} for k in [*audit.SYSTEM_ROOTS,*audit.REPOSITORY_ROOTS]}
        return dict(schema=1,scope='hosted-ubuntu-native-gateway-startup-images-and-system-python-tls',advisory_status='complete',qualified=False,
            checkout=dict(commit='a'*40,clean=True),inputs={'public':'b'*64},inventory=dict(roots=roots,files={'/usr/lib/public.so':dict(binary_package='libpublic1:amd64')},
            packages={'libpublic1:amd64':dict(name='libpublic1',version='1:1.0-1ubuntu1',source_version='1:1.0-1ubuntu1',architecture='amd64')}))

    def test_refresh_requires_exact_current_complete_scope_and_file_owners(self):
        import copy
        module=self.module();good=self.baseline();self.assertEqual(module.select(good,'a'*40,{'public':'b'*64}),['libpublic1'])
        for kind in ['incomplete','dirty','source','owners','name','roots']:
            value=copy.deepcopy(good)
            if kind=='incomplete':value['advisory_status']='failed'
            if kind=='dirty':value['checkout']['clean']=False
            if kind=='source':value['inputs']={}
            if kind=='owners':value['inventory']['files']={}
            if kind=='name':value['inventory']['packages']['libpublic1:amd64']['name']='--unsafe'
            if kind=='roots':value['inventory']['roots'].pop('controller')
            with self.subTest(kind=kind),self.assertRaises(module.ubuntu.Refusal):module.select(value,'a'*40,{'public':'b'*64})

    def test_refresh_personal_host_refuses_before_reading_manifest_or_installing(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as d,mock.patch.dict(os.environ,{},clear=True),mock.patch.object(module.ubuntu,'package') as package:
            with self.assertRaises(module.ubuntu.Refusal):module.run(Path(d)/'absent',Path(d)/'report')
            package.assert_not_called();self.assertFalse((Path(d)/'report').exists())


if __name__=='__main__':unittest.main()
