"""Offline payload-chain refusal controls; live GPG/dpkg evidence belongs to hosted CI."""
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import payloads as p


class NativePayloadTests(unittest.TestCase):
    def setUp(self):
        self.trust=json.loads((ROOT/p.POLICY).read_bytes())
        self.now=datetime(2026,10,25,12,tzinfo=timezone.utc)
        self.data=b'owned synthetic ELF fixture\x00\xff'
        self.want=dict(uid=0,gid=0,mode=0o644,size=len(self.data),sha256=hashlib.sha256(self.data).hexdigest())

    def test_personal_host_refuses_before_reading_sources_tools_or_network(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{},clear=True),patch.object(p.candidate,'read') as read,patch.object(p.u,'command') as command,patch.object(p,'Evidence') as evidence:
            root=Path(d)
            with self.assertRaisesRegex(p.u.Refusal,'hosted-ubuntu-required'):p.run(root/'absent',root/'work',root/'report')
            read.assert_not_called();command.assert_not_called();evidence.assert_not_called();self.assertEqual(list(root.iterdir()),[])

    def test_failed_payload_check_retains_failure_and_never_overwrites_advisory_status(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);receipt=root/'candidate';report=root/'report';work=root/'work';head='a'*40;inputs={'owned':'b'*64}
            keyring=root/'fixture-keyring';keyring.write_bytes(b'unit fixture only');original_policy=p.policy
            parent=dict(schema=1,scope=p.candidate.SCOPE,advisory_status='complete',qualified=False,
                        checkout=dict(clean=True,commit=head),inputs=inputs,inventory=dict(packages={}),findings=[{'advisory':'fixture'}])
            receipt.write_text(json.dumps(parent))
            def command(args):return head if 'rev-parse' in args else ''
            with patch.object(p,'KEYRING',keyring),patch.object(p,'policy',side_effect=lambda trust,now:original_policy(trust,self.now)),patch.object(p.u,'hosted'),patch.object(p.u,'command',side_effect=command),patch.object(p.candidate,'source_inputs',return_value=inputs),patch.object(p,'protected'),patch.object(p,'Evidence'),patch.object(p,'authenticate',side_effect=p.u.Refusal('native-payload-bytes')):
                with self.assertRaisesRegex(p.u.Refusal,'native-payload-bytes'):p.run(receipt,work,report)
            result=json.loads(report.read_bytes())
            self.assertFalse(result['qualified']);self.assertEqual(result['verification_status'],'failed')
            self.assertEqual(result['failure_category'],'native-payload-bytes');self.assertEqual(result['advisory_findings'],1)
            for name in ['distribution_payloads_authenticated','package_payload_authenticated','advisory_gate_qualified','deployment_permitted']:
                self.assertIs(result[name],False)

    def test_pending_candidate_cannot_start_a_payload_assessment(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);receipt=root/'candidate';receipt.write_text(json.dumps(dict(schema=1,scope=p.candidate.SCOPE,advisory_status='pending')))
            with patch.object(p.u,'hosted'),patch.object(p.u,'command',return_value='a'*40),patch.object(p.candidate,'source_inputs',return_value={}),patch.object(p,'Evidence') as evidence:
                with self.assertRaisesRegex(p.u.Refusal,'complete-current-candidate'):p.run(receipt,root/'work',root/'report')
                evidence.assert_not_called();self.assertFalse((root/'work').exists());self.assertFalse((root/'report').exists())

    def test_policy_pins_authority_and_expires_without_a_fallback(self):
        p.policy(self.trust,self.now)
        for key,value in [('schema',True),('signer','A'*40),('archive','https://example.invalid/'),('suites',['noble']),
                          ('rolling_metadata_max_age_seconds',True),('future_tolerance_seconds',301),
                          ('valid_until','2026-10-25T00:00:00+00:00'),('valid_until','2027-01-01T00:00:00+00:00')]:
            with self.subTest(key=key,value=value),self.assertRaises(p.u.Refusal):p.policy(dict(self.trust,**{key:value}),self.now)

    def status(self,signer=None,algorithm='1',digest='10',subkey=None):
        signer=signer or self.trust['signer']
        return ('[GNUPG:] VALIDSIG '+(subkey or signer)+' 2026-10-25 1792929600 0 4 0 '+algorithm+' '+digest+' 01'+(' '+signer if subkey else '')+'\n').encode()

    def test_signature_requires_exact_primary_key_and_modern_digest(self):
        self.assertEqual(p.signature(self.status(),self.trust['signer']),self.trust['signer'])
        self.assertEqual(p.signature(self.status(subkey='B'*40),self.trust['signer']),self.trust['signer'])
        for raw in [self.status(signer='A'*40),self.status(algorithm='17'),self.status(digest='2'),b'',
                    self.status()*2,self.status()+b'[GNUPG:] BADSIG 1 fixture\n',self.status()+b'[GNUPG:] REVKEYSIG 1 fixture\n']:
            with self.subTest(raw=raw),self.assertRaises(p.u.Refusal):p.signature(raw,self.trust['signer'])

    def release(self,suite='noble-updates',when=None,extra=''):
        rows=''.join(' '+'a'*64+' 123 '+c+'/binary-amd64/Packages.gz\n' for c in self.trust['components'])
        return ('Origin: Ubuntu\nLabel: Ubuntu\nSuite: '+suite+'\nCodename: noble\nVersion: 24.04\nArchitectures: amd64 arm64\nDate: '+format_datetime(when or self.now)+'\n'+extra+'SHA256:\n'+rows).encode()

    def test_signed_release_identity_freshness_and_complete_indexes(self):
        raw=self.release();indexes,_=p.release(raw,'noble-updates',self.trust,self.now);self.assertEqual(len(indexes),4)
        changes=[raw.replace(b'Origin: Ubuntu',b'Origin: Other'),raw.replace(b'Suite: noble-updates',b'Suite: noble-security'),
                 raw.replace(b'Codename: noble',b'Codename: jammy'),raw.replace(b'Version: 24.04',b'Version: 22.04'),
                 raw.replace(b'Architectures: amd64 arm64',b'Architectures: arm64'),raw+b'Origin: Ubuntu\n',
                 raw.replace(b'main/binary-amd64/Packages.gz',b'../Packages.gz'),raw.replace(b'main/binary-amd64/Packages.gz',b'main/Packages.gz'),
                 self.release(when=datetime(2026,10,1,tzinfo=timezone.utc)),self.release(when=datetime(2026,10,26,tzinfo=timezone.utc)),
                 self.release(extra='Valid-Until: Fri, 23 Oct 2026 12:00:00 +0000\n')]
        for value in changes:
            with self.subTest(raw=value),self.assertRaises(p.u.Refusal):p.release(value,'noble-updates',self.trust,self.now)
        # The immutable base suite is not incorrectly subjected to a seven-day update age.
        p.release(self.release(suite='noble',when=datetime(2024,4,25,tzinfo=timezone.utc)),'noble',self.trust,self.now)

    def row(self):
        return dict(Package=b'libowned1',Version=b'1:2.0-1ubuntu1',Architecture=b'amd64',Source=b'owned (1:2.0-1ubuntu1)',
                    SHA256=b'a'*64,Size=b'123',Filename=b'pool/main/o/owned/libowned1_2.0-1ubuntu1_amd64.deb')

    def selected(self):
        return dict(name='libowned1',version='1:2.0-1ubuntu1',architecture='amd64',source='owned',source_version='1:2.0-1ubuntu1')

    def test_exact_package_binary_source_epochs_and_canonical_archive(self):
        good=self.row();self.assertEqual(p.package_record(good,self.selected())['version'],'1:2.0-1ubuntu1')
        for key,value in [('Package',b'libother'),('Version',b'2.0-1ubuntu1'),('Architecture',b'arm64'),
                          ('Source',b'owned (1:2.0-1ubuntu2)'),('SHA256',b'x'*64),('Size',b'0'),
                          ('Filename',b'/pool/package.deb'),('Filename',b'pool/main/../package.deb'),
                          ('Filename',b'pool//package.deb'),('Filename',b'pool/package.deb?next=other')]:
            with self.subTest(key=key,value=value),self.assertRaises(p.u.Refusal):p.package_record(dict(good,**{key:value}),self.selected())

    def test_deb822_duplicate_continued_or_oversized_identities_refuse(self):
        self.assertEqual(list(p.paragraphs(io.BytesIO(b'Package: owned\nDescription: fixture\n harmless continuation\n\n'))),[dict(Package=b'owned',Description=b'fixture')])
        for raw in [b'Package: owned\npackage: other\n',b'Version: 1.0\n 2.0\n',b' continuation without field\n',b'Package: '+b'x'*32768+b'\n']:
            with self.subTest(raw=raw[:50]),self.assertRaises(p.u.Refusal):list(p.paragraphs(io.BytesIO(raw)))
        with self.assertRaises(p.u.Refusal):list(p.paragraphs(io.BytesIO(b'Package: owned\n'),maximum=8))

    def tar(self,entries):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w') as archive:
            for name,changes,data in entries:
                info=tarfile.TarInfo(name);info.mode=0o644;info.uid=info.gid=0;info.size=len(data)
                for key,value in changes.items():setattr(info,key,value)
                archive.addfile(info,io.BytesIO(data) if info.isreg() else None)
        output.seek(0);return output

    def test_real_tar_bytes_and_qualified_usrmerge_member_mapping(self):
        entries=[('./',dict(type=tarfile.DIRTYPE),b''),('./usr/',dict(type=tarfile.DIRTYPE),b''),
                 ('./lib/owned.so',{},self.data),('./usr/lib/unselected',{},b'other')]
        found=p.payload_members(self.tar(entries),{'/usr/lib/owned.so':self.want},{'lib':'usr/lib'})
        self.assertEqual(found['/usr/lib/owned.so']['sha256'],self.want['sha256'])
        with self.assertRaisesRegex(p.u.Refusal,'all-mapped'):p.payload_members(self.tar(entries),{'/usr/lib/owned.so':self.want},{})

    def test_selected_file_bytes_mode_owner_size_missing_and_alias_duplicates_refuse(self):
        name='./usr/lib/owned.so';good=(name,{},self.data)
        variants=[[(name,{},b'changed')],[(name,dict(mode=0o666),self.data)],[(name,dict(uid=1000),self.data)],
                  [(name,dict(gid=1000),self.data)],[],[good,good],[good,('./lib/owned.so',{},self.data)],
                  [(name,dict(type=tarfile.SYMTYPE,linkname='other'),b'')],[(name,dict(type=tarfile.LNKTYPE,linkname='other'),b'')]]
        for entries in variants:
            with self.subTest(entries=entries),self.assertRaises(p.u.Refusal):p.payload_members(self.tar(entries),{'/usr/lib/owned.so':self.want},{'lib':'usr/lib'})
        with self.assertRaisesRegex(p.u.Refusal,'native-payload-bytes'):p.payload_members(self.tar([good]),{'/usr/lib/owned.so':dict(self.want,sha256='b'*64)},{})
        with patch.object(p,'MAX_TAR',8),self.assertRaisesRegex(p.u.Refusal,'payload-member-budget'):p.payload_members(self.tar([good]),{'/usr/lib/owned.so':self.want},{})

    def test_truncated_selected_payload_fails_in_the_bounded_seekable_file(self):
        member=tarfile.TarInfo('./usr/lib/owned.so');member.mode=0o644;member.size=8192
        archive=io.BytesIO(member.tobuf()+b'truncated')
        expected=dict(self.want,size=8192)
        with self.assertRaises(tarfile.ReadError):p.payload_members(archive,{'/usr/lib/owned.so':expected},{})

    def test_tar_traversal_absolute_and_ambiguous_paths_are_never_followed(self):
        for name in ['/usr/lib/owned.so','../owned.so','./usr/../owned.so','usr//lib/owned.so','usr/./lib/owned.so','usr\\lib\\owned.so']:
            with self.subTest(name=name),self.assertRaisesRegex(p.u.Refusal,'canonical-payload-member'):p.payload_members(self.tar([(name,{},self.data)]),{}, {})

    def test_archive_admission_and_real_changed_byte_control_share_the_same_predicate(self):
        with tempfile.TemporaryDirectory() as d:
            archive=Path(d)/'owned.deb';archive.write_bytes(self.data);expected=dict(size=len(self.data),sha256=hashlib.sha256(self.data).hexdigest())
            p.archive_identity(expected['size'],expected['sha256'],expected);p.changed_archive_control(archive,expected)
            for size,sha in [(expected['size']+1,expected['sha256']),(expected['size'],'b'*64)]:
                with self.assertRaisesRegex(p.u.Refusal,'archive-digest'):p.archive_identity(size,sha,expected)
            # Disabling the refusal must make its live control fail, not manufacture a pass.
            with patch.object(p,'archive_identity'),self.assertRaisesRegex(p.u.Refusal,'actual-changed-archive-control'):p.changed_archive_control(archive,expected)

    def test_access_time_changes_are_allowed_but_content_identity_changes_are_not(self):
        names=('st_dev','st_ino','st_mode','st_uid','st_gid','st_size','st_mtime_ns','st_ctime_ns')
        before=dict.fromkeys(names,1);before['st_atime_ns']=1
        after=dict(before,st_atime_ns=2)
        self.assertEqual(p.stable_stat(SimpleNamespace(**before)),p.stable_stat(SimpleNamespace(**after)))
        for name in names:
            self.assertNotEqual(p.stable_stat(SimpleNamespace(**before)),p.stable_stat(SimpleNamespace(**dict(after,**{name:2}))))

    def test_download_checks_signed_size_hash_and_never_follows_a_redirect(self):
        class Response(io.BytesIO):
            status=200
            def geturl(self):return 'https://archive.ubuntu.com/ubuntu/fixture'
        with tempfile.TemporaryDirectory() as d:
            evidence=object.__new__(p.Evidence);evidence.total=0;evidence.deadline=p.time.monotonic()+30
            evidence.opener=SimpleNamespace(open=lambda *a,**k:Response(self.data))
            root=Path(d);url='https://archive.ubuntu.com/ubuntu/fixture'
            expected=dict(size=len(self.data),sha256=hashlib.sha256(self.data).hexdigest())
            self.assertEqual(evidence.get(url,root/'good',1000,expected),expected)
            for name,record in [('size',dict(expected,size=1)),('sha',dict(expected,sha256='a'*64))]:
                with self.assertRaisesRegex(p.u.Refusal,'archive-digest'):evidence.get(url,root/name,1000,record)
            with self.assertRaisesRegex(p.u.Refusal,'archive-download-budget'):evidence.get(url,root/'limit',1)
            with self.assertRaisesRegex(p.u.Refusal,'exact-archive-response'):evidence.get(url+'-redirect',root/'redirect',1000)
        self.assertIsNone(p.NoRedirect().redirect_request(None,None,None,None,None,None))


if __name__=='__main__':unittest.main()
