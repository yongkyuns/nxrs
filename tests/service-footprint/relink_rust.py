#!/usr/bin/env python3
"""Relink one footprint demo against an already-built NuttX tree.

Serial use only: this mutates the prepared tree, but saves each final artifact
in a new directory. Kernel config, C helpers, std sysroot and profile are fixed.
It never flashes a device and is not a replacement for the deployment builder.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lock_build_tree(tree):
    """Refuse concurrent mutation before staging helpers or changing config.

    Retain the returned handle for the caller's entire build. The lock file
    contains no payload and may remain; ownership is the open file lock.
    """
    handle = (tree / '.qualification-build.lock').open('a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(f'qualification build tree is already in use: {tree}') from None
    return handle


def optimization_override(level):
    value = level if level.isdecimal() else json.dumps(level)
    return f'profile.release.package.nxrs-footprint-demo.opt-level={value}'


def selected_helpers(app, invocation_names):
    """Select the cycle helper and only helpers staged for this invocation."""
    names = ['esp32s3_cycles.c', *invocation_names]
    if any(not (app / name).is_file() for name in names):
        raise ValueError('prepared base or requested C helper is missing')
    return names


def freeze_partial(messages, binary_name, partial):
    """Save a verified Cargo cache hit, without assuming an executable path."""
    if partial.is_file():
        return False
    binaries = [m for m in messages if m.get('reason') == 'compiler-artifact'
                and m['target']['name'] == binary_name and m.get('executable')]
    if len(binaries) != 1:
        raise ValueError('Cargo did not identify exactly one diagnostic executable')
    executable = Path(binaries[0]['executable']).resolve()
    header = subprocess.check_output(['xtensa-esp32s3-elf-readelf', '-h', str(executable)], text=True)
    if not re.search(r'Type:\s+REL\b', header) or 'Xtensa' not in header:
        raise ValueError('cached Cargo artifact is not a relocatable Xtensa input')
    shutil.copy2(executable, partial)
    return True


def refresh_registration(tree, make_variables, env, log):
    """Regenerate builtins when switching diagnostic command names.

    Incremental application links do not remove a previous PROGNAME's registry
    entry. Use NuttX's own generated-file targets, then register every selected
    application again. Kernel sources and configuration are left untouched.
    """
    common = ['make', f'TOPDIR={tree / "nuttx"}',
              f'APPDIR={tree / "apps"}', *make_variables]
    subprocess.run([*common, '-C', str(tree / 'apps/builtin'),
                    'clean_context'], env=env, stdout=log,
                   stderr=subprocess.STDOUT, check=True)
    # Application.mk's clean removes the shared libapps.a, so clean *all*
    # selected app .built stamps too; otherwise the archive can contain only
    # rebuilt builtins and lose NSH/the diagnostic entry.
    subprocess.run([*common, '-C', str(tree / 'apps'), 'clean'],
                   env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    subprocess.run([*common, '-C', str(tree / 'apps'), 'context'],
                   env=env, stdout=log, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree', required=True, type=Path)
    parser.add_argument('--sysroot', required=True, type=Path)
    parser.add_argument('--cargo-target', type=Path,
                        default=Path(__file__).resolve().parents[2] / 'target/service-footprint/cargo')
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--feature', action='append', default=[])
    parser.add_argument('--target-c-source', action='append', default=[], type=Path)
    parser.add_argument('--target-c-header', action='append', default=[], type=Path)
    parser.add_argument('--app-opt-level', choices=('z', 's', '2', '3'), default='z',
                        help='application-only optimization; pinned std remains size-built')
    parser.add_argument('--c-opt-level', choices=('s', '2'),
                        help='application-helper override; leave kernel optimization unchanged')
    parser.add_argument('--c-define', action='append', default=[],
                        help='safe NAME or NAME=decimal app-helper preprocessor define')
    parser.add_argument('--bin', default='cq-scale',
                        choices=('cq-scale', 'cq-payload-processing', 'event-services'))
    parser.add_argument('--command', default='cq_scale')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9_]*', args.command):
        parser.error('invalid NuttX command name')
    for definition in args.c_define:
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*(?:=[0-9]+)?', definition):
            parser.error(f'invalid C definition: {definition}')
    root = Path(__file__).resolve().parents[2]
    tree, sysroot, target, out = (p.resolve() for p in
                                (args.tree, args.sysroot, args.cargo_target, args.out))
    build_lock = lock_build_tree(tree)
    # Never overwrite prior evidence or clean/delete a user tree.
    out.mkdir(parents=True, exist_ok=False)
    config = tree / 'nuttx/.config'
    config_text = config.read_text()
    if 'CONFIG_EXAMPLES_NXRS_STD_APP=y\n' not in config_text or \
            'CONFIG_EXAMPLES_NXRS_BENCH=y\n' in config_text:
        parser.error('prepared tree must select the Rust app exclusively')
    before = digest(config)
    app = tree / 'apps/examples/nxrs_std_app'
    for header in args.target_c_header:
        if header.suffix != '.h' or not header.is_file():
            parser.error(f'missing C header: {header}')
        shutil.copy2(header.resolve(), app / header.name)
    base_source = Path(__file__).resolve().parent / 'esp32s3_cycles.c'
    if not base_source.is_file():
        parser.error(f'missing base C helper: {base_source}')
    shutil.copy2(base_source, app / base_source.name)
    staged_names = []
    for index, source in enumerate(args.target_c_source):
        name = 'nxrs_target_helper.c' if index == 0 else f'nxrs_target_helper_{index}.c'
        if not source.is_file() or source.suffix != '.c':
            parser.error(f'missing C helper: {source}')
        shutil.copy2(source.resolve(), app / name)
        staged_names.append(name)
    helpers = selected_helpers(app, staged_names)
    env = dict(os.environ)
    env.update(RUSTUP_TOOLCHAIN='1.90.0', RUSTC_BOOTSTRAP='1',
               RUSTC=str(sysroot / 'bin/rustc'),
               RUSTDOC=str(sysroot / 'bin/rustdoc'),
               NUTTX_STD_SYSROOT=str(sysroot), CARGO_TARGET_DIR=str(target),
               NUTTX_STD_GNU_LINKER=shutil.which('xtensa-esp32s3-elf-ld'),
               NUTTX_STD_LINK_LOG=str(out / 'rust-link.json'),
               CARGO_PROFILE_RELEASE_LTO='fat', CARGO_PROFILE_RELEASE_DEBUG='0',
               CARGO_PROFILE_RELEASE_STRIP='none',
               RUSTFLAGS=f'-C panic=abort -C linker={root}/tests/nuttx-std/link.py')
    if env['NUTTX_STD_GNU_LINKER'] is None:
        raise SystemExit('source the pinned ESP environment first')
    cargo = subprocess.check_output(['rustup', 'which', '--toolchain', '1.90.0', 'cargo'],
                                    text=True).strip()
    features = (['mq-backend'] if args.bin == 'cq-scale' else []) + args.feature
    command = [cargo, '--config', 'build.jobs=2', '--config',
               optimization_override(args.app_opt_level),
               'build', '--locked', '--release',
               '-p', 'nxrs-footprint-demo',
               '--features', ','.join(features), '--bin', args.bin,
               '--target', str(tree / 'xtensa-esp32s3-nuttx.json'),
               '-Zbuild-std=std,panic_abort',
               '-Zbuild-std-features=backtrace-trace-only,optimize_for_size,panic_immediate_abort',
               '--message-format=json-render-diagnostics']
    print('Cargo:', ','.join(features), flush=True)
    with (out / 'cargo-messages.jsonl').open('w') as log:
        subprocess.run(command, cwd=root, env=env, stdout=log, check=True)
    messages = [json.loads(line) for line in (out / 'cargo-messages.jsonl').read_text().splitlines()]
    std = [message for message in messages if message.get('reason') == 'compiler-artifact'
           and message['target']['name'] == 'std'
           and 'rlib' in message['target']['crate_types']]
    assert len(std) == 1, 'expected exactly one built std artifact'
    actual = Path(std[0]['target']['src_path']).resolve()
    expected = sysroot / 'lib/rustlib/src/rust/library/std/src/lib.rs'
    assert actual == expected.resolve(), 'Cargo selected a different std source'
    std_proof = dict(std_source=str(actual), selected_source_matches=True)
    (out / 'std-source.json').write_text(json.dumps(std_proof, indent=2) + '\n')
    # Freeze the executable identified by this successful Cargo build, even on
    # a cache hit. Validate that it is our relocatable partial link, not a host
    # or final executable. Never infer a cached path from a previous experiment.
    partial = out / 'rust-input.elf'
    cached = freeze_partial(messages, args.bin, partial)
    binaries = [m for m in messages if m.get('reason') == 'compiler-artifact'
                and m['target']['name'] == args.bin and m.get('executable')]
    assert len(binaries) == 1 and binaries[0]['profile']['opt_level'] == args.app_opt_level, \
        'Cargo selected a different application optimization level'
    print('Relinking final NuttX image', flush=True)
    make = ['make', '-j2', 'CROSSDEV=xtensa-esp32s3-elf-',
            f'NXRS_STD_ELF={partial}', f'NXRS_APP_COMMAND={args.command}',
            'NXRS_APP_PRIORITY=100', 'NXRS_APP_STACKSIZE=8192',
            'NXRS_TARGET_C_SOURCE=' + ' '.join(helpers),
            'NXRS_TARGET_C_FLAGS=' + ' '.join('-D' + definition for definition in args.c_define)
            + (f' -O{args.c_opt_level}' if args.c_opt_level else ''),
            'ESPTOOL_BINDIR=.']
    with (out / 'make.log').open('w') as log:
        refresh_registration(tree, make[2:], env, log)
        subprocess.run(make, cwd=tree / 'nuttx', env=env, stdout=log,
                       stderr=subprocess.STDOUT, check=True)
    assert digest(config) == before, 'NuttX config changed during relink'
    artifacts = {'nuttx': 'rust.elf', 'nuttx.map': 'rust-final.map',
                 'System.map': 'rust-system.map', 'nuttx.bin': 'rust.unpadded.bin',
                 'nuttx.merged.bin': 'rust.merged.bin', '.config': 'resolved.config'}
    for source, destination in artifacts.items():
        shutil.copy2(tree / 'nuttx' / source, out / destination)
    manifest = dict(tree=str(tree), sysroot=str(sysroot), features=features,
                    app_opt_level=args.app_opt_level, cargo_artifact_cached=cached,
                    rust_source_sha256=digest(Path(__file__).resolve().parent / 'src' /
                                             {'cq-scale': 'scale.rs',
                                              'cq-payload-processing': 'payload_processing.rs',
                                              'event-services': '../../event-services-comparison/nuttx.rs'}[args.bin]),
                    rust_module_sha256={p.name: digest(p) for p in
                                        (Path(__file__).resolve().parent / 'src').glob('*.rs')},
                    config_sha256=before, cargo_command=command, make_command=make,
                    helper_sha256={name: digest(app / name) for name in helpers},
                    header_sha256={header.name: digest(app / header.name)
                                   for header in args.target_c_header},
                    std_source=std_proof,
                    artifacts={name: digest(out / name) for name in artifacts.values()})
    (out / 'relink-provenance.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Saved:', out, flush=True)


if __name__ == '__main__':
    main()
