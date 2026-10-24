import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_media_qualifier', ROOT / 'tools/native-media/qualify.py')
qualifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualifier)


class NativeMediaEvidenceTests(unittest.TestCase):
    def test_uninstrumented_or_partially_instrumented_native_object_cannot_qualify(self):
        names = ['__asan_report_load1', '__ubsan_handle_shift_out_of_bounds_abort',
                 '__sanitizer_cov_8bit_counters_init', '__sanitizer_cov_pcs_init',
                 '__sanitizer_cov_trace_const_cmp4']
        self.assertTrue(qualifier.instrumentation('\n'.join(names)))
        for raw in ['', '__asan_report_load1', *('\n'.join(names[:i] + names[i+1:]) for i in range(len(names)))]:
            with self.assertRaises(ValueError):
                qualifier.instrumentation(raw)

    def test_archive_cannot_escape_or_introduce_native_links(self):
        for name, kind, target in [('sdk/../escape', tarfile.REGTYPE, ''),
                                    ('/sdk/file', tarfile.REGTYPE, ''),
                                    ('sdk/link', tarfile.SYMTYPE, '../../escape'),
                                    ('sdk/hard', tarfile.LNKTYPE, 'sdk/file'),
                                    ('sdk/fifo', tarfile.FIFOTYPE, '')]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / 'source.tar'
                with tarfile.open(archive, 'w') as output:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    entry.linkname = target
                    output.addfile(entry)
                with self.assertRaises(ValueError):
                    qualifier.extract(archive, root / 'out', 'sdk')
                self.assertFalse((root / 'out').exists())
                self.assertFalse((root / 'escape').exists())

    def test_duplicate_archive_members_refuse_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'source.tar'
            with tarfile.open(archive, 'w') as output:
                for _ in range(2):
                    entry = tarfile.TarInfo('sdk/file')
                    entry.size = 1
                    output.addfile(entry, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                qualifier.extract(archive, root / 'out', 'sdk')
            self.assertFalse((root / 'out').exists())

    def test_valid_source_archive_keeps_exact_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'source.tar'
            with tarfile.open(archive, 'w') as output:
                entry = tarfile.TarInfo('sdk/file')
                entry.size = 8
                output.addfile(entry, io.BytesIO(b'upstream'))
            extracted = qualifier.extract(archive, root / 'out', 'sdk')
            self.assertEqual((extracted / 'file').read_bytes(), b'upstream')


if __name__ == '__main__':
    unittest.main()
