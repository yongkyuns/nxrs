#!/usr/bin/env python3
"""Prepare a fresh, shell-free NuttX benchmark tree; never edit dependencies."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = load('minimal_nuttx_config', HERE.parent / 'rtos-bench/build.py')


def values(path):
    config = {}
    for line in Path(path).read_text().splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            name, value = line.split('=', 1)
            config[name] = value
        elif line.startswith('# CONFIG_') and line.endswith(' is not set'):
            config[line[2:-11]] = 'n'
    return config


def validate(config, baseline):
    """Keep the timing, ABI, assertions and resource reservations unchanged."""
    current, original = values(config), values(baseline)
    protected = (
        'BUILD_FLAT', 'SMP', 'USEC_PER_TICK', 'RR_INTERVAL',
        'SYSTEM_TIME64', 'FS_LARGEFILE', 'TLS_GLOBAL_KEYS', 'TLS_NELEM',
        'TLS_DTOR_ITERATIONS', 'TLS_NCLEANUP', 'PTHREAD_STACK_DEFAULT',
        'ARCH_INTERRUPTSTACK', 'IDLETHREAD_STACKSIZE', 'MQ_MAXMSGSIZE',
        'PREALLOC_MQ_MSGS', 'PREALLOC_MQ_IRQ_MSGS', 'STACK_COLORATION',
        'DEBUG_FEATURES', 'DEBUG_ASSERTIONS', 'DEBUG_ASSERTIONS_FILENAME',
        'ARCH_STACKDUMP', 'NDEBUG', 'USERLED_LOWER_READSTATE',
        'ESP32S3_DEFAULT_CPU_FREQ_MHZ', 'ESPRESSIF_FLASH_MODE_DIO',
        'ESPRESSIF_FLASH_FREQ_40M', 'ESP32S3_INSTRUCTION_CACHE_SIZE',
        'ESP32S3_DATA_CACHE_SIZE', 'ESP32S3_USBSERIAL',
        'ESP32S3_SPI_FLASH_DONT_USE_ROM_CODE', 'ESP32S3_SPI_FLASH_USE_32BIT_ADDRESS',
    )
    for name in protected:
        symbol = 'CONFIG_' + name
        if current.get(symbol) != original.get(symbol):
            raise ValueError(f'minimal profile changed a matched setting: {symbol}')
    overlay = values(HERE / 'nuttx-minimal.conf')
    for symbol, value in overlay.items():
        # Disabled dependent symbols may disappear entirely after olddefconfig.
        if current.get(symbol, 'n') != value:
            raise ValueError(f'minimal profile did not resolve: {symbol}={value}')
    for name in ('DISABLE_PTHREAD', 'DISABLE_MQUEUE', 'DISABLE_POLL'):
        if current.get('CONFIG_' + name, 'n') != 'n':
            raise ValueError(f'minimal profile removed required POSIX support: {name}')


def prepare(baseline, hal_cache, out, target_spec=None):
    baseline, hal_cache, out = map(Path, (baseline, hal_cache, out))
    if out.exists() or not baseline.is_file() or not (hal_cache / 'components').is_dir():
        raise ValueError('fresh output, resolved baseline and Espressif HAL cache required')
    out.mkdir(parents=True)
    revisions = {name: helpers.archive_submodule(name, out / directory)
                 for name, directory in (('nuttx', 'nuttx'), ('nuttx-apps', 'apps'))}
    for component, directory in (('nuttx', 'nuttx'), ('nuttx-apps', 'apps')):
        helpers.run([sys.executable, ROOT / 'tools/apply-nuttx-patches.py',
                     '--component', component, '--source', out / directory,
                     '--revision', revisions[component],
                     '--record', out / (component + '-patches.json')])
    hal_revision = subprocess.check_output(
        ['git', '-C', hal_cache, 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', hal_cache, 'status', '--porcelain',
                                '--untracked-files=no', '--ignore-submodules=all'], text=True).strip():
        raise ValueError('Espressif HAL cache has modified tracked sources')
    destination = out / 'nuttx/arch/xtensa/src/esp32s3/esp-hal-3rdparty'
    # Fresh local clones preserve pins, not cached source modifications. NuttX
    # applies its own bundled mbedTLS patches in this disposable build copy.
    helpers.run(['git', 'clone', '--quiet', '--shared', hal_cache, destination])
    submodule = 'components/mbedtls/mbedtls'
    expected = subprocess.check_output(['git', '-C', hal_cache, 'rev-parse',
                                       'HEAD:' + submodule], text=True).strip()
    actual = subprocess.check_output(['git', '-C', hal_cache / submodule,
                                     'rev-parse', 'HEAD'], text=True).strip()
    if actual != expected:
        raise ValueError('cached mbedTLS revision differs from the HAL gitlink')
    helpers.run(['git', 'clone', '--quiet', '--shared', hal_cache / submodule,
                 destination / submodule])
    shutil.copytree(ROOT / 'platform/nuttx/std-app', out / 'apps/examples/nxrs_std_app')
    app = out / 'apps/examples/nxrs_bench'
    app.mkdir()
    (app / 'Kconfig').write_text('config EXAMPLES_NXRS_BENCH\n\tbool "C event-service control"\n\tdefault n\n')
    (app / 'Make.defs').write_text('ifneq ($(CONFIG_EXAMPLES_NXRS_BENCH),)\nCONFIGURED_APPS += $(APPDIR)/examples/nxrs_bench\nendif\n')
    (app / 'Makefile').write_text('include $(APPDIR)/Make.defs\ninclude $(APPDIR)/Application.mk\n')
    nuttx = out / 'nuttx'
    helpers.run(['./tools/configure.sh', '-l', 'esp32s3-devkit:nsh'], cwd=nuttx)
    config = values(baseline)
    config.update(values(HERE / 'nuttx-minimal.conf'))
    config['CONFIG_EXAMPLES_NXRS_BENCH'] = 'y'
    config['CONFIG_EXAMPLES_NXRS_STD_APP'] = 'n'
    (nuttx / '.config').write_text('\n'.join(f'{name}={value}' for name, value in sorted(config.items())) + '\n')
    helpers.run(['bash', out / 'apps/tools/mkkconfig.sh', '-m', 'Examples'], cwd=app.parent)
    helpers.run(['make', 'olddefconfig'], cwd=nuttx)
    validate(nuttx / '.config', baseline)
    shutil.copy2(nuttx / '.config', out / 'baseline.config')
    if target_spec:
        shutil.copy2(target_spec, out / 'xtensa-esp32s3-nuttx.json')
    record = dict(schema=1, profile='minimal', revisions=revisions,
                  hal_revision=hal_revision,
                  baseline_config_sha256=helpers.digest(baseline),
                  overlay_sha256=helpers.digest(HERE / 'nuttx-minimal.conf'),
                  resolved_config_sha256=helpers.digest(out / 'baseline.config'),
                  kernel_config_identity=helpers.config_identity(out / 'baseline.config'))
    (out / 'profile.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def verify_rust_input(bundle, tree):
    """Reuse an immutable partial link, not a compiler cache guessed by path."""
    proof = json.loads((bundle / 'compiler-input.json').read_text())
    pins = json.loads((ROOT / 'upstream/rust-llvm/upstream.json').read_text())
    if proof['rust_revision'] != pins['rust_revision'] or proof['llvm_revision'] != pins['revision']:
        raise ValueError('frozen compiler source pins differ')
    if helpers.digest(bundle / 'rust-input.elf') != proof['elf_sha256']:
        raise ValueError('frozen Rust input hash differs')
    if helpers.digest(tree / 'xtensa-esp32s3-nuttx.json') != proof['target_spec_sha256']:
        raise ValueError('frozen Rust target specification differs')
    if helpers.digest(ROOT / 'tests/nuttx-std/link.py') != proof['link_wrapper_sha256']:
        raise ValueError('frozen Rust partial-link wrapper differs')
    # Host scripts and the C kernel may change. Every Rust firmware input
    # recorded by the original build must still match, without a blanket waiver.
    rust_inputs = {name: digest for name, digest in proof['source_sha256'].items()
                   if Path(name).suffix in ('.rs', '.toml') or name.endswith('Cargo.lock')}
    if not {'core.rs', 'nuttx.rs', 'controls.rs'}.issubset(rust_inputs):
        raise ValueError('frozen Rust input lacks firmware source identities')
    for name, expected in rust_inputs.items():
        if helpers.digest(HERE / name) != expected:
            raise ValueError(f'frozen Rust firmware input changed: {name}')
    if proof['app_opt_level'] != '2' or proof['std_build_features'] != [
            'backtrace-trace-only', 'optimize_for_size', 'panic_immediate_abort']:
        raise ValueError('frozen Rust optimization policy differs')
    header = subprocess.check_output(['xtensa-esp32s3-elf-readelf', '-h',
                                     bundle / 'rust-input.elf'], text=True)
    if 'REL (Relocatable file)' not in header or 'Xtensa' not in header:
        raise ValueError('frozen input is not a relocatable Xtensa ELF')
    return proof


def link_rust_input(bundle, tree, sources, headers, definitions, stage, env,
                    relink, *, size_kernel):
    bundle, tree = Path(bundle).resolve(), Path(tree).resolve()
    proof = verify_rust_input(bundle, tree)
    stage.mkdir()
    lock = relink.lock_build_tree(tree)
    try:
        config_hash = helpers.digest(tree / 'nuttx/.config')
        app = tree / 'apps/examples/nxrs_std_app'
        for header in headers:
            shutil.copy2(header, app / header.name)
        base = ROOT / 'tests/service-footprint/esp32s3_cycles.c'
        shutil.copy2(base, app / base.name)
        names = [base.name]
        for index, source in enumerate(sources):
            name = f'nxrs_target_helper_{index}.c'
            shutil.copy2(source, app / name)
            names.append(name)
        partial = stage / 'rust-input.elf'
        shutil.copy2(bundle / partial.name, partial)
        flags = ' '.join('-D' + value for value in [*definitions, 'ES_RUST=1'])
        if size_kernel:
            flags += ' -O2'
        make = ['make', '-j2', 'CROSSDEV=xtensa-esp32s3-elf-',
                f'NXRS_STD_ELF={partial}', 'NXRS_APP_COMMAND=es_rust',
                'NXRS_APP_PRIORITY=100', 'NXRS_APP_STACKSIZE=8192',
                'NXRS_TARGET_C_SOURCE=' + ' '.join(names),
                'NXRS_TARGET_C_FLAGS=' + flags, 'ESPTOOL_BINDIR=.']
        with (stage / 'make.log').open('w') as log:
            relink.refresh_registration(tree, make[2:], env, log)
            subprocess.run(make, cwd=tree / 'nuttx', env=env, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        if helpers.digest(tree / 'nuttx/.config') != config_hash:
            raise ValueError('kernel configuration changed during frozen relink')
        for source, name in {'nuttx':'rust.elf', 'nuttx.bin':'rust.unpadded.bin',
                             'nuttx.merged.bin':'rust.merged.bin',
                             '.config':'resolved.config', 'nuttx.map':'rust-final.map'}.items():
            shutil.copy2(tree / 'nuttx' / source, stage / name)
        provenance = dict(kind='verified frozen Rust input',
                          input_elf_sha256=proof['elf_sha256'],
                          compiler_manifest_sha256=helpers.digest(bundle / 'compiler-input.json'),
                          rust_revision=proof['rust_revision'], llvm_revision=proof['llvm_revision'],
                          compiler_sha256=proof.get('compiler_sha256'),
                          compiler_patch_ledger_sha256=proof.get('compiler_patch_ledger_sha256'),
                          compiler_patches=[{'name':p['name'], 'sha256':p['sha256']}
                                            for p in proof.get('compiler_patches', {}).get('patches', [])],
                          config_sha256=config_hash, make_command=make,
                          helper_sha256={name:helpers.digest(app / name) for name in names})
        (stage / 'relink-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-config', required=True, type=Path)
    parser.add_argument('--hal-cache', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--target-spec', type=Path, help='prepared nxrs Rust target specification')
    args = parser.parse_args()
    prepare(args.baseline_config.resolve(), args.hal_cache.resolve(), args.out.resolve(), args.target_spec)


if __name__ == '__main__':
    main()
