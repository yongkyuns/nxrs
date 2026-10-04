#!/usr/bin/env python3
"""Build the C side of the matched ESP32-S3 service footprint demo."""
import argparse
import hashlib
import json
import importlib.util
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from relink_rust import lock_build_tree, refresh_registration


ROOT = Path(__file__).resolve().parents[2]


def validate_c_symbols(symbols, command):
    entry = re.compile(r' [TtWw] ' + re.escape(command) + r'_main$', re.MULTILINE)
    rust = re.compile(r'(?:cq_scale|core|alloc|std)::|(?:^|\s)(?:__rust_|__rdl_|_RN)', re.MULTILINE)
    if not entry.search(symbols) or rust.search(symbols):
        raise ValueError('C control link is missing its entry or retains Rust code')


def run(args, cwd, log=None):
    args = list(map(str, args))
    if log is None:
        subprocess.run(args, cwd=cwd, check=True)
        return
    with Path(log).open('w') as output:
        subprocess.run(args, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nuttx', required=True, type=Path, help='existing configured and built NuttX tree')
    parser.add_argument('--apps', required=True, type=Path, help='matching NuttX apps tree')
    parser.add_argument('--baseline-config', required=True, type=Path,
                        help='resolved .config from the matched Rust baseline build')
    parser.add_argument('--source', type=Path,
                        default=Path(__file__).resolve().parent / 'channel_scale_mq.c')
    parser.add_argument('--platform', default='esp32s3-service-footprint')
    parser.add_argument('--command', default='cq_c_scale')
    parser.add_argument('--stack-size', type=int, default=8192)
    parser.add_argument('--target-c-source', type=Path, action='append', default=[])
    parser.add_argument('--target-c-header', type=Path, action='append', default=[])
    parser.add_argument('--c-define', action='append', default=[],
                        help='safe NAME or NAME=decimal C preprocessor define')
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--reuse-kernel', action='store_true',
                        help='reuse a built, config-matched kernel; still clean all app archives')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9_]*', args.command):
        parser.error('invalid NuttX command name')
    if args.stack_size < 2048:
        parser.error('NuttX command stack must be at least 2048 bytes')
    nuttx, apps = args.nuttx.resolve(), args.apps.resolve()
    build_lock = lock_build_tree(nuttx.parent)
    rust_config, out = args.baseline_config.resolve(), args.out.resolve()
    if out.exists():
        parser.error(f'output already exists: {out}')
    if not (nuttx / '.config').is_file() or not apps.is_dir() or not rust_config.is_file():
        parser.error('NuttX, apps, or Rust resolved config path is missing')
    source = args.source.resolve()
    if not source.is_file() or source.suffix != '.c':
        parser.error(f'missing C source: {source}')
    profile_path = ROOT / 'platform/nuttx/platforms' / f'{args.platform}.toml'
    if not profile_path.is_file():
        parser.error(f'missing platform profile: {profile_path}')
    target_c_sources = [source.resolve() for source in args.target_c_source]
    for target_c_source in target_c_sources:
        if not target_c_source.is_file() or target_c_source.suffix != '.c':
            parser.error(f'missing target C source: {target_c_source}')
    for header in args.target_c_header:
        if not header.is_file() or header.suffix != '.h':
            parser.error(f'missing C header: {header}')
    for definition in args.c_define:
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*(?:=[0-9]+)?', definition):
            parser.error(f'invalid C definition: {definition}')
    helper_path = ROOT / 'tests/rtos-bench/build.py'
    spec = importlib.util.spec_from_file_location('nxrs_rtos_build', helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    # Capture this before changing the tree, including when baseline-config names
    # its current .config. Only the selected app may differ afterward.
    rust_identity = helper.config_identity(rust_config)
    if args.reuse_kernel and (nuttx.name != 'nuttx' or apps != nuttx.parent / 'apps'
                              or not (nuttx / 'nuttx').is_file()
                              or helper.config_identity(nuttx / '.config') != rust_identity):
        parser.error('--reuse-kernel requires an existing config-matched kernel build')
    app = apps / 'examples/nxrs_bench'
    app.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, app / source.name)
    for header in args.target_c_header:
        shutil.copy2(header.resolve(), app / header.name)
    target_helpers = []
    for index, target_c_source in enumerate(target_c_sources):
        name = 'nxrs_target_helper.c' if index == 0 else f'nxrs_target_helper_{index}.c'
        shutil.copy2(target_c_source, app / name)
        target_helpers.append(name)
    (app / 'Kconfig').write_text(
        'config EXAMPLES_NXRS_BENCH\n'
        '\ttristate "C footprint control"\n'
        '\tdefault n\n')
    (app / 'Make.defs').write_text(
        'ifneq ($(CONFIG_EXAMPLES_NXRS_BENCH),)\n'
        'CONFIGURED_APPS += $(APPDIR)/examples/nxrs_bench\n'
        'endif\n')
    (app / 'Makefile').write_text(
        'include $(APPDIR)/Make.defs\n'
        f'PROGNAME = {args.command}\nPRIORITY = 100\nSTACKSIZE = {args.stack_size}\n'
        'MODULE = $(CONFIG_EXAMPLES_NXRS_BENCH)\n'
        f'MAINSRC = {source.name}\n'
        + (f'CSRCS += {" ".join(target_helpers)}\n' if target_helpers else '')
        + 'CFLAGS += -std=c11 -DTM_TEST_DURATION=1'
        + ''.join(f' -D{definition}' for definition in args.c_define)
        + '\n'
        'include $(APPDIR)/Application.mk\n')
    # apps/examples/Kconfig is generated from discovered example directories.
    # Without regenerating it, olddefconfig silently drops the new C option
    # and can produce a misleadingly tiny image with no control app linked.
    current_config = (nuttx / '.config').read_text()
    if 'CONFIG_EXAMPLES_NXRS_BENCH=y\n' not in current_config or \
            'CONFIG_EXAMPLES_NXRS_STD_APP=y\n' in current_config:
        run(['bash', apps / 'tools/mkkconfig.sh', '-m', 'Examples'], apps / 'examples')
        run(['kconfig-tweak', '--disable', 'CONFIG_EXAMPLES_NXRS_STD_APP'], nuttx)
        run(['kconfig-tweak', '--enable', 'CONFIG_EXAMPLES_NXRS_BENCH'], nuttx)
        run(['make', 'olddefconfig'], nuttx)

    config = (nuttx / '.config').read_text()
    if 'CONFIG_EXAMPLES_NXRS_BENCH=y\n' not in config or 'CONFIG_EXAMPLES_NXRS_STD_APP=y\n' in config:
        parser.error('C control was not selected exclusively after olddefconfig')
    c_identity = helper.config_identity(nuttx / '.config')
    if rust_identity != c_identity:
        parser.error('C config differs from the paired Rust build beyond app-selection flags')

    out.mkdir(parents=True)
    shutil.copy2(nuttx / '.config', out / 'resolved.config')
    shutil.copy2(source, out / source.name)
    profile = tomllib.loads(profile_path.read_text())
    # NuttX application archives can retain an object from the previously
    # selected Rust app even after Kconfig switches it off. Rebuild archives
    # from the selected C sources rather than linking a mixed stale archive.
    if args.reuse_kernel:
        with (out / 'clean.log').open('w') as log:
            refresh_registration(nuttx.parent,
                                 [f'CROSSDEV={profile["crossdev"]}', 'ESPTOOL_BINDIR=.'],
                                 dict(os.environ), log)
    else:
        run(['make', 'clean'], nuttx, out / 'clean.log')
    make_args = ['make', '-j2', 'V=1', f'CROSSDEV={profile["crossdev"]}', 'ESPTOOL_BINDIR=.']
    run(make_args, nuttx, out / 'build.log')
    tool = profile['crossdev']
    elf = nuttx / 'nuttx'
    symbols = subprocess.check_output([tool + 'nm', '-C', elf], text=True)
    validate_c_symbols(symbols, args.command)
    (out / 'symbols.txt').write_text(symbols)
    (out / 'size.txt').write_text(subprocess.check_output([tool + 'size', elf], text=True))
    (out / 'sections.txt').write_text(subprocess.check_output([tool + 'size', '-A', elf], text=True))
    (out / 'layout.txt').write_text(subprocess.check_output(
        [tool + 'readelf', '-W', '-h', '-l', '-S', elf], text=True))
    (out / 'config-identity.txt').write_text(c_identity + '\n')
    # Preserve the measured inputs before the serial-use tree is changed by
    # another control build. Diagnostics must never point at a mutable ELF.
    for source_name, saved_name in {
        'nuttx': 'c.elf', 'nuttx.map': 'c-final.map',
        'System.map': 'c-system.map', 'nuttx.bin': 'c.unpadded.bin',
        'nuttx.merged.bin': 'c.merged.bin',
    }.items():
        shutil.copy2(nuttx / source_name, out / saved_name)
    provenance = dict(config_identity=c_identity, c_defines=args.c_define,
                      command=args.command, command_stack=args.stack_size,
                      make_command=make_args,
                      source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in [source, *target_c_sources, *args.target_c_header]},
                      artifacts={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in out.iterdir() if p.suffix in ('.elf', '.bin', '.config')})
    (out / 'c-build-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    print(f'C_FOOTPRINT_CONFIG_MATCH={c_identity}')
    print(f'C_FOOTPRINT_ELF={elf}')
    print(f'C_FOOTPRINT_IMAGE={nuttx / profile["image"]}')


if __name__ == '__main__':
    main()
