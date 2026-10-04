#!/usr/bin/env python3
"""Isolated benchmark builds. Never edits production profiles or upstream loops."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TM = {
    'basic': 'tm_basic_processing_test.c',
    'basic-observable': 'tm_basic_processing_test.c',
    'cooperative': 'tm_cooperative_scheduling_test.c',
    'preemptive': 'tm_preemptive_scheduling_test.c',
    'memory': 'tm_memory_allocation_test.c',
    'message': 'tm_message_processing_test.c',
    'synchronization': 'tm_synchronization_processing_test.c',
}

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def basic_observable_source(text):
    """Minimal diagnostic transform; the pinned upstream file remains untouched."""
    declaration = 'unsigned long   tm_basic_processing_counter;'
    if text.count(declaration) != 1:
        raise ValueError('unexpected Thread-Metric basic counter declaration')
    return text.replace(declaration, 'volatile unsigned long   tm_basic_processing_counter;')

def upstream(destination):
    """Pinned commit AND independently recorded Git blob identities. No edits."""
    pin = json.loads((HERE / 'upstream.json').read_text())
    destination.mkdir(parents=True, exist_ok=True)
    observed = {}
    for name, expected in pin['git_blob_sha1'].items():
        path = destination / name
        if not path.exists():
            url = f"https://raw.githubusercontent.com/{pin['repository']}/{pin['commit']}/{pin['directory']}/{name}"
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read(1024 * 1024)
        else:
            data = path.read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if actual != expected:
            raise ValueError(f'upstream identity mismatch: {name}')
        path.write_bytes(data)
        observed[name] = hashlib.sha256(data).hexdigest()
    (destination / 'sha256.json').write_text(json.dumps(observed, indent=2) + '\n')
    return destination

def run(args, cwd=ROOT, env=None):
    print('+', ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)

def write_image_size(tool_prefix, image, destination):
    output = subprocess.check_output([tool_prefix + 'size', image], text=True)
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines[-1]) < 4:
        raise ValueError('unexpected GNU size output')
    text_size, data_size, bss_size = map(int, lines[-1][:3])
    record = {
        'schema': 1,
        'scope': 'whole-linked-firmware-image',
        'text_bytes': text_size,
        'data_bytes': data_size,
        'bss_bytes': bss_size,
        'flash_like_bytes': text_size + data_size,
        'static_ram_bytes': data_size + bss_size,
        'note': 'Includes OS, benchmark app and linked runtime; matched-config C vs Rust is a whole-image footprint comparison, not a pure language-runtime constant.',
    }
    destination.write_text(json.dumps(record, indent=2) + '\n')
    return record

def config_identity(path):
    # Only app-selection flags differ between independent C and Rust images.
    # No scheduler/timing/TLS/debug/library setting is normalized away.
    ignored = {'CONFIG_EXAMPLES_NXRS_STD_APP', 'CONFIG_EXAMPLES_NXRS_BENCH'}
    lines = []
    for line in Path(path).read_text().splitlines():
        if not line.startswith('CONFIG_') or line.split('=')[0] in ignored:
            continue
        # This generated provenance label is not a kernel configuration change.
        if line.startswith('CONFIG_BASE_DEFCONFIG=') and line.endswith('-dirty"'):
            line = line.removesuffix('-dirty"') + '"'
        lines.append(line)
    return hashlib.sha256(('\n'.join(sorted(lines)) + '\n').encode()).hexdigest()

def archive_submodule(name, destination):
    source = ROOT / 'external' / name
    expected = subprocess.check_output(
        ['git', 'rev-parse', f'HEAD:external/{name}'], cwd=ROOT, text=True).strip()
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if actual != expected:
        raise ValueError(f'unpinned {name}')
    destination.mkdir(parents=True)
    archive = subprocess.Popen(['git', 'archive', expected], cwd=source, stdout=subprocess.PIPE)
    try:
        subprocess.run(['tar', '-x', '-C', str(destination)], stdin=archive.stdout, check=True)
    finally:
        archive.stdout.close()
    if archive.wait() != 0:
        raise RuntimeError('git archive failed')
    return expected

def c_firmware(args, out):
    profile = tomllib.loads((ROOT / f'platform/nuttx/platforms/{args.platform}.toml').read_text())
    if profile['target'] != 'thumbv8m.main-nuttx-eabi':
        raise ValueError('initial standalone C firmware qualification supports the two ARM profiles only')
    revisions = {name: archive_submodule(name, out / directory)
                 for name, directory in [('nuttx', 'nuttx'), ('nuttx-apps', 'apps')]}
    run(['python3', ROOT / 'tools/apply-nuttx-patches.py',
         '--source', out / 'nuttx', '--revision', revisions['nuttx'],
         '--record', out / 'nuttx-patches.json'])
    run(['python3', ROOT / 'tools/apply-nuttx-patches.py',
         '--component', 'nuttx-apps', '--source', out / 'apps',
         '--revision', revisions['nuttx-apps'],
         '--record', out / 'nuttx-apps-patches.json'])
    app = out / 'apps/examples/nxrs_bench'
    app.mkdir()
    if args.suite == 'posix':
        sources, main, command = ['posix.c'], 'c_main.c', 'rt_c'
        for name in sources + [main, 'posix.h']:
            shutil.copy2(HERE / name, app / name)
    elif args.suite == 'footprint':
        sources, main, command = [], 'minimal_c.c', 'rt_minimal_c'
        shutil.copy2(HERE / main, app / main)
    else:
        original = upstream(ROOT / 'target/rtos-bench/upstream')
        for name in ['tm_api.h', 'tm_porting_layer.h']:
            shutil.copy2(original / name, app / name)
        tm_source = TM[args.case]
        tm_variant = 'upstream'
        if args.case == 'basic-observable':
            # Auxiliary diagnostic only. Preserve the pinned source separately and
            # make exactly the reporter-visible counter volatile in the build copy.
            text = (original / tm_source).read_text()
            built_source = 'tm_basic_processing_observable_test.c'
            (app / built_source).write_text(basic_observable_source(text))
            tm_variant = 'basic-counter-volatile'
        else:
            built_source = tm_source
            shutil.copy2(original / tm_source, app / built_source)
        for name in ['tm_port.c', 'tm_entry.c']:
            shutil.copy2(HERE / name, app / name)
        sources, main, command = ['tm_port.c', built_source], 'tm_entry.c', 'tm_bench'
    (app / 'Kconfig').write_text('config EXAMPLES_NXRS_BENCH\n\ttristate "Independent C benchmark"\n\tdefault n\n')
    (app / 'Make.defs').write_text('ifneq ($(CONFIG_EXAMPLES_NXRS_BENCH),)\nCONFIGURED_APPS += $(APPDIR)/examples/nxrs_bench\nendif\n')
    app_stack = 65536 if args.suite == 'footprint' else 16384
    (app / 'Makefile').write_text(
        'include $(APPDIR)/Make.defs\n' +
        f'PROGNAME = {command}\nPRIORITY = 100\nSTACKSIZE = {app_stack}\n' +
        'MODULE = $(CONFIG_EXAMPLES_NXRS_BENCH)\n' +
        f'CSRCS = {" ".join(sources)}\nMAINSRC = {main}\n' +
        f'CFLAGS += -std=c11 -DTM_TEST_DURATION={args.window_seconds}\n' +
        ('CFLAGS += -O2 -fno-lto\n' if args.suite == 'posix' else '') +
        'include $(APPDIR)/Application.mk\n')
    nuttx = out / 'nuttx'
    run(['./tools/configure.sh', '-l', profile['board']], cwd=nuttx)
    if args.matched_config:
        shutil.copy2(args.matched_config, nuttx / '.config')
    def tweak(flag, name, *value):
        run(['kconfig-tweak', flag, 'CONFIG_' + name, *value], cwd=nuttx)
    if not args.matched_config:
        for symbol in profile['kconfig'].get('enable', []): tweak('--enable', symbol)
        for symbol in profile['kconfig'].get('disable', []): tweak('--disable', symbol)
        for assignment in profile['kconfig'].get('set', []):
            symbol, value = assignment.split('=', 1); tweak('--set-val', symbol, value)
        for symbol in ['EXAMPLES_HELLO', 'TESTING_OSTEST', 'TESTING_GETPRIME', 'SMP', 'SCHED_TICKLESS',
                       'DEBUG_FEATURES', 'STACK_COLORATION', 'PRIORITY_INHERITANCE', 'DISABLE_PTHREAD', 'DISABLE_MQUEUE']:
            tweak('--disable', symbol)
        for symbol in ['DEBUG_CUSTOMOPT', 'NDEBUG', 'SYSTEM_TIME64', 'NSH_DISABLEBG']:
            tweak('--enable', symbol)
        tweak('--set-str', 'DEBUG_OPTLEVEL', '-Ofast' if args.profile == 'raw-ofast' else '-O2')
        tweak('--set-val', 'RR_INTERVAL', '0')
        tweak('--set-val', 'USEC_PER_TICK', '1000')
    tweak('--disable', 'EXAMPLES_NXRS_STD_APP')
    tweak('--enable', 'EXAMPLES_NXRS_BENCH')
    run(['make', 'olddefconfig'], cwd=nuttx)
    shutil.copy2(nuttx / '.config', out / 'resolved.config')
    identity = config_identity(out / 'resolved.config')
    if args.matched_config and identity != config_identity(args.matched_config):
        raise ValueError('matched kernel settings changed beyond the two app-selection flags')
    config = (out / 'resolved.config').read_text()
    if 'CONFIG_SMP=y' in config or 'CONFIG_BUILD_FLAT=y' not in config:
        raise ValueError('this benchmark firmware requires a flat, single-core configuration')
    run(['make', '-j4', 'V=1', f'CROSSDEV={profile["crossdev"]}'], cwd=nuttx)
    # A C baseline must contain no Rust runtime, even when matching std's Kconfig.
    symbols = subprocess.check_output([profile['crossdev'] + 'nm', nuttx / 'nuttx'], text=True)
    (out / 'symbols.txt').write_text(symbols)
    if any(marker in symbols for marker in ['rust_eh_personality', 'rust_begin_unwind', '_RNv', '_ZN3std']):
        raise ValueError('unexpected Rust symbols in standalone C baseline')
    image_size = write_image_size(profile['crossdev'], nuttx / 'nuttx', out / 'image-size.json')
    metadata = dict(schema=1, suite=args.suite, case=args.case, platform=args.platform,
                    board=profile['board'], profile=args.profile, runtime_kind='unmeasured',
                    thread_metric_source_variant=(tm_variant if args.suite == 'thread-metric' else None),
                    clock_hz=None, revisions=revisions, window_seconds=args.window_seconds,
                    kernel_config_sha256=identity, resolved_config_sha256=digest(out / 'resolved.config'),
                    image_sha256=digest(nuttx / 'nuttx'), image_size=image_size,
                    report_exact_reproduction=False,
                    source_sha256={p.name: digest(p) for p in app.iterdir() if p.suffix in ['.c', '.h']},
                    compiler=subprocess.check_output([profile['crossdev'] + 'gcc', '--version'], text=True))
    (out / 'build.json').write_text(json.dumps(metadata, indent=2) + '\n')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['fetch', 'c-host', 'c-firmware', 'rust-firmware'])
    parser.add_argument('--platform', choices=['mps2-an521-mock', 'pico2-mock'], default='mps2-an521-mock')
    parser.add_argument('--suite', choices=['posix', 'thread-metric', 'footprint'], default='posix')
    parser.add_argument('--case', choices=list(TM), default='basic')
    parser.add_argument('--profile', choices=['raw-o2', 'raw-ofast', 'matched', 'nxrs'])
    parser.add_argument('--matched-config', type=Path)
    parser.add_argument('--window-seconds', type=int, default=30)
    parser.add_argument('--rust-bin', choices=['rt-bench', 'rt-minimal'], default='rt-bench')
    args = parser.parse_args()
    args.profile = args.profile or ('nxrs' if args.mode == 'rust-firmware' else 'raw-o2')
    if (args.profile == 'nxrs') != (args.mode == 'rust-firmware'):
        parser.error('rust-firmware uses the unchanged nxrs profile; raw/matched profiles are for C')
    if args.mode != 'rust-firmware' and args.rust_bin != 'rt-bench':
        parser.error('--rust-bin is only valid for rust-firmware')
    if args.window_seconds not in [1, 30]: parser.error('only 1-second smoke or 30-second reference windows')
    if args.mode == 'fetch': upstream(ROOT / 'target/rtos-bench/upstream'); return
    if args.mode == 'rust-firmware' and args.rust_bin == 'rt-minimal':
        out = ROOT / 'target/rtos-bench' / args.mode / args.platform / 'footprint' / 'minimal' / args.profile
    else:
        out = ROOT / 'target/rtos-bench' / args.mode / args.platform / args.suite / args.case / args.profile
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    if args.mode == 'c-host':
        command = ['gcc', '-std=c11', '-O2', '-fno-lto', '-Wall', '-Wextra', '-Werror', '-pthread',
                   HERE / 'posix.c', HERE / 'c_main.c', '-lrt', '-o', out / 'rt_c']
        run(command)
        (out / 'build.json').write_text(json.dumps(dict(command=list(map(str, command)), runtime_kind='host',
            compiler=subprocess.check_output(['gcc', '--version'], text=True), image_sha256=digest(out / 'rt_c')), indent=2) + '\n')
    elif args.mode == 'c-firmware':
        if (args.profile == 'matched') != bool(args.matched_config):
            parser.error('matched profile requires --matched-config; other profiles forbid it')
        c_firmware(args, out)
    else:
        env = dict(os.environ, CARGO_PROFILE_RELEASE_OPT_LEVEL='2')
        rust_bin = args.rust_bin
        rust_command = 'rt_minimal' if rust_bin == 'rt-minimal' else 'rt_bench'
        abi_profile = 'minimal' if rust_bin == 'rt-minimal' else 'active'
        run(['bash', ROOT / 'tools/build-nuttx-std-app.sh', '--app-manifest', HERE / 'rust/Cargo.toml',
             '--app-package', 'nxrs-rt-bench', '--bin', rust_bin, '--command', rust_command,
             '--priority', '100', '--stack-size', '65536', '--platform', args.platform, '--out', out,
             '--abi-profile', abi_profile], env=env)
        profile = tomllib.loads((ROOT / f'platform/nuttx/platforms/{args.platform}.toml').read_text())
        image = out / 'nuttx/nuttx'
        write_image_size(profile['crossdev'], image, out / 'image-size.json')
    print('BENCH_ARTIFACT_DIRECTORY=' + str(out))
if __name__ == '__main__': main()
