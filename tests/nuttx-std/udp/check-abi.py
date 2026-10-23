#!/usr/bin/env python3
"""Measure socket ABI using the exact toolchain/dependencies from the std gate.

The existing thread gate produces two fully specified witness compile commands.
Reuse those commands, changing only witness source/object paths. No independent
libc resolution, guessed host layout, or whitelist of socket mismatches.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess


EXPECTED_LIBC = {
    'riscv32imac-unknown-nuttx-elf': (
        '0.2.175',
        '6a82ae493e598baaea5209805c49bbf2ea7de956d50d7da0da1164f9c6d28543',
        'riscv64-unknown-elf-',
    ),
    'xtensa-esp32s3-nuttx': (
        '0.2.174',
        '1171693293099992e19cddea4e8b849964e9846f4acee11b3948bcc337be8776',
        'xtensa-esp32s3-elf-',
    ),
}
EXPECTED_LIBC_SOURCE_BLOB = '69732d845b400e9b8781724d5a9dc3d5f3856913'
EXPECTED_PATCH_MODE = 'nuttx-parker-fcntl-sigign-and-libc-socket-align'


def fields(include_tcp=False):
    rows = []
    for name in ('socklen_t', 'sa_family_t', 'in_port_t', 'ssize_t',
                 'sockaddr', 'sockaddr_in', 'in_addr', 'sockaddr_storage', 'timeval'):
        ctype = ('struct ' if name in ('sockaddr', 'sockaddr_in', 'in_addr', 'sockaddr_storage', 'timeval') else '') + name
        for part, c, rust in (('size', f'sizeof({ctype})', f'size_of::<libc::{name}>()'),
                              ('align', f'_Alignof({ctype})', f'align_of::<libc::{name}>()')):
            # Generic address storage may be overprovisioned, never underaligned.
            rule = 'capacity' if name == 'sockaddr_storage' else 'exact'
            rows.append((f'{name}.{part}', c, rust, rule))
    for name, members in (('sockaddr', ('sa_family', 'sa_data')),
                          ('sockaddr_in', ('sin_family', 'sin_port', 'sin_addr', 'sin_zero')),
                          ('in_addr', ('s_addr',)), ('sockaddr_storage', ('ss_family',)),
                          ('timeval', ('tv_sec', 'tv_usec'))):
        for member in members:
            rows.append((f'{name}.{member}', f'offsetof(struct {name}, {member})',
                         f'offset_of!(libc::{name}, {member})', 'exact'))
    for name in ('AF_INET', 'SOCK_DGRAM', 'SOL_SOCKET', 'SO_RCVTIMEO', 'SO_ERROR',
                 'FIONBIO', 'FIOCLEX', 'F_DUPFD_CLOEXEC', 'EAGAIN', 'EWOULDBLOCK',
                 'EINTR', 'ETIMEDOUT', 'EINVAL', 'EADDRINUSE'):
        rows.append((name, name, f'libc::{name}', 'exact'))
    if include_tcp:
        # The pinned NuttX std creates/accepts sockets with socket/accept then
        # FileDesc::set_cloexec (FIOCLEX, already measured above). Its libc
        # exports neither SOCK_CLOEXEC nor SO_TYPE, and this path uses neither.
        # connect_timeout also uses the core gate's measured pollfd/POLL* ABI.
        for name in ('SOCK_STREAM', 'IPPROTO_TCP', 'TCP_NODELAY', 'SO_SNDTIMEO',
                     'SO_REUSEADDR', 'SHUT_RD', 'SHUT_WR', 'SHUT_RDWR',
                     'EINPROGRESS', 'EISCONN', 'EPIPE', 'ECONNRESET', 'ENOTCONN'):
            rows.append((name, name, f'libc::{name}', 'exact'))
    return rows


def compare(native, rust, include_tcp=False):
    rows = fields(include_tcp)
    names = {row[0] for row in rows}
    assert set(native) == set(rust) == names, 'incomplete socket evidence'
    checks = []
    for name, _, _, rule in rows:
        c, r = native[name], rust[name]
        valid = r == c
        if rule == 'capacity':
            valid = r >= c
            if name.endswith('.align'):
                valid = c > 0 and r >= c and r % c == 0
        checks.append(dict(name=name, native=c, rust=r, rule=rule, compatible=valid))
    errors = [row['name'] for row in checks if not row['compatible']]
    return dict(checks=checks, errors=errors, socket_abi_compatible=not errors,
                scope=('listed IPv4 UDP/TCP, timeout, address storage, descriptor and shutdown observations; '
                       'not all std::net' if include_tcp else
                       'listed IPv4 UDP, timeout, address storage and descriptor observations; not all std::net'))


def self_test():
    c = {row[0]: 4 for row in fields()}
    c.update({'sockaddr_storage.size': 128, 'sockaddr_storage.align': 8})
    r = dict(c)
    assert compare(c, r)['socket_abi_compatible']
    assert compare(c, dict(r, **{'sockaddr_storage.size': 160}))['socket_abi_compatible']
    for key, value in [('sockaddr_storage.size', 64), ('sockaddr_storage.align', 4),
                       ('sockaddr_storage.align', 12), ('sockaddr_in.sin_addr', 5),
                       ('socklen_t.size', 8), ('FIONBIO', 0), ('SO_RCVTIMEO', 0),
                       ('EWOULDBLOCK', 999), ('timeval.tv_usec', 0)]:
        assert not compare(c, dict(r, **{key: value}))['socket_abi_compatible']
    try:
        compare({}, r)
    except AssertionError:
        pass
    else:
        raise AssertionError('accepted missing socket evidence')
    print('PASS: ten socket ABI rejection controls; safe overprovisioning accepted')
    for name in ('SOCK_STREAM', 'TCP_NODELAY', 'SO_SNDTIMEO', 'SHUT_RDWR', 'EINPROGRESS', 'EISCONN'):
        native = {row[0]: 4 for row in fields(True)}
        assert compare(native, native.copy(), True)['socket_abi_compatible']
        changed = dict(native, **{name: 8})
        assert not compare(native, changed, True)['socket_abi_compatible']
    print('PASS: six additional TCP/socket-option/connection-error rejection controls')


def run(out, include_tcp=False):
    out = out.resolve()
    core = json.loads((out / 'abi-report.json').read_text())
    target = core['target']
    assert target in EXPECTED_LIBC, f'unsupported socket ABI target: {target}'
    expected_version, expected_crate_sha256, cross_prefix = EXPECTED_LIBC[target]
    assert core['probe_abi_compatible'] and not core['errors'], 'thread/startup ABI gate failed'
    if include_tcp:
        assert core['poll_abi_compatible'] is True, 'TCP connect_timeout needs qualified poll ABI'
    assert hashlib.sha256((out / 'resolved.config').read_bytes()).hexdigest() == core['config_sha256']
    proof = json.loads((out / 'std-source.json').read_text())
    assert proof['selected_source_matches'], 'unverified std selection'

    patch = json.loads((out / 'std-patch.json').read_text())
    assert patch.get('mode') == EXPECTED_PATCH_MODE, 'wrong private SDK patch mode'
    assert patch.get('libc_sockaddr_storage_alignment') == 8, 'socket alignment repair not declared'
    libc_patch = patch.get('libc', {})
    assert libc_patch.get('libc_version') == expected_version, 'wrong vendored libc version'
    assert libc_patch.get('crate_sha256') == expected_crate_sha256, 'wrong vendored libc crate'
    assert libc_patch.get('source_blob') == EXPECTED_LIBC_SOURCE_BLOB, 'unexpected original NuttX libc binding'
    assert libc_patch.get('alignment') == 8, 'wrong private libc alignment repair'

    expected_libc = (out / 'toolchain/lib/rustlib/src/rust/library/libc-nuttx/src/lib.rs').resolve()
    assert Path(core['libc_source']).resolve() == expected_libc, 'compiled libc did not come from private SDK'
    assert core['libc_package'].endswith('libc@' + expected_version), 'unexpected compiled libc version'
    binding = expected_libc.parent / 'unix/nuttx/mod.rs'
    binding_text = binding.read_text()
    assert binding_text.count('#[repr(align(8))]\n    pub struct sockaddr_storage {') == 1, (
        'private libc does not contain the qualified sockaddr_storage alignment repair'
    )

    commands = core['commands'][:2]
    assert len(commands) == 2
    native_compiler = commands[0][0]
    assert Path(native_compiler).name == cross_prefix + 'gcc', 'wrong native witness compiler'
    assert commands[1][0] == str(Path(proof['rustc']).resolve()), 'wrong Rust compiler'
    libraries = [Path(arg.removeprefix('libc=')) for arg in commands[1] if arg.startswith('libc=')]
    assert len(libraries) == 1
    assert hashlib.sha256(libraries[0].read_bytes()).hexdigest() == core['libc_rlib_sha256'], 'changed libc'
    rows = fields(include_tcp)
    c_source = ('#include <nuttx/config.h>\n#include <stdint.h>\n#include <stddef.h>\n'
                '#include <sys/types.h>\n#include <sys/socket.h>\n#include <netinet/in.h>\n'
                '#include <sys/time.h>\n#include <sys/ioctl.h>\n#include <fcntl.h>\n#include <errno.h>\n'
                + ('#include <netinet/tcp.h>\n' if include_tcp else '')
                +
                '__attribute__((section(".nxrs_abi"), used))\nconst uint32_t socket_abi[] = {\n'
                + ''.join(f'  (uint32_t)({c}), /* {name} */\n' for name, c, _, _ in rows) + '};\n')
    rust_source = ('#![no_std]\n#![feature(rustc_private)]\nextern crate libc;\n'
                   'use core::mem::{size_of, align_of, offset_of};\n'
                   '#[used]\n#[no_mangle]\n#[link_section = ".nxrs_abi"]\n'
                   f'pub static SOCKET_ABI: [u32; {len(rows)}] = [\n'
                   + ''.join(f'    ({r}) as u32, // {name}\n' for name, _, r, _ in rows) + '];\n')
    (out / 'socket-native.c').write_text(c_source)
    (out / 'socket-rust.rs').write_text(rust_source)
    executed, values = [], {}
    for role, extension, original in zip(('native', 'rust'), ('c', 'rs'), commands):
        replacements = {str(out / f'abi-{role}.{extension}'): str(out / f'socket-{role}.{extension}'),
                        str(out / f'abi-{role}.o'): str(out / f'socket-{role}.o')}
        assert all(original.count(key) == 1 for key in replacements), 'unexpected witness command'
        command = [replacements.get(arg, arg) for arg in original]
        subprocess.run(command, check=True)
        executed.append(command)
        compiler = Path(native_compiler)
        objcopy_name = cross_prefix + 'objcopy'
        objcopy = str(compiler.with_name(objcopy_name)) if compiler.parent != Path('.') else objcopy_name
        command = [objcopy, '--dump-section',
                   f'.nxrs_abi={out}/socket-{role}.bin', str(out / f'socket-{role}.o')]
        subprocess.run(command, check=True)
        executed.append(command)
        raw = (out / f'socket-{role}.bin').read_bytes()
        assert len(raw) == len(rows) * 4, 'wrong socket witness size'
        values[role] = dict(zip([row[0] for row in rows], struct.unpack('<' + 'I' * len(rows), raw)))
    result = compare(values['native'], values['rust'], include_tcp)
    result.update(tcp_constants_checked=include_tcp, commands=executed, target=core['target'], libc_package=core['libc_package'],
                  libc_rlib_sha256=core['libc_rlib_sha256'], config_sha256=core['config_sha256'],
                  image_sha256=hashlib.sha256((out / 'nuttx/nuttx').read_bytes()).hexdigest())
    (out / 'socket-abi-report.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['socket_abi_compatible'], f'Socket ABI gate rejected: {result["errors"]}'
    print(f'PASS: {len(rows)} socket ABI observations; runtime qualification still required')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--tcp', action='store_true', help='add actual TCP socket/option/shutdown observations')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.out is None:
        parser.error('--out is required')
    else:
        run(args.out, args.tcp)
