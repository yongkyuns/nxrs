"""Package admission, archive integrity, scope and personal-host refusal controls."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import pcre_package as package

class PackageTests(unittest.TestCase):
    def test_standalone_command_initializes_source_work_without_hidden_import_setup(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,GITHUB_ACTIONS='true',RUNNER_ENVIRONMENT='github-hosted'):
            work=package.pcre_source_work(Path(directory))
            self.assertEqual(work.work,Path(directory));self.assertEqual(work.env['RUNNER_ENVIRONMENT'],'github-hosted')
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_bootstrap_shell_keeps_output_private_and_preserves_failure_status(self):
        with tempfile.TemporaryDirectory() as directory:
            events=Path(directory)/'protocol with spaces';native=Path(directory)/'native with spaces'
            command=['/bin/sh','-c',package.BOOTSTRAP_SHELL,'owned-fixture',str(events),str(native),
                     sys.executable,'-c','import sys;print("private synthetic fixture");sys.exit(3)']
            result=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
            self.assertEqual(result.returncode,3);self.assertEqual(result.stdout+result.stderr,b'')
            self.assertEqual(events.read_bytes(),b'X: 3\n');self.assertEqual(native.read_bytes(),b'private synthetic fixture\n')

    def test_large_diagnostics_are_read_from_the_bounded_tail_and_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private-log';path.write_bytes(b'private prefix'*(100000)+b'dpkg: error: owned fixture')
            raw=package.diagnostic_tail(path,64)
            self.assertEqual(len(raw),64);self.assertTrue(raw.endswith(b'dpkg: error: owned fixture'))
            self.assertEqual(package.bootstrap_categories(raw),['package-configuration'])
            link=Path(directory)/'alias';link.symlink_to(path)
            self.assertEqual(package.diagnostic_tail(link),b'')
            self.assertEqual(package.diagnostic_tail(Path(directory)),b'')
            with self.assertRaises(package.u.Refusal):package.diagnostic_tail(path,-1)

    def test_bootstrap_package_state_excludes_unknown_names_fields_and_installed_packages(self):
        raw=b'Package: libc-bin\nStatus: install ok half-configured\nDescription: private-data\n\nPackage: secret-package\nStatus: install ok unpacked\n\nPackage: dpkg\nStatus: install ok installed\n'
        self.assertEqual(package.bootstrap_package_states(raw),['libc-bin:half-configured'])
        self.assertEqual(package.bootstrap_package_states(b'private-path'),['UNCLASSIFIED'])

    def test_bootstrap_context_uses_only_reviewed_stages_and_command_names(self):
        raw=b'P: 1 7 INSTCORE\nWA: chroot /private/seed-key dpkg-deb -f private-data\nP: 3 7 PRIVATE_STAGE\n'
        self.assertEqual(package.bootstrap_context(raw),dict(phase='INSTCORE',commands=['dpkg-deb']))
        self.assertEqual(package.bootstrap_context(b'WA: private unknown data'),dict(phase='UNCLASSIFIED',commands=[]))
        self.assertNotIn('merged-usr',package.bootstrap_categories(b'I: retrieving usr-is-merged package'))

    def test_bootstrap_machine_protocol_never_publishes_argument_or_format_records(self):
        raw=b'E: USRMERGEFAIL\nEA: /private/seed-key\nEF: cannot move private-data\nW: CONF_BASE_FAIL_FIVE\nE: PRIVATE_UNKNOWN\n'
        self.assertEqual(package.bootstrap_codes(raw),['CONF_BASE_FAIL_FIVE','USRMERGEFAIL'])
        self.assertEqual(package.bootstrap_codes(b'EA: PRIVATE_KEY\nEF: unsafe\n'),['UNCLASSIFIED'])
        with self.assertRaises(package.u.Refusal):package.bootstrap_codes(b'x'*(1024*1024+1))

    def test_bootstrap_diagnostics_emit_only_constant_classifications(self):
        raw=b'dpkg: error processing package /private/seed-key (blocked)\nsecret=never-publish\nerror while loading shared libraries'
        self.assertEqual(package.bootstrap_categories(raw),['loader','package-configuration'])
        self.assertEqual(package.bootstrap_categories(b'private arbitrary data'),['unclassified'])
        with self.assertRaises(package.u.Refusal):package.bootstrap_categories(b'x'*(1024*1024+1))

    def test_native_failure_reports_only_the_fixed_phase_without_private_output(self):
        class Work:
            def execute(self,*args,**options):raise ValueError('private native output must stay local')
        work=package.phase_commands(Work())
        with self.assertRaises(package.u.Refusal) as error:work.execute(['/private/program'],'first-package')
        self.assertEqual(str(error.exception),'package-phase-first-package')
        with self.assertRaisesRegex(package.u.Refusal,'classified-package-phase'):
            work.execute(['/private/program'],'/private/unclassified-path')

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

    def test_bootstrap_cannot_fall_back_to_unverified_https(self):
        self.assertEqual(package.bootstrap_signature(b'--force-check-gpg --keyring=K'),'--force-check-gpg')
        for raw in [b'',b'--no-check-gpg --keyring=K',b'--force-check-gpg',b'--keyring=K --force-check-sig']:
            with self.assertRaises(package.u.Refusal):package.bootstrap_signature(raw)

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
