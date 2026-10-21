"""Directory bindings must match NuttX before procfs inventory can be trusted."""
from pathlib import Path
import tempfile
import unittest

from test_poll_abi import ABI, PREPARE, ROOT

SOURCE = '''use crate::prelude::*;
use crate::{in6_addr, in_addr_t, timespec, DIR};

pub type nlink_t = u16;
pub type ino_t = u16;
pub type blkcnt_t = u64;
pub type blksize_t = i16;
pub type cc_t = u8;

s! {
    pub struct stat {
    }

    pub struct dirent {
        pub d_type: u8,
        pub d_name: [c_char; __NAME_MAX__ + 1],
    }
}
'''


class DirectoryAbiTests(unittest.TestCase):
    def test_patch_repairs_inode_directory_and_stat_block_size(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            crate = Path(directory) / 'libc'
            source = crate / 'src/unix/nuttx/mod.rs'
            source.parent.mkdir(parents=True)
            source.write_text(SOURCE)
            PREPARE.apply_dependency_patch(crate, PREPARE.LIBC_DIRECTORY_PATCH,
                                           Path(directory), 'directory.patch')
            changed = source.read_text()
            self.assertIn('pub type ino_t = u32;', changed)
            self.assertIn('pub type blksize_t = i32;', changed)
            self.assertIn('pub d_ino: ino_t,\n        pub d_type: u8,', changed)
            with self.assertRaisesRegex(AssertionError, 'patch rejected'):
                PREPARE.apply_dependency_patch(crate, PREPARE.LIBC_DIRECTORY_PATCH,
                                               Path(directory), 'repeated.patch')

    def test_every_public_filesystem_offset_and_width_is_required(self):
        native = {row[0]: 4 for row in ABI.fields(True)}
        native.update(SIG_IGN=0, SIG_DFL=0, SIG_ERR=0xffffffff)
        rust = dict(native, SIG_IGN=1)
        self.assertTrue(ABI.compare(native, rust, True, True)['probe_abi_compatible'])
        for name in native:
            if name.startswith(('dirent.', 'stat.', 'ino_t.', 'blksize_t.')):
                with self.subTest(field=name):
                    self.assertIn(name, ABI.compare(native, dict(rust, **{name: 1}),
                                                    True, True)['errors'])
        for name in ['dirent.d_ino', 'dirent.d_name', 'stat.st_blksize.size']:
            with self.subTest(missing=name), self.assertRaises(AssertionError):
                ABI.compare(native, {k: v for k, v in rust.items() if k != name}, True, True)

    def test_trailing_storage_can_be_larger_but_offsets_cannot_move(self):
        native = {row[0]: 4 for row in ABI.fields(True)}
        native.update(SIG_IGN=0, SIG_DFL=0, SIG_ERR=0xffffffff)
        rust = dict(native, SIG_IGN=1,
                    **{'dirent.size': 72, 'dirent.d_name.size': 65, 'stat.size': 128})
        self.assertTrue(ABI.compare(native, rust, True, True)['probe_abi_compatible'])
        self.assertIn('dirent.d_name', ABI.compare(native, dict(rust, **{'dirent.d_name': 1}),
                                                  True, True)['errors'])


if __name__ == '__main__':
    unittest.main()
