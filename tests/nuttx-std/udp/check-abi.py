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
        '0.2.191',
        '65cd87c9a6d60536580b316ad985726104730a5151c99845f2490428ef91240a',
        'riscv64-unknown-elf-',
    ),
    'xtensa-esp32s3-nuttx': (
        '0.2.191',
        '65cd87c9a6d60536580b316ad985726104730a5151c99845f2490428ef91240a',
        'xtensa-esp32s3-elf-',
    ),
}
EXPECTED_LIBC_SOURCE_BLOB = '4970511599f7da3ab4b9ab7c24fd09b586197454'
EXPECTED_PATCH_MODE = 'nuttx-parker-fcntl-sigign-and-libc-socket-align'


def fields():
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
    return rows


def compare(native, rust):
    rows = fields()
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
                scope='listed IPv4 UDP, timeout, address storage and descriptor observations; not all std::net')


def private_libc_source(proof):
    assert proof['selected_source_matches'] is True, 'unverified std selection'
    library = Path(proof['rustc']).resolve().parent.parent / 'lib/rustlib/src/rust/library'
    expected_std = (library / 'std/src/lib.rs').resolve()
    assert Path(proof['expected_source']).resolve() == expected_std, 'std does not belong to the private compiler SDK'
    assert Path(proof['std_source']).resolve() == expected_std, 'different std source selected'
    return (library / 'libc-nuttx/src/lib.rs').resolve()


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
    for location in ('fixture-output/toolchain', 'fixture-shared-sdk/toolchain'):
        sdk = Path(location).resolve()
        source = str(sdk / 'lib/rustlib/src/rust/library/std/src/lib.rs')
        proof = dict(rustc=str(sdk / 'bin/rustc'), selected_source_matches=True,
                     expected_source=source, std_source=source)
        assert private_libc_source(proof) == sdk / 'lib/rustlib/src/rust/library/libc-nuttx/src/lib.rs'
        for key, value in (('selected_source_matches', False),
                           ('expected_source', str(sdk / 'foreign/std/src/lib.rs')),
                           ('std_source', str(sdk / 'foreign/std/src/lib.rs'))):
            try:
                private_libc_source(dict(proof, **{key: value}))
            except AssertionError:
                pass
            else:
                raise AssertionError('accepted inconsistent private SDK provenance')
    print('PASS: owned/shared private SDK identity and inconsistent std provenance rejection')


def run(out):
    out = out.resolve()
    core = json.loads((out / 'abi-report.json').read_text())
    target = core['target']
    assert target in EXPECTED_LIBC, f'unsupported socket ABI target: {target}'
    expected_version, expected_crate_sha256, cross_prefix = EXPECTED_LIBC[target]
    assert core['probe_abi_compatible'] and not core['errors'], 'thread/startup ABI gate failed'
    assert hashlib.sha256((out / 'resolved.config').read_bytes()).hexdigest() == core['config_sha256']
    proof = json.loads((out / 'std-source.json').read_text())
    expected_libc = private_libc_source(proof)

    patch = json.loads((out / 'std-patch.json').read_text())
    assert patch.get('mode') == EXPECTED_PATCH_MODE, 'wrong private SDK patch mode'
    assert patch.get('libc_sockaddr_storage_alignment') == 8, 'socket alignment repair not declared'
    libc_patch = patch.get('libc', {})
    assert libc_patch.get('libc_version') == expected_version, 'wrong vendored libc version'
    assert libc_patch.get('crate_sha256') == expected_crate_sha256, 'wrong vendored libc crate'
    assert libc_patch.get('source_blob') == EXPECTED_LIBC_SOURCE_BLOB, 'unexpected original NuttX libc binding'
    assert libc_patch.get('alignment') == 8, 'wrong private libc alignment repair'

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
    records = [json.loads(line) for line in (out / 'cargo-messages.jsonl').read_text().splitlines()]
    recorded_metadata = {(m['package_id'], m['target']['name'], str(Path(p).resolve()))
                         for m in records if m.get('reason') == 'compiler-artifact'
                         for p in m['filenames'] if p.endswith('.rmeta')}
    for metadata in core['rust_metadata']:
        path = Path(metadata['path'])
        assert path.is_relative_to(Path(core['target_artifacts'])), 'not target metadata'
        assert (metadata['package_id'], metadata['crate'], str(path)) in recorded_metadata, 'unrecorded metadata'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata['sha256'], 'changed Rust metadata'
    metadata_paths = {m['path'] for m in core['rust_metadata']}
    for name in ('core', 'compiler_builtins', 'libc'):
        selected = [arg.removeprefix(name + '=') for arg in commands[1] if arg.startswith(name + '=')]
        assert len(selected) == 1 and selected[0] in metadata_paths, f'unverified {name} metadata'
    search_dirs = {arg.removeprefix('-Ldependency=') for arg in commands[1] if arg.startswith('-Ldependency=')}
    assert search_dirs == {str(Path(p).parent) for p in metadata_paths}, 'changed metadata search directories'
    libc_metadata = [m for m in core['rust_metadata'] if m['crate'] == 'libc']
    assert len(libc_metadata) == 1 and libraries[0] == Path(libc_metadata[0]['path']), 'wrong libc metadata'
    library = Path(core['libc_rlib'])
    assert library.with_suffix('.rmeta') == libraries[0], 'unpaired libc archive and metadata'
    assert any(m.get('reason') == 'compiler-artifact' and m['package_id'] == core['libc_package']
               and str(library) in m['filenames'] and str(libraries[0]) in m['filenames']
               for m in records), 'libc archive and metadata did not come from the same build'
    assert hashlib.sha256(library.read_bytes()).hexdigest() == core['libc_rlib_sha256'], 'changed libc archive'
    rows = fields()
    c_source = ('#include <nuttx/config.h>\n#include <stdint.h>\n#include <stddef.h>\n'
                '#include <sys/types.h>\n#include <sys/socket.h>\n#include <netinet/in.h>\n'
                '#include <sys/time.h>\n#include <sys/ioctl.h>\n#include <fcntl.h>\n#include <errno.h>\n'
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
    result = compare(values['native'], values['rust'])
    result.update(commands=executed, target=core['target'], libc_package=core['libc_package'],
                  libc_rlib=core['libc_rlib'], libc_rlib_sha256=core['libc_rlib_sha256'],
                  rust_metadata=core['rust_metadata'], config_sha256=core['config_sha256'],
                  image_sha256=hashlib.sha256((out / 'nuttx/nuttx').read_bytes()).hexdigest())
    (out / 'socket-abi-report.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['socket_abi_compatible'], f'Socket ABI gate rejected: {result["errors"]}'
    print(f'PASS: {len(rows)} socket ABI observations; runtime qualification still required')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.out is None:
        parser.error('--out is required')
    else:
        run(args.out)
