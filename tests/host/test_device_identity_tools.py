"""Real extraction controls for the identity source release; no SDK execution."""
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('identity_tools', ROOT/'tools/device-identity/qualify.py')
qualifier = importlib.util.module_from_spec(spec); spec.loader.exec_module(qualifier)


class SourceArchiveTests(unittest.TestCase):
    def fixture(self, entries):
        temporary = tempfile.TemporaryDirectory(prefix='nxrs-identity-archive-'); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); archive = root/'input.tar'
        with tarfile.open(archive, 'w') as target:
            for name, kind, value in entries:
                member = tarfile.TarInfo(name)
                if kind == 'file':
                    raw = value.encode(); member.size = len(raw); target.addfile(member, io.BytesIO(raw))
                elif kind == 'link':
                    member.type = tarfile.SYMTYPE; member.linkname = value; target.addfile(member)
                elif kind == 'hardlink':
                    member.type = tarfile.LNKTYPE; member.linkname = value; target.addfile(member)
        output = root/'output'; output.mkdir()
        return archive, output

    def test_actual_internal_link_and_regular_file_are_contained(self):
        archive, output = self.fixture([('mbedtls-4.1.1/source.h','file','public fixture'),
                                      ('mbedtls-4.1.1/subdir/link.h','link','../source.h')])
        result = qualifier.extract(archive, output)
        self.assertEqual((result/'subdir/link.h').read_text(), 'public fixture')
        self.assertTrue((result/'subdir/link.h').resolve().is_relative_to(output.resolve()))

    def test_external_members_links_hardlinks_and_duplicates_are_refused_before_extraction(self):
        invalid = [('mbedtls-4.1.1/a/./b','file','x'), ('','file','x'), ('../escape','file','x'),('/absolute','file','x'),
                   ('mbedtls-4.1.1/link','link','../../escape'),
                   ('mbedtls-4.1.1/link','link','/absolute'),
                   ('mbedtls-4.1.1/link','hardlink','mbedtls-4.1.1/source.h')]
        for entry in invalid:
            with self.subTest(entry=entry[:2]):
                archive, output = self.fixture([entry])
                with self.assertRaises(ValueError):qualifier.extract(archive, output)
                self.assertEqual(list(output.iterdir()), [])
        archive, output = self.fixture([('mbedtls-4.1.1/same','file','a'),('mbedtls-4.1.1/same','file','b')])
        with self.assertRaises(ValueError):qualifier.extract(archive, output)
        self.assertEqual(list(output.iterdir()), [])



class NativeAllocatorCountsTests(unittest.TestCase):
    def test_only_complete_bounded_numeric_operation_evidence_is_accepted(self):
        spec = importlib.util.spec_from_file_location('native_counts', ROOT / 'tools/device-identity/allocator_qualification.py')
        native = importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for operation in native.OPERATIONS:(root / ('native-' + operation + '.counts')).write_bytes(b'100 5 5 0 5\n')
            result = native.read_counts(root)
            self.assertEqual(len(result), 8)
            self.assertEqual(result['identity']['observed_sites'], 5)
            for operation in ('tls-read','tls-write'):(root / ('native-' + operation + '.counts')).write_bytes(b'0 0 0 0 0\n')
            self.assertEqual(native.read_counts(root)['tls-read']['observed_sites'],0)
            target = root / 'native-identity.counts'
            for raw in [b'SECRET', b'1 0 0 0 0\n', b'1 2 2 0 2\n', b'100 5 4 0 5\n',
                        b'100 5 5 0 41\n', b'10000001 5 5 0 5\n', b'100 257 257 0 257\n',
                        b'100 5 5 0 5\nSECRET', b'x' * 97]:
                with self.subTest(raw=raw[:16]):
                    target.write_bytes(raw)
                    with self.assertRaises(ValueError):native.read_counts(root)


if __name__=='__main__':unittest.main()
