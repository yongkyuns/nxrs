"""Pinned polling patches apply to isolated copies and fail closed."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


PREPARE = module('prepare_std_poll', 'tests/nuttx-std/prepare-std.py')
ABI = module('check_std_poll', 'tests/nuttx-std/check-abi.py')

UNIX = '''s! {
    pub struct iovec {
        pub iov_base: *mut c_void,
        pub iov_len: size_t,
    }

    pub struct pollfd {
        pub fd: c_int,
        pub events: c_short,
        pub revents: c_short,
    }

    pub struct winsize {
    }
}
'''
NUTTX = '''pub type time_t = i64;
pub type wchar_t = i32;

s! {
    pub struct stat {
        pub st_dev: dev_t,
    }
}

// sys/poll.h
pub const POLLIN: i16 = 0x01;
pub const POLLOUT: i16 = 0x04;
pub const POLLHUP: i16 = 0x10;
pub const POLLERR: i16 = 0x08;
pub const POLLNVAL: i16 = 0x20;

// sys/socket.h
'''


class PollAbiTests(unittest.TestCase):
    def test_libc_patch_does_not_depend_on_neighboring_platform_attributes(self):
        # libc 0.2.174 has an AIX attribute here; 0.2.175 does not.
        for source in (UNIX, UNIX.replace('    pub struct winsize {',
                                         '    #[cfg(not(target_os = "aix"))]\n    pub struct winsize {')):
            with self.subTest(aix_attribute='aix' in source), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                archive = Path(directory) / 'libc'
                (archive / 'src/unix/nuttx').mkdir(parents=True)
                (archive / 'src/unix/mod.rs').write_text(source)
                (archive / 'src/unix/nuttx/mod.rs').write_text(NUTTX)
                PREPARE.apply_dependency_patch(archive, PREPARE.LIBC_POLL_PATCH,
                                               Path(directory), 'poll.patch')
                self.assertIn('#[cfg(not(target_os = "nuttx"))]',
                              (archive / 'src/unix/mod.rs').read_text())

    def test_public_libc_patch_is_applied_even_with_a_relative_archive_path(self):
        # Reproduce Git's nested-workspace skip without touching a dependency.
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            archive = Path(directory) / 'libc'
            (archive / 'src/unix/nuttx').mkdir(parents=True)
            unix, nuttx = archive / 'src/unix/mod.rs', archive / 'src/unix/nuttx/mod.rs'
            unix.write_text(UNIX)
            nuttx.write_text(NUTTX)
            PREPARE.apply_dependency_patch(archive.relative_to(ROOT), PREPARE.LIBC_POLL_PATCH,
                                           Path(directory), 'poll.patch')
            self.assertIn('#[cfg(not(target_os = "nuttx"))]', unix.read_text())
            self.assertIn('pub cb: pollcb_t,', nuttx.read_text())
            self.assertIn('pub const POLLOUT: pollevent_t', nuttx.read_text())
            self.assertEqual((Path(directory) / 'poll.patch').read_bytes(), PREPARE.LIBC_POLL_PATCH.read_bytes())
            with self.assertRaisesRegex(AssertionError, 'patch rejected'):
                PREPARE.apply_dependency_patch(archive, PREPARE.LIBC_POLL_PATCH,
                                               Path(directory), 'repeated.patch')

    def test_layout_and_constants_are_all_required_before_allowing_poll(self):
        native = {row[0]: 4 for row in ABI.fields(True)}
        native.update(SIG_IGN=0, SIG_DFL=0, SIG_ERR=0xffffffff)
        rust = dict(native, SIG_IGN=1)
        self.assertTrue(ABI.compare(native, rust, True, True)['poll_abi_compatible'])
        for name in native:
            if name.startswith(('pollfd.', 'pollevent_t.', 'pollcb_t.', 'POLL')):
                with self.subTest(field=name):
                    changed = dict(rust, **{name: rust[name] ^ 1})
                    self.assertFalse(ABI.compare(native, changed, True, True)['poll_abi_compatible'])
        with self.assertRaises(AssertionError):
            ABI.compare(native, {key: value for key, value in rust.items() if key != 'pollfd.cb'}, True, True)

    def test_symbol_only_check_cannot_allow_unqualified_poll_or_ppoll(self):
        with self.assertRaises(AssertionError):
            ABI.check_imports(' U poll\n')
        self.assertEqual(ABI.check_imports(' U poll\n', True), ['poll'])
        with self.assertRaises(AssertionError):
            ABI.check_imports(' U ppoll\n', True)

    def test_std_patch_initializes_the_whole_native_descriptor(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            archive = Path(directory) / 'library'
            source = archive / 'std/src/sys/net/connection/socket/unix.rs'
            source.parent.mkdir(parents=True)
            source.write_text('''        }

        let mut pollfd = libc::pollfd { fd: self.as_raw_fd(), events: libc::POLLOUT, revents: 0 };

        if timeout.as_secs() == 0 && timeout.subsec_nanos() == 0 {
            return Err(io::Error::ZERO_TIMEOUT);
''')
            PREPARE.apply_dependency_patch(archive, PREPARE.STD_POLL_PATCH, Path(directory), 'std.patch')
            self.assertIn('mem::zeroed()', source.read_text())
            self.assertNotIn('libc::pollfd {', source.read_text())


if __name__ == '__main__':
    unittest.main()
