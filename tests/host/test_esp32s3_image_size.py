"""Link-map accounting controls: discarded input, relaxation and merged strings."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('image_size', Path(__file__).resolve().parents[2] / 'tools/esp32s3-image-size.py')
image_size = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_size)


class ImageSizeTests(unittest.TestCase):
    def test_counts_linked_intervals_once_including_shared_strings_and_fill(self):
        sections, entries = image_size.read_map('''Discarded input sections
 .text.unused 0x0000 0x80 libunused.a(unused.o)
.flash.text 0x1000 0x20
 .text.kept
                0x1000 0x10 libsched.a(start.o)
                           0x30 (size before relaxing)
 .text.kept 0x1010 0x08 libnet.a(net.o)
.flash.rodata
                0x2000 0x10
 .rodata.a.str1.1 0x2000 0x0a libsched.a(start.o)
 .rodata.b.str1.1 0x2002 0x08 libnet.a(net.o)
 .rodata.other 0x200a 0x04 libnet.a(net.o)
''')
        counts = image_size.account(sections, entries, tuple(sections))
        self.assertEqual(sum(sum(row.values()) for row in counts.values()), 48)
        self.assertEqual(counts['libsched.a']['.flash.text'], 16)
        self.assertEqual(counts['Merged strings (shared; ownership unavailable)']['.flash.rodata'], 10)
        self.assertEqual(sum(counts['Linker fill / unowned'].values()), 10)
        self.assertNotIn('libunused.a', counts)

    def test_native_function_sections_follow_input_ownership(self):
        native = image_size.sdk_sections('''.text.native_fn 0x0000 0x10
 .text.native_fn 0x0000 0x10 /tmp/libtls-123.rlib(sdk.o)
.rodata.shared 0x0000 0x08
 .rodata.a 0x0000 0x04 /tmp/libtls-123.rlib(sdk.o)
 .rodata.b 0x0004 0x04 rust.o
.text.rust_fn 0x0000 0x04
 .text.rust_fn 0x0000 0x04 rust.o
''')
        self.assertEqual(native, {'.text.native_fn'})
        self.assertEqual(image_size.component('.text.native_fn', 'libapps.a(nxrs_app_1.o)',
                                             set(), native), 'Application TLS native objects / ABI')


if __name__ == '__main__':
    unittest.main()
