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


if __name__=='__main__':unittest.main()
