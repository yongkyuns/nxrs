#!/usr/bin/env python3
"""Compare target-compiled C/Rust ABI witnesses for the selected NuttX probe.

Uses the libc rlib reported by the std build, not an independently resolved crate.
This is a limited gate, not an assertion that every libc API is safe. Opaque
pthread objects need sufficient storage/alignment; public layouts and constants
need exact agreement except the explicitly contained, still-unsupported bindings.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess

TARGET = 'riscv32imac-unknown-nuttx-elf'
TARGETS = {
    TARGET: ('riscv64-unknown-elf-', ['-march=rv32imac', '-mabi=ilp32']),
    'thumbv8m.main-nuttx-eabi': ('arm-none-eabi-', ['-mcpu=cortex-m33', '-mthumb', '-mfloat-abi=soft']),
    'xtensa-esp32s3-nuttx': ('xtensa-esp32s3-elf-', ['-mlongcalls', '-mtext-section-literals']),
}
SCALARS = ['int', 'long', 'size_t', 'time_t', 'off_t', 'nfds_t', 'pthread_t', 'pthread_key_t']
OPAQUE = ['pthread_attr_t', 'pthread_mutex_t', 'pthread_mutexattr_t',
          'pthread_cond_t', 'pthread_condattr_t']
# This libc target does not export PTHREAD_MUTEX_RECURSIVE; do not fabricate it.
CONSTANTS = ['CLOCK_MONOTONIC', 'CLOCK_REALTIME', 'ETIMEDOUT', 'EINTR', 'EBADF',
             'F_GETFD', 'PTHREAD_MUTEX_NORMAL', 'SIGPIPE']
# Check every open flag exported by the pinned NuttX libc, including unchanged
# flags. NuttX 13.1 changed the access modes and most of the flag bit positions.
OPEN_FLAGS = ['O_RDONLY', 'O_WRONLY', 'O_RDWR', 'O_CREAT', 'O_EXCL', 'O_NOCTTY',
              'O_TRUNC', 'O_APPEND', 'O_NONBLOCK', 'O_DSYNC', 'O_DIRECT',
              'O_LARGEFILE', 'O_DIRECTORY', 'O_NOFOLLOW', 'O_NOATIME',
              'O_CLOEXEC', 'O_ACCMODE']
CONSTANTS += OPEN_FLAGS
SIGNAL_HANDLERS = ['SIG_IGN', 'SIG_DFL', 'SIG_ERR']


def check_imports(text, poll_qualified=False):
    # Native NuttX code legitimately uses poll with its own matching C layout.
    undefined = {parts[-1] for line in text.splitlines()
                 if len(parts := line.split()) >= 2 and parts[-2] in ('U', 'w', 'v')}
    forbidden = sorted(undefined & ({'ppoll'} if poll_qualified else {'poll', 'ppoll'}))
    assert not forbidden, f'Rust imports known-incompatible pollfd ABI: {forbidden}'
    return sorted(undefined)


def fields(poll_fixed=False):
    rows = []
    for name in SCALARS + OPAQUE + ['timespec', 'pollfd'] + (['pollevent_t', 'pollcb_t'] if poll_fixed else []):
        c_type = 'struct ' + name if name in ('timespec', 'pollfd') else name
        rust_type = {'int': 'c_int', 'long': 'c_long'}.get(name, name)
        for kind, c, rust in [
                ('size', f'sizeof({c_type})', f'size_of::<libc::{rust_type}>()'),
                ('align', f'_Alignof({c_type})', f'align_of::<libc::{rust_type}>()')]:
            rule = 'capacity' if name in OPAQUE else 'exact'
            rows.append((f'{name}.{kind}', c, rust, rule))
    for name, members in [('timespec', ['tv_sec', 'tv_nsec']),
                          ('pollfd', ['fd', 'events', 'revents'] + (['arg', 'cb', 'priv'] if poll_fixed else []))]:
        for member in members:
            rust_member = 'priv_' if member == 'priv' else member
            rows.append((f'{name}.{member}', f'offsetof(struct {name}, {member})',
                         f'offset_of!(libc::{name}, {rust_member})', 'exact'))
    if poll_fixed:
        for member in ('events', 'revents'):
            rows.append((f'pollfd.{member}.size', f'sizeof(((struct pollfd *)0)->{member})',
                         f'size_of_val(&unsafe {{ core::mem::zeroed::<libc::pollfd>() }}.{member})', 'exact'))
    constants = CONSTANTS + (['POLLIN', 'POLLOUT', 'POLLERR', 'POLLHUP', 'POLLNVAL'] if poll_fixed else [])
    rows.extend((name, name, f'libc::{name}', 'exact') for name in constants)
    rows.extend((name, f'(uintptr_t){name}', f'libc::{name}', 'exact') for name in SIGNAL_HANDLERS)
    return rows


# These raw bindings stay visibly unqualified. Only the declared SDK startup
# workarounds contain their use in this probe; they do not repair libc generally.
KNOWN_POLLFDS = {'pollfd.size': (24, 8), 'pollfd.align': (4, 4),
                'pollfd.fd': (0, 0), 'pollfd.events': (4, 4), 'pollfd.revents': (8, 6)}


def compare(c_values, rust_values, fixes, poll_fixed=False):
    names = [row[0] for row in fields(poll_fixed)]
    assert set(c_values) == set(rust_values) == set(names), 'incomplete ABI evidence'
    checks, errors = [], []
    for name, _, _, rule in fields(poll_fixed):
        c, rust = c_values[name], rust_values[name]
        if name in KNOWN_POLLFDS and not poll_fixed:
            valid = fixes and (c, rust) == KNOWN_POLLFDS[name]
            status = 'known-unsupported-pollfd' if valid else 'unexpected-or-uncontained-pollfd'
        elif name == 'SIG_IGN':
            valid = fixes and (c, rust) == (0, 1)
            status = 'known-unsupported-SIG_IGN' if valid else 'unexpected-or-uncontained-SIG_IGN'
        else:
            valid = rust >= c if rule == 'capacity' else rust == c
            if rule == 'capacity' and name.endswith('.align'):
                valid = rust >= c and rust % c == 0
            status = 'compatible' if valid else 'incompatible'
        checks.append(dict(name=name, native=c, rust=rust, rule=rule, status=status))
        if not valid:
            errors.append(name)
    return dict(checks=checks, errors=errors, probe_abi_compatible=not errors,
                poll_abi_compatible=poll_fixed and not errors,
                general_signal_api_qualified=False,
                unavailable_bindings=['PTHREAD_MUTEX_RECURSIVE'],
                scope='listed layouts/constants; opaque capacity is not initializer equivalence; raw SIG_IGN remains wrong')


def self_test():
    c = {row[0]: 4 for row in fields()}
    c.update(SIGPIPE=13, SIG_DFL=0, SIG_ERR=0xffffffff)
    rust = dict(c)
    for key, (cv, rv) in {**KNOWN_POLLFDS, 'SIG_IGN': (0, 1)}.items():
        c[key], rust[key] = cv, rv
    assert compare(c, rust, True)['probe_abi_compatible']
    larger = dict(rust, **{'pthread_mutex_t.size': 64})
    assert compare(c, larger, True)['probe_abi_compatible']
    bad_cases = [(c, rust, False), (c, dict(rust, **{'timespec.size': 8}), True),
                 (c, dict(rust, **{'pthread_attr_t.size': 1}), True),
                 (c, dict(rust, **{'pthread_cond_t.align': 1}), True),
                 (c, dict(rust, **{'CLOCK_MONOTONIC': 99}), True),
                 (c, dict(rust, **{'pollfd.size': 24}), True),
                 (dict(c, SIG_IGN=2), rust, True),
                 (c, dict(rust, SIG_IGN=0), True),
                 (dict(c, SIG_DFL=1), rust, True),
                 (c, dict(rust, SIG_ERR=0), True),
                 (c, dict(rust, SIGPIPE=99), True)]
    for args in bad_cases:
        assert not compare(*args)['probe_abi_compatible'], 'accepted unsafe ABI evidence'
    for name in OPEN_FLAGS:
        assert name in compare(c, dict(rust, **{name: rust[name] ^ 1}), True)['errors']
    try:
        compare({}, rust, True)
    except AssertionError:
        pass
    else:
        raise AssertionError('accepted missing evidence')
    print(f'PASS: {len(bad_cases) + 1} ABI rejection controls; opaque overprovisioning accepted')
    print(f'PASS: {len(OPEN_FLAGS)} open-flag ABI rejection controls')
    assert check_imports('00000000 T main\n         U pthread_create\n') == ['pthread_create']
    assert check_imports('00000000 T poll\n         U poll_notify\n') == ['poll_notify']
    for text in ('         U poll\n', '         U ppoll\n', '         w poll\n', '         v ppoll\n'):
        try:
            check_imports(text)
        except AssertionError:
            pass
        else:
            raise AssertionError('accepted an unqualified Rust poll import')
    print('PASS: 4 unsafe-import rejection controls and 2 valid symbol controls')
    native = {row[0]: 4 for row in fields(True)}
    native.update(SIG_IGN=0, SIG_DFL=0, SIG_ERR=0xffffffff)
    rust = dict(native, SIG_IGN=1)
    assert compare(native, rust, True, True)['poll_abi_compatible']
    for name, _, _, _ in fields(True):
        if name.startswith(('pollfd.', 'pollevent_t.', 'pollcb_t.')) or name.startswith('POLL'):
            assert not compare(native, dict(rust, **{name: rust[name] ^ 1}), True, True)['poll_abi_compatible']
    assert check_imports('         U poll\n', True) == ['poll']
    try:
        check_imports('         U ppoll\n', True)
    except AssertionError:
        pass
    else:
        raise AssertionError('poll layout qualification must not qualify ppoll signal arguments')
    print('PASS: complete native poll layout/constant rejection controls; ppoll remains blocked')


def run(out, target=TARGET, target_spec=None):
    prefix, c_flags = TARGETS[target]
    out = out.resolve()
    target_arg = target
    if target == 'xtensa-esp32s3-nuttx':
        assert target_spec is not None, 'Xtensa needs the exact build target spec'
        assert target_spec.resolve() == out / (target + '.json'), 'wrong target-spec path'
        spec = json.loads(target_spec.read_text())
        assert spec['arch'] == 'xtensa' and spec['os'] == 'nuttx'
        assert spec['cpu'] == 'esp32s3' and str(spec['target-pointer-width']) == '32'
        assert spec['executables'] and spec['target-family'] == ['unix']
        target_arg = str(target_spec.resolve())
    else:
        assert target_spec is None, 'built-in targets must not use a custom spec'
    patch = json.loads((out / 'std-patch.json').read_text())
    poll_fixed = patch.get('libc', {}).get('pollfd_abi') == 'nuttx-13.1'
    rows = fields(poll_fixed)
    records = [json.loads(line) for line in (out / 'cargo-messages.jsonl').read_text().splitlines()]
    libs = [m for m in records if m.get('reason') == 'compiler-artifact'
            and m['target']['name'] == 'libc'
            and any(p.endswith('.rlib') for p in m['filenames'])]
    assert len(libs) == 1, 'ambiguous libc artifact'
    library = [Path(p) for p in libs[0]['filenames'] if p.endswith('.rlib')]
    assert len(library) == 1 and library[0].is_file(), 'missing compiled libc'
    library = library[0].resolve()
    assert f'/{target}/' in str(library), 'not the selected target libc'
    cores = [Path(p) for m in records if m.get('reason') == 'compiler-artifact'
             and m['target']['name'] == 'core' for p in m['filenames'] if p.endswith('.rlib')]
    assert len(cores) == 1 and cores[0].is_file(), 'missing exact rebuilt core'
    builtins = [Path(p) for m in records if m.get('reason') == 'compiler-artifact'
                and m['target']['name'] == 'compiler_builtins'
                for p in m['filenames'] if p.endswith('.rlib')]
    assert len(builtins) == 1 and builtins[0].is_file(), 'missing exact compiler_builtins'
    rustc = Path(os.environ['RUSTC']).resolve()
    src = Path(libs[0]['target']['src_path']).resolve()
    version = '0.2.174' if target == 'xtensa-esp32s3-nuttx' else '0.2.175'
    assert libs[0]['package_id'].endswith('libc@' + version), 'review ABI checks for a new libc'
    c_source = ('#include <nuttx/config.h>\n#include <stdint.h>\n#include <stddef.h>\n'
                '#include <time.h>\n#include <sys/types.h>\n#include <poll.h>\n'
                '#include <pthread.h>\n#include <errno.h>\n#include <fcntl.h>\n#include <signal.h>\n'
                '__attribute__((section(".nxrs_abi"), used))\n'
                'const uint32_t nxrs_abi[] = {\n'
                + ''.join(f'  (uint32_t)({c}), /* {name} */\n' for name, c, _, _ in rows) + '};\n')
    rust_source = ('#![no_std]\n#![feature(rustc_private)]\nextern crate libc;\n'
                   'use core::mem::{size_of, size_of_val, align_of, offset_of};\n'
                   '#[used]\n#[no_mangle]\n#[link_section = ".nxrs_abi"]\n'
                   f'pub static NXRS_ABI: [u32; {len(rows)}] = [\n'
                   + ''.join(f'    ({r}) as u32, // {name}\n' for name, _, r, _ in rows) + '];\n')
    (out / 'abi-native.c').write_text(c_source)
    (out / 'abi-rust.rs').write_text(rust_source)
    commands = [
        [prefix + 'gcc', '-c', '-std=c11', *c_flags,
         '-I' + str(out / 'nuttx/include'), str(out / 'abi-native.c'), '-o', str(out / 'abi-native.o')],
        [str(rustc), '--edition=2021', '--target', target_arg, '--crate-type=lib', '--emit=obj',
         '-Cpanic=abort', '--extern', 'core=' + str(cores[0]),
         '--extern', 'compiler_builtins=' + str(builtins[0]),
         '--extern', 'libc=' + str(library), '-Ldependency=' + str(library.parent),
         str(out / 'abi-rust.rs'), '-o', str(out / 'abi-rust.o')]]
    for command in commands:
        subprocess.run(command, check=True)
    values = {}
    for role in ('native', 'rust'):
        command = [prefix + 'objcopy', '--dump-section',
                   f'.nxrs_abi={out}/abi-{role}.bin', str(out / f'abi-{role}.o')]
        commands.append(command)
        subprocess.run(command, check=True)
        raw = (out / f'abi-{role}.bin').read_bytes()
        assert len(raw) == 4 * len(rows), f'wrong witness length: {role}'
        values[role] = dict(zip([r[0] for r in rows], struct.unpack('<' + 'I' * len(rows), raw)))
    fixes = (
        patch.get('mode') == 'nuttx-parker-fcntl-sigign-and-libc-socket-align'
        and patch.get('startup_sig_ign') == 0
        and patch.get('libc_sockaddr_storage_alignment') == 8
    )
    result = compare(values['native'], values['rust'], fixes, poll_fixed)
    assert result['probe_abi_compatible'], f'ABI gate rejected: {result["errors"]}'
    imports = check_imports((out / 'rust-symbols.txt').read_text(), result['poll_abi_compatible'])
    result.update(target=target, rust_imports=imports,
                  libc_package=libs[0]['package_id'], libc_source=str(src),
                  libc_rlib_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                  config_sha256=hashlib.sha256((out / 'resolved.config').read_bytes()).hexdigest(),
                  commands=commands)
    if target_spec is not None:
        result['target_spec_sha256'] = hashlib.sha256(target_spec.read_bytes()).hexdigest()
    (out / 'abi-report.json').write_text(json.dumps(result, indent=2) + '\n')
    poll_status = 'poll layout compatible' if result['poll_abi_compatible'] else 'raw poll remains unsupported'
    print(f'PASS: {len(rows)} ABI witnesses checked for {target}; {poll_status}; raw SIG_IGN remains unsupported')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--check-imports', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--target', choices=TARGETS, default=TARGET)
    parser.add_argument('--target-spec', type=Path)
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.check_imports:
        check_imports(args.check_imports.read_text())
        print('PASS: no unqualified Rust poll/ppoll imports')
    else:
        if args.out is None:
            parser.error('--out is required')
        run(args.out, args.target, args.target_spec)
