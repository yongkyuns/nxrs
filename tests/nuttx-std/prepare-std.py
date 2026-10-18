#!/usr/bin/env python3
"""Test scoped NuttX std fixes in a private copy, never the installed SDK."""
import argparse
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path('lib/rustlib/src/rust/library/std/src')
RELATIVE = ROOT / 'sys/sync/thread_parking/pthread.rs'
EXPECTED = '14bc793c15de254594778b59ad045006b58fd845'
ANCHOR = '        Pin::new_unchecked(&mut (*parker).cvar).init();\n'
INSERT = ('        // NuttX libc bindings do not encode its configured static mutex initializer.\n'
          '        #[cfg(target_os = "nuttx")]\n'
          '        Pin::new_unchecked(&mut (*parker).lock).init();\n\n')
UNIX = ROOT / 'sys/pal/unix/mod.rs'
UNIX_EXPECTED = {
    'nightly-2025-09-15': 'dd1059fe04a2daf0b4ce610f380813417642f48f',
    'esp-1.90.0.0': 'ba9e14b8009cd918cd9681dabe518ebf3c3a0a3a',
}
POLL_ANCHOR = '            target_os = "rtems",\n'
POLL_INSERT = ('            // NuttX pollfd has extra fields absent from this libc binding.\n'
               '            // Use the existing fcntl fallback; do not skip fd sanitization.\n'
               '            target_os = "nuttx",\n')
FAST = '        // fast path with a single syscall for systems with poll()\n'
FALLBACK = '        // fallback in case poll isn\'t available or limited by RLIMIT_NOFILE\n'
END = '    unsafe fn reset_sigpipe('
LIBRARY = Path('lib/rustlib/src/rust/library')
REPOSITORY = Path(__file__).resolve().parents[2]
SOCKET = ROOT / 'sys/net/connection/socket/unix.rs'
SOCKET_EXPECTED = {
    'nightly-2025-09-15': '8216f8d2fd58294cf777fffbf41951869695fc22',
    'esp-1.90.0.0': 'cc111f3521bc1516db00b69c510436060da844f8',
}
LIBC_POLL_PATCH = REPOSITORY / 'upstream/libc/patches/0001-nuttx-pollfd-abi.patch'
STD_POLL_PATCH = REPOSITORY / 'upstream/rust-std/patches/0001-native-pollfd-initialization.patch'
LIBC_SPECS = {
    'nightly-2025-09-15': ('0.2.175', '6a82ae493e598baaea5209805c49bbf2ea7de956d50d7da0da1164f9c6d28543'),
    'esp-1.90.0.0': ('0.2.174', '1171693293099992e19cddea4e8b849964e9846f4acee11b3948bcc337be8776'),
}
LIBC_NUTTX_BLOB = '69732d845b400e9b8781724d5a9dc3d5f3856913'
LIBC_ALIGN_ANCHOR = '    pub struct sockaddr_storage {\n'
LIBC_ALIGN_INSERT = '    #[repr(align(8))]\n'
# NuttX 13.1 aligns open flags with Linux; both pinned libc releases predate it.
# Keep a separate generated patch so the ABI migration is reviewable on its own.
LIBC_OPEN_FLAGS = {
    'O_RDONLY': (0x1, 0x0), 'O_WRONLY': (0x2, 0x1), 'O_RDWR': (0x3, 0x2),
    'O_CREAT': (0x4, 0x40), 'O_EXCL': (0x8, 0x80), 'O_NOCTTY': (0x0, 0x100),
    'O_TRUNC': (0x20, 0x200), 'O_APPEND': (0x10, 0x400),
    'O_NONBLOCK': (0x40, 0x800), 'O_DSYNC': (0x80, 0x1000),
    'O_DIRECT': (0x200, 0x4000), 'O_LARGEFILE': (0x2000, 0x8000),
    'O_DIRECTORY': (0x800, 0x10000), 'O_NOFOLLOW': (0x1000, 0x20000),
    'O_CLOEXEC': (0x400, 0x80000),
}
LIBRARY_PATCH_ANCHOR = "rustc-std-workspace-std = { path = 'rustc-std-workspace-std' }\n"
LIBRARY_PATCH_INSERT = "libc = { path = 'libc-nuttx' }\n"
SIG_MATCH = '            let (sigpipe_attr_specified, handler) = match sigpipe {\n'
SIG_INSERT = ('            // NuttX SIG_IGN is NULL, not the generic Unix libc value 1.\n'
              '            // This SDK profile measures the native constant before boot.\n'
              '            #[cfg(target_os = "nuttx")]\n'
              '            const IGNORE: libc::sighandler_t = 0;\n'
              '            #[cfg(not(target_os = "nuttx"))]\n'
              '            const IGNORE: libc::sighandler_t = libc::SIG_IGN;\n\n')


def blob(data):
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()


def patch(data):
    assert blob(data) == EXPECTED, 'unexpected Rust source; do not patch a different SDK'
    text = data.decode()
    assert text.count(ANCHOR) == 1, 'ambiguous parker initialization'
    return text.replace(ANCHOR, INSERT + ANCHOR).encode()


def patch_fds(data, sdk='nightly-2025-09-15'):
    assert blob(data) == UNIX_EXPECTED[sdk], 'unexpected Unix std source; refuse SDK drift'
    return transform_fds(data)


def transform_fds(data):
    text = data.decode()
    assert text.count(FAST) == text.count(FALLBACK) == text.count(END) == 1
    start, fallback, end = text.index(FAST), text.index(FALLBACK), text.index(END)
    assert start < fallback < end
    fast = text[start:fallback]
    assert fast.count(POLL_ANCHOR) == 1 and 'target_os = "nuttx"' not in fast
    tail = text[fallback:end]
    assert 'target_os = "nuttx"' not in tail, 'NuttX must keep the fcntl path'
    assert 'libc::fcntl(fd, libc::F_GETFD)' in tail
    return (text[:start] + fast.replace(POLL_ANCHOR, POLL_ANCHOR + POLL_INSERT)
            + text[fallback:]).encode()


def transform_sigign(data):
    text = data.decode()
    assert text.count(END) == text.count(SIG_MATCH) == 1
    start = text.index(END)
    prefix, tail = text[:start], text[start:]
    assert 'const IGNORE:' not in tail, 'already patched signal startup'
    assert tail.count('Some(libc::SIG_IGN)') == 2
    assert 'sigpipe::INHERIT => (true, None)' in tail
    assert 'sigpipe::SIG_DFL => (true, Some(libc::SIG_DFL))' in tail
    assert 'signal(libc::SIGPIPE, handler) != libc::SIG_ERR' in tail
    tail = tail.replace(SIG_MATCH, SIG_INSERT + SIG_MATCH)
    return (prefix + tail.replace('Some(libc::SIG_IGN)', 'Some(IGNORE)')).encode()


def copy_sdk(source, destination):
    assert not destination.exists(), 'private toolchain directory must be new'
    if sys.platform == 'darwin':
        # APFS copy-on-write clones keep isolation without duplicating the SDK's
        # binaries. Never use hard links: patching must not change the install.
        subprocess.run(['cp', '-cR', str(source), str(destination)], check=True)
    else:
        shutil.copytree(source, destination, symlinks=True)
    # Materialize the separate Xtensa rust-src distribution in the private copy.
    library = destination / LIBRARY
    if library.is_symlink():
        original = (source / LIBRARY).resolve(strict=True)
        library.unlink()
        shutil.copytree(original, library, symlinks=True)
    assert library.resolve().is_relative_to(destination.resolve()), 'SDK library escaped private copy'


def transform_libc_nuttx(data):
    assert blob(data) == LIBC_NUTTX_BLOB, 'unexpected libc NuttX binding; refuse crate drift'
    text = data.decode()
    assert text.count(LIBC_ALIGN_ANCHOR) == 1
    assert LIBC_ALIGN_INSERT not in text
    return text.replace(LIBC_ALIGN_ANCHOR, LIBC_ALIGN_INSERT + LIBC_ALIGN_ANCHOR).encode()


def transform_library_manifest(data):
    text = data.decode()
    assert text.count(LIBRARY_PATCH_ANCHOR) == 1
    assert LIBRARY_PATCH_INSERT not in text
    return text.replace(LIBRARY_PATCH_ANCHOR,
                        LIBRARY_PATCH_ANCHOR + LIBRARY_PATCH_INSERT).encode()


def transform_libc_open_flags(data):
    text = data.decode()
    for name, (old, new) in LIBC_OPEN_FLAGS.items():
        before = f'pub const {name}: i32 = {old:#x};\n'
        assert text.count(before) == 1, f'unexpected or already patched {name}'
        text = text.replace(before, f'pub const {name}: i32 = {new:#x};\n')
    return text.encode()


def transform_library_lock(data, version):
    text = data.decode()
    blocks = text.split('[[package]]')
    found = 0
    result = [blocks[0]]
    marker = f'name = "libc"\nversion = "{version}"\n'
    for body in blocks[1:]:
        block_text = '[[package]]' + body
        if marker in block_text:
            found += 1
            block_text = ''.join(
                line for line in block_text.splitlines(True)
                if not line.startswith('source = ') and not line.startswith('checksum = ')
            )
        result.append(block_text)
    assert found == 1, f'expected exactly one locked libc {version}, found {found}'
    return ''.join(result).encode()


def apply_dependency_patch(source, patch_file, output, name):
    # Archives nested in nxrs must not inherit the outer Git root. Only the
    # isolated SDK/crate copies prepared above are passed to this helper.
    source = source.resolve(strict=True)
    patch_file = patch_file.resolve(strict=True)
    environment = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(source.parent)}
    entries = subprocess.check_output(['git', 'apply', '--numstat', str(patch_file)], text=True).splitlines()
    paths = [source / entry.split('\t', 2)[2] for entry in entries]
    assert paths and all(path.is_file() and path.resolve().is_relative_to(source) for path in paths), 'unsafe dependency patch targets'
    before = {path: blob(path.read_bytes()) for path in paths}
    command = ['git', 'apply', '--check', str(patch_file)]
    checked = subprocess.run(command, cwd=source, env=environment,
                             capture_output=True, text=True)
    assert checked.returncode == 0, f'dependency patch rejected: {patch_file.name}: {checked.stderr}'
    subprocess.run(['git', 'apply', str(patch_file)], cwd=source,
                   env=environment, check=True)
    assert all(blob(path.read_bytes()) != before[path] for path in paths), 'dependency patch was skipped or made no change'
    shutil.copyfile(patch_file, output / name)
    return name


def vendor_libc(destination, output, sdk):
    version, checksum = LIBC_SPECS[sdk]
    url = f'https://static.crates.io/crates/libc/libc-{version}.crate'
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    assert hashlib.sha256(archive).hexdigest() == checksum, 'libc crate checksum mismatch'

    library = destination / LIBRARY
    vendored = library / 'libc-nuttx'
    assert not vendored.exists(), 'private libc override already exists'
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
            prefix = f'libc-{version}'
            for member in package.getmembers():
                path = Path(member.name)
                assert path.parts and path.parts[0] == prefix, 'unexpected libc archive layout'
                assert '..' not in path.parts, 'unsafe libc archive path'
            package.extractall(root, filter='data')
        shutil.copytree(root / prefix, vendored)

    nuttx = vendored / 'src/unix/nuttx/mod.rs'
    original_nuttx = nuttx.read_bytes()
    aligned_nuttx = transform_libc_nuttx(original_nuttx)
    changed_nuttx = transform_libc_open_flags(aligned_nuttx)
    nuttx.write_bytes(changed_nuttx)
    unix = vendored / 'src/unix/mod.rs'
    original_unix = unix.read_bytes()
    poll_patch = apply_dependency_patch(vendored, LIBC_POLL_PATCH, output,
                                       'libc-nuttx-poll.patch')

    manifest = library / 'Cargo.toml'
    original_manifest = manifest.read_bytes()
    changed_manifest = transform_library_manifest(original_manifest)
    manifest.write_bytes(changed_manifest)

    lock = library / 'Cargo.lock'
    assert lock.is_file(), 'rust-src library Cargo.lock is required by --locked build-std'
    original_lock = lock.read_bytes()
    changed_lock = transform_library_lock(original_lock, version)
    lock.write_bytes(changed_lock)

    patches = []
    for name, original, changed, source in [
        ('libc-sockaddr-storage.patch', original_nuttx, aligned_nuttx,
         f'libc-{version}/src/unix/nuttx/mod.rs'),
        ('libc-nuttx-13-open-flags.patch', aligned_nuttx, changed_nuttx,
         f'libc-{version}/src/unix/nuttx/mod.rs'),
        ('std-libc-override.patch', original_manifest, changed_manifest, str(LIBRARY / 'Cargo.toml')),
        ('std-libc-lock.patch', original_lock, changed_lock, str(LIBRARY / 'Cargo.lock')),
    ]:
        (output / name).write_text(''.join(difflib.unified_diff(
            original.decode().splitlines(True), changed.decode().splitlines(True),
            fromfile=source, tofile=source)))
        patches.append(name)

    return {
        'libc_version': version,
        'crate_sha256': checksum,
        'source_blob': blob(original_nuttx),
        'patched_blob': blob(nuttx.read_bytes()),
        'alignment': 8,
        'open_flags_abi': 'nuttx-13.1',
        'path': str((LIBRARY / 'libc-nuttx').as_posix()),
        'pollfd_abi': 'nuttx-13.1',
        'poll_sources': [
            {'path': 'src/unix/mod.rs', 'original_blob': blob(original_unix),
             'patched_blob': blob(unix.read_bytes())},
            {'path': 'src/unix/nuttx/mod.rs', 'original_blob': blob(changed_nuttx),
             'patched_blob': blob(nuttx.read_bytes())},
        ],
        'patches': patches + [poll_patch],
    }


def self_test():
    fixture = (FAST + '#[cfg(not(any(\n' + POLL_ANCHOR + ')))]\n\'poll: {}\n'
               + FALLBACK + 'libc::fcntl(fd, libc::F_GETFD);\n' + END + ') {}\n').encode()
    changed = transform_fds(fixture)
    assert changed.replace(POLL_INSERT.encode(), b'', 1) == fixture
    assert changed.split(FALLBACK.encode())[1] == fixture.split(FALLBACK.encode())[1]
    signal = (END + ') {\n' + SIG_MATCH
              + 'sigpipe::DEFAULT => (false, Some(libc::SIG_IGN)),\n'
              + 'sigpipe::INHERIT => (true, None),\n'
              + 'sigpipe::SIG_IGN => (true, Some(libc::SIG_IGN)),\n'
              + 'sigpipe::SIG_DFL => (true, Some(libc::SIG_DFL)),\n'
              + 'signal(libc::SIGPIPE, handler) != libc::SIG_ERR\n').encode()
    fixed_signal = transform_sigign(signal)
    assert fixed_signal.replace(SIG_INSERT.encode(), b'', 1).replace(b'Some(IGNORE)', b'Some(libc::SIG_IGN)') == signal
    rejected = 0
    cases = [(patch, b''), (patch_fds, b''),
             (lambda b: patch_fds(b, 'esp-1.90.0.0'), b''),
             (transform_fds, changed),
             (transform_fds, fixture.replace(POLL_ANCHOR.encode(), b'')),
             (transform_fds, fixture.replace(FALLBACK.encode(), FALLBACK.encode() * 2)),
             (transform_fds, fixture.replace(END.encode(), b'target_os = "nuttx"\n' + END.encode())),
             (transform_sigign, fixed_signal),
             (transform_sigign, signal.replace(SIG_MATCH.encode(), b'')),
             (transform_sigign, signal.replace(SIG_MATCH.encode(), SIG_MATCH.encode() * 2)),
             (transform_sigign, signal.replace(b'INHERIT => (true, None)', b'INHERIT => (true, Some(0))')),
             (transform_sigign, signal.replace(b'signal(libc::SIGPIPE, handler)', b'skipped'))]
    for function, bad in cases:
        try:
            function(bad)
        except AssertionError:
            rejected += 1
        else:
            raise AssertionError('accepted invalid or already patched SDK')
    libc_fixture = ('s! {\n' + LIBC_ALIGN_ANCHOR + '        pub ss_family: sa_family_t,\n'
                    '    }\n}\n').encode()
    # The real source is blob-pinned; exercise only the structural transform here.
    transformed = libc_fixture.decode().replace(
        LIBC_ALIGN_ANCHOR, LIBC_ALIGN_INSERT + LIBC_ALIGN_ANCHOR).encode()
    assert transformed.count(LIBC_ALIGN_INSERT.encode()) == 1
    flags = ''.join(f'pub const {name}: i32 = {old:#x};\n'
                    for name, (old, _) in LIBC_OPEN_FLAGS.items()).encode()
    fixed_flags = transform_libc_open_flags(flags)
    for name, (_, new) in LIBC_OPEN_FLAGS.items():
        assert f'pub const {name}: i32 = {new:#x};\n'.encode() in fixed_flags
    for bad in (b'', fixed_flags, flags.replace(b'O_RDWR', b'O_RDWR_DRIFT')):
        try:
            transform_libc_open_flags(bad)
        except AssertionError:
            pass
        else:
            raise AssertionError('accepted drift or repeated open-flag patch')
    manifest = (LIBRARY_PATCH_ANCHOR + '[workspace]\n').encode()
    assert transform_library_manifest(manifest).count(LIBRARY_PATCH_INSERT.encode()) == 1
    lock = ('[[package]]\nname = "libc"\nversion = "0.2.175"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            'checksum = "abc"\ndependencies = []\n').encode()
    fixed_lock = transform_library_lock(lock, '0.2.175')
    assert b'source = ' not in fixed_lock and b'checksum = ' not in fixed_lock
    print(f'PASS: {rejected} SDK rejection controls; fcntl, inheritance and signal call retained')
    print('PASS: private libc override manifest/lock transforms are structural and fail closed')
    print('PASS: 15 NuttX 13.1 open-flag replacements; drift/reapplication rejected')
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        source, external, destination = base / 'installed', base / 'rust-src', base / 'copy'
        external.mkdir()
        (external / 'witness').write_text('original')
        (source / LIBRARY).parent.mkdir(parents=True)
        (source / LIBRARY).symlink_to(external, target_is_directory=True)
        (source / 'binary-witness').write_text('installed')
        copy_sdk(source, destination)
        assert not (destination / LIBRARY).is_symlink()
        (destination / LIBRARY / 'witness').write_text('changed')
        assert (external / 'witness').read_text() == 'original'
        assert (source / LIBRARY).is_symlink()
        (destination / 'binary-witness').write_text('private')
        assert (source / 'binary-witness').read_text() == 'installed'
    print('PASS: private SDK source symlink materialized without installed-source mutation')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--sdk', choices=UNIX_EXPECTED, default='nightly-2025-09-15')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        if args.source is None or args.output is None:
            parser.error('--source and --output are required')
        originals = {relative: (args.source / relative).read_bytes()
                     for relative in (RELATIVE, UNIX, SOCKET)}
        # Hash-check both original inputs before transforming or copying.
        parker = patch(originals[RELATIVE])
        fd_fixed = patch_fds(originals[UNIX], args.sdk)
        assert blob(originals[SOCKET]) == SOCKET_EXPECTED[args.sdk], 'unexpected socket std source; refuse SDK drift'
        changes = {RELATIVE: parker, UNIX: transform_sigign(fd_fixed)}
        destination = args.output / 'toolchain'
        copy_sdk(args.source, destination)
        libc_record = vendor_libc(destination, args.output, args.sdk)
        records = []
        for relative, changed in changes.items():
            original = originals[relative]
            target = destination / relative
            assert target.resolve().is_relative_to(destination.resolve()), 'source symlink escaped private copy'
            target.write_bytes(changed)
            assert (args.source / relative).read_bytes() == original, 'installed source changed'
            name = 'std-parker.patch' if relative == RELATIVE else 'std-fd-sanitization.patch'
            (args.output / name).write_text(''.join(difflib.unified_diff(
                original.decode().splitlines(True),
                (changed if relative == RELATIVE else fd_fixed).decode().splitlines(True),
                fromfile=str(relative), tofile=str(relative))))
            patches = [name]
            if relative == UNIX:
                name = 'std-sigign.patch'
                (args.output / name).write_text(''.join(difflib.unified_diff(
                    fd_fixed.decode().splitlines(True), changed.decode().splitlines(True),
                    fromfile=str(relative), tofile=str(relative))))
                patches.append(name)
            records.append({'source_path': str(relative), 'original_blob': blob(original),
                            'patched_blob': blob(changed), 'patches': patches})
        original_socket = originals[SOCKET]
        name = apply_dependency_patch(destination / LIBRARY, STD_POLL_PATCH,
                                      args.output, 'std-pollfd-init.patch')
        records.append({'source_path': str(SOCKET), 'original_blob': blob(original_socket),
                        'patched_blob': blob((destination / SOCKET).read_bytes()),
                        'patches': [name]})
        assert (args.source / SOCKET).read_bytes() == original_socket, 'installed source changed'
        patch_names = libc_record['patches'] + [name for entry in records for name in entry['patches']]
        patchset = {name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
                    for name in patch_names}
        (args.output / 'std-patch.json').write_text(json.dumps({
            'mode': 'nuttx-parker-fcntl-sigign-and-libc-socket-align',
            'sdk': args.sdk, 'files': records, 'libc': libc_record,
            'patchset_sha256': patchset,
            'startup_sig_ign': 0, 'libc_sockaddr_storage_alignment': 8,
            'installed_toolchain_unchanged': True,
        }, indent=2) + '\n')
