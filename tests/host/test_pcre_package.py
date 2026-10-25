"""Package admission, archive integrity, scope and personal-host refusal controls."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import pcre_package as package

class PackageTests(unittest.TestCase):
    def test_personal_host_refuses_before_writes_commands_or_source_reads(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True),\
                patch.object(package.u,'command') as command,patch.object(package.e,'verify') as verify:
            work=Path(directory)/'new'
            with self.assertRaisesRegex(package.u.Refusal,'hosted-ubuntu-required'):
                package.run(work,Path(directory)/'absent-source')
            self.assertFalse(work.exists());command.assert_not_called();verify.assert_not_called()

    def test_hosted_paths_cannot_escape_runner_temp_or_reuse_work(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(package.u,'hosted'),\
                patch.object(package.os,'getuid',return_value=1000),\
                patch.object(package.os,'uname',return_value=type('U',(),{'machine':'x86_64'})()),\
                patch.dict(os.environ,RUNNER_TEMP=directory):
            root=Path(directory).resolve();source=root/'source';source.mkdir();work=root/'work'
            package.hosted(work,source)
            for bad in [Path('/'),root.parent/'outside',root/'nested/work',root/'source/../new']:
                with self.subTest(path=bad),self.assertRaises(package.u.Refusal):package.hosted(bad,source)
            link=root/'linked';link.symlink_to(source,target_is_directory=True)
            with self.assertRaises(package.u.Refusal):package.hosted(work,link)
            work.mkdir()
            with self.assertRaises(package.u.Refusal):package.hosted(work,source)

    def test_dependency_comes_from_only_public_libc_version_requirements(self):
        raw=b'(NEEDED) Shared library: [libc.so.6]\nName: GLIBC_2.9\nName: GLIBC_2.34\nName: GLIBC_2.3.4\n'
        self.assertEqual(package.libc_dependency(raw),'libc6 (>= 2.34)')
        for invalid in [b'',raw+b'(NEEDED) Shared library: [libm.so.6]\n',raw+b'GLIBC_PRIVATE',raw.replace(b'GLIBC_2.34',b'GLIBC_3.0')]:
            with self.subTest(value=invalid),self.assertRaises(package.u.Refusal):package.libc_dependency(invalid)

    def test_changed_image_fixture_never_changes_a_loaded_segment(self):
        data=bytearray(256);data[:7]=b'\x7fELF\x02\x01\x01'
        struct.pack_into('<Q',data,32,64);struct.pack_into('<HH',data,54,56,1)
        struct.pack_into('<IIQQQQQQ',data,64,1,5,128,128,128,64,64,4096)
        changed=package.changed_fixture(bytes(data))
        self.assertEqual(changed[:-1],bytes(data[:-1]));self.assertEqual(changed[-1],1)
        invalid=bytearray(data);struct.pack_into('<Q',invalid,64+32,128)
        for raw in [b'',b'not an ELF',bytes(invalid),bytes(data[:-1])+b'x']:
            with self.assertRaises(package.u.Refusal):package.changed_fixture(raw)

    def test_only_configured_exact_candidate_version_and_architecture_are_admitted(self):
        package.installed_status(b'install ok installed\t10.49-0nxrs1\tamd64')
        for state in ['install ok unpacked','install ok half-configured','install reinstreq installed','deinstall ok config-files']:
            with self.subTest(state=state),self.assertRaises(package.u.Refusal):
                package.installed_status((state+'\t10.49-0nxrs1\tamd64').encode())
        for row in [b'install ok installed\t10.42-4ubuntu2.1\tamd64',b'install ok installed\t10.49-0nxrs1\tarm64']:
            with self.assertRaises(package.u.Refusal):package.installed_status(row)

    def stage(self,root):
        library=root/'libpcre2-8.so.0.16.1';library.write_bytes(b'owned synthetic ELF fixture')
        stage=root/'stage'
        files=package.stage(stage,library,{'public_fixture':True},b'BSD Copyright fixture','libc6 (>= 2.34)')
        return library,stage,files

    def archive(self,stage,mutation=None):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w') as archive:
            for path in sorted(stage.rglob('*')):
                if 'DEBIAN' in path.relative_to(stage).parts:continue
                name='./'+path.relative_to(stage).as_posix()
                member=archive.gettarinfo(str(path),arcname=name);member.uid=member.gid=0
                if member.issym():member.mode=0o777
                if mutation:mutation(member)
                archive.addfile(member,io.BytesIO(path.read_bytes()) if member.isreg() else None)
        return output.getvalue()

    def test_package_owns_runtime_soname_license_source_and_every_regular_file_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            library,stage,files=self.stage(Path(directory))
            package.inventory(self.archive(stage),files,library)
            self.assertEqual((stage/package.LIBDIR/'libpcre2-8.so.0').readlink(),Path(library.name))
            self.assertFalse((stage/package.LIBDIR/'libpcre2-8.so').exists())
            self.assertEqual((stage/'DEBIAN/triggers').read_text(),'activate-noawait ldconfig\n')
            sums={row.split('  ')[1]:row.split('  ')[0] for row in (stage/'DEBIAN/md5sums').read_text().splitlines()}
            self.assertEqual(sums,{str(path):hashlib.md5(raw).hexdigest() for path,raw in files.items()})
            for mode in [0o666,0o4755]:
                def alter(member):
                    if member.isreg():member.mode=mode
                with self.assertRaises(package.u.Refusal):package.inventory(self.archive(stage,alter),files,library)
            def foreign(member):member.uid=1000
            with self.assertRaises(package.u.Refusal):package.inventory(self.archive(stage,foreign),files,library)
            def redirect(member):
                if member.issym():member.linkname='../../outside'
            with self.assertRaises(package.u.Refusal):package.inventory(self.archive(stage,redirect),files,library)
            (stage/'unexpected').write_text('not in reviewed payload')
            with self.assertRaises(package.u.Refusal):package.inventory(self.archive(stage),files,library)

    def test_license_requires_exact_signed_archive_bytes_and_regular_bounded_member(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.tar'
            def write(name,text=b'BSD Copyright fixture',link=False):
                with tarfile.open(path,'w') as archive:
                    member=tarfile.TarInfo(name);member.size=len(text)
                    if link:member.type=tarfile.SYMTYPE;member.linkname='/outside'
                    archive.addfile(member,io.BytesIO(text) if not link else None)
                return dict(root='pcre2-10.49',sha256=package.u.file_digest(path))
            pin=write('pcre2-10.49/LICENCE.md');self.assertEqual(package.license_bytes(path,pin),b'BSD Copyright fixture')
            changed=dict(pin,sha256='0'*64)
            with self.assertRaises(package.u.Refusal):package.license_bytes(path,changed)
            for name,text,link in [('pcre2-10.49/LICENCE',b'BSD Copyright fixture',False),
                ('pcre2-10.49/LICENCE.md',b'no license',False),('pcre2-10.49/LICENCE.md',b'',True)]:
                pin=write(name,text,link)
                with self.assertRaises(package.u.Refusal):package.license_bytes(path,pin)

    def test_public_receipt_cannot_claim_boot_power_loss_rollback_release_or_private_fields(self):
        value=dict(schema=1,scope=package.SCOPE,checkout=dict(commit='a'*40,clean=True),inputs=package.inputs(),
            package=dict(name=package.PACKAGE,version=package.VERSION,architecture='amd64',sha256='b'*64,dependency='libc6 (>= 2.34)'),
            source_qualification_sha256='c'*64,library_sha256='d'*64,
            controls=dict.fromkeys(package.CONTROLS,True),**dict.fromkeys(package.FLAGS,False))
        package.validate(value)
        for name,item in [('schema',True),('unclassified_private_path','/private/example'),('inputs',{}),('controls',{})]:
            changed=copy.deepcopy(value);changed[name]=item
            with self.subTest(name=name),self.assertRaises(package.u.Refusal):package.validate(changed)
        for name in package.FLAGS:
            changed=copy.deepcopy(value);changed[name]=True
            with self.subTest(flag=name),self.assertRaises(package.u.Refusal):package.validate(changed)
        for name in package.CONTROLS:
            changed=copy.deepcopy(value);changed['controls'][name]=1
            with self.subTest(control=name),self.assertRaises(package.u.Refusal):package.validate(changed)

if __name__=='__main__':unittest.main()
