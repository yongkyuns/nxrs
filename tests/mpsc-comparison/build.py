"""Board wiring around the normal NuttX frontend and pinned Zephyr CMake build."""
import hashlib
import importlib.util
import json
import os
from collections import Counter
from pathlib import Path
import re
import secrets
import shlex
import struct
import subprocess
import sys
import tempfile
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
from rtos_harness.zephyr import ZEPHYR_SHA, check_checkout, resolved_config

BOARDS = tomllib.loads((HERE / 'boards.toml').read_text())
BUILD_SCHEMA = 'mpsc-comparison-build-v3'
IDENTITY_FIELDS = ('schema', 'board', 'runtime', 'backend', 'source_sha256', 'board_contract', 'native_build_inputs', 'nonce')
NATIVE_QUEUE_ROOTS = ('mp_queue_create', 'mp_queue_send', 'mp_queue_receive', 'mp_queue_destroy', 'mp_queue_waiting')


def selected_backend(runtime, backend=None):
    backend = backend or ('std' if runtime == 'nuttx' else 'native')
    if (runtime, backend) not in {('nuttx', 'std'), ('nuttx', 'native'), ('zephyr', 'native')}:
        raise ValueError('unsupported runtime/backend')
    return backend


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def configure_native_inputs(env, out):
    """Make compiled timestamps/assertion paths repeatable before compilation.

    These are real allocated bytes. Do not erase them from the kernel identity.
    Keep inherited compiler flags intact and use the normal frontend's recorded
    EXTRAFLAGS/SOURCE_DATE_EPOCH inputs. Only the owned output path is remapped.
    """
    owned = str(out.resolve())
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', owned):
        raise ValueError('MPSC output path must not contain whitespace or shell/Make metacharacters')
    inherited = env.get('EXTRAFLAGS', '')
    if any(c in inherited for c in '\x00\r\n$`;|&<>'):
        raise ValueError('EXTRAFLAGS contains unsupported shell/Make syntax; flags were not overwritten')
    try:
        words = shlex.split(inherited)
        if words != shlex.split(inherited, comments=True):
            raise ValueError('unquoted comment')
    except ValueError as error:
        raise ValueError('EXTRAFLAGS is not a complete compiler argument list; flags were not overwritten') from error
    revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', ':external/nuttx'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('missing pinned NuttX gitlink revision')
    epoch = subprocess.check_output(['git', '-C', str(ROOT / 'external/nuttx'),
                                     'show', '-s', '--format=%ct', revision], text=True).strip()
    if not epoch.isdigit():
        raise ValueError('invalid pinned NuttX commit timestamp')
    if env.get('SOURCE_DATE_EPOCH', epoch) != epoch:
        raise ValueError('SOURCE_DATE_EPOCH conflicts with the pinned NuttX commit timestamp; value was not overwritten')
    mapped = '/nxrs-mpsc-build'
    flag = f'-fmacro-prefix-map={owned}={mapped}'
    combined = inherited + (' ' if inherited else '') + flag
    if shlex.split(combined, comments=True) != words + [flag]:
        raise ValueError('cannot safely append the owned-path map to EXTRAFLAGS')
    env.update(EXTRAFLAGS=combined, SOURCE_DATE_EPOCH=epoch)
    return dict(schema='mpsc-native-build-inputs-v1', source_date_epoch=int(epoch),
                source_date_epoch_origin=dict(component='nuttx', revision=revision, field='committer timestamp'),
                macro_prefix_map=dict(source='owned output directory', replacement=mapped),
                inherited_extraflags_sha256=hashlib.sha256(inherited.encode()).hexdigest())


def transport_selection(elf, prefix, backend, destination):
    """Check final-link API reachability, not the complete implementation graph."""
    if backend not in ('std', 'native'):
        raise ValueError('unsupported transport selection')
    output = subprocess.check_output([prefix + 'nm', '--defined-only', '--format=posix', str(elf)], text=True)
    defined = {parts[0] for line in output.splitlines()
               if len(parts := line.split()) >= 2 and parts[1] in ('T', 't', 'W', 'w')}
    retained = [name for name in NATIVE_QUEUE_ROOTS if name in defined]
    passed = len(retained) == len(NATIVE_QUEUE_ROOTS) if backend == 'native' else not retained
    record = dict(schema='mpsc-transport-selection-v1', elf_sha256=digest(elf), backend=backend,
                  scope='final linked native queue API roots; not full algorithm or indirect-call qualification',
                  native_queue_roots=list(NATIVE_QUEUE_ROOTS), retained_native_queue_roots=retained,
                  native_queue_roots_required=backend == 'native', passed=passed)
    destination.write_text(json.dumps(record, indent=2) + '\n')
    if not passed:
        raise ValueError('wrong or mixed linked transport selection; inspect transport-selection.json')
    return record


def archive_members(path, prefix):
    """Fingerprint link inputs without debug paths or archive timestamps.

    objcopy removes debug sections only. Symbols, relocations, executable and
    data sections remain in each member. Preserve order; never ignore runtime
    string/data differences or normalize an implementation mismatch away.
    """
    with tempfile.TemporaryDirectory(prefix='mpsc-kernel-identity-') as directory:
        root = Path(directory)
        normalized = root / 'normalized.a'
        subprocess.run([prefix + 'objcopy', '--strip-debug', str(path), str(normalized)],
                       check=True, capture_output=True)
        names = subprocess.check_output([prefix + 'ar', 't', str(normalized)], text=True).splitlines()
        if any(Path(n).name != n or n in ('.', '..') for n in names):
            raise ValueError('unsafe kernel archive member path')
        members = root / 'members'
        members.mkdir()
        subprocess.run([prefix + 'ar', 'x', str(normalized)], cwd=members, check=True, capture_output=True)
        counts, seen, result = Counter(names), Counter(), []
        for name in names:
            seen[name] += 1
            if counts[name] > 1:
                # Espressif's libarch legitimately contains duplicate basenames.
                # Default extraction overwrites them; xN selects each occurrence
                # without collapsing linker order or ignoring an earlier member.
                subprocess.run([prefix + 'ar', 'xN', str(seen[name]), str(normalized), name],
                               cwd=members, check=True, capture_output=True)
            result.append(dict(name=name, occurrence=seen[name], sha256=digest(members / name)))
        return result


def kernel_identity(out, prefix, config):
    """Retain raw archives as well as reproducible non-application identities."""
    firmware = out / 'firmware'
    archives = sorted((firmware / 'nuttx/staging').glob('*.a'))
    archives = [path for path in archives if path.name != 'libapps.a']
    if not archives:
        raise ValueError('missing non-application kernel archives')
    patch_files = sorted(firmware.glob('*-patches.json')) + [firmware / 'std-patch.json']
    if not {'nuttx-patches.json', 'nuttx-apps-patches.json', 'std-patch.json'} <= {p.name for p in patch_files}:
        raise ValueError('missing kernel/SDK patch receipts')
    record = dict(schema='mpsc-nuttx-kernel-v1',
                  scope='non-application staging archive members; debug stripped; member order retained',
                  resolved_config_sha256=digest(config),
                  patch_receipts={p.name: canonical(json.loads(p.read_text())) for p in patch_files},
                  archives={})
    for path in archives:
        if not path.resolve().is_relative_to(out):
            raise ValueError('kernel archive escapes owned build')
        members = archive_members(path, prefix)
        record['archives'][path.name] = dict(file=str(path.relative_to(out)),
                                             raw_sha256=digest(path), members=members,
                                             link_sha256=canonical(members))
    destination = out / 'kernel-identity.json'
    destination.write_text(json.dumps(record, indent=2) + '\n')
    return destination, [*archives, *patch_files]


def read_kernel_identity(root, record):
    name = record.get('kernel_identity_file')
    if name not in record['artifacts_sha256']:
        raise ValueError('unbound kernel identity')
    identity = json.loads((root / name).read_text())
    if (identity.get('schema') != 'mpsc-nuttx-kernel-v1'
            or identity.get('resolved_config_sha256') != digest(root / record['config_file'])
            or not identity.get('archives')):
        raise ValueError('kernel configuration/identity mismatch')
    present = {p.name for p in (root / 'firmware/nuttx/staging').glob('*.a') if p.name != 'libapps.a'}
    if set(identity['archives']) != present:
        raise ValueError('kernel archive census changed')
    for filename, expected in identity.get('patch_receipts', {}).items():
        path = 'firmware/' + filename
        if (Path(filename).name != filename or path not in record['artifacts_sha256']
                or canonical(json.loads((root / path).read_text())) != expected):
            raise ValueError('unbound kernel patch receipt')
    if not {'nuttx-patches.json', 'nuttx-apps-patches.json', 'std-patch.json'} <= identity.get('patch_receipts', {}).keys():
        raise ValueError('missing kernel/SDK patch identities')
    for archive, value in identity['archives'].items():
        path = value.get('file')
        if (Path(archive).name != archive or archive == 'libapps.a'
                or path != 'firmware/nuttx/staging/' + archive
                or path not in record['artifacts_sha256']
                or record['artifacts_sha256'][path] != value.get('raw_sha256')
                or canonical(value.get('members')) != value.get('link_sha256')):
            raise ValueError('unbound kernel archive identity')
    return identity


def comparable_kernel(identity):
    """Exact resolved inputs and link-relevant bytes; final placement is separate."""
    return dict(schema=identity['schema'], scope=identity['scope'],
                resolved_config_sha256=identity['resolved_config_sha256'],
                patch_receipts=identity['patch_receipts'],
                archives={name: value['link_sha256'] for name, value in identity['archives'].items()})


def validate_cohort(left, right):
    fields = ('board', 'board_contract', 'source_sha256', 'rust_compiler', 'rust_compiler_sha256',
              'c_compiler', 'c_compiler_sha256')
    if any(left.get(key) is None or left[key] != right.get(key) for key in fields):
        raise ValueError('unmatched build cohort')


def validate_nuttx_control(std, native):
    """Validate the queue substitution even when no physical captures exist."""
    if ((std.get('runtime'), std.get('backend')) != ('nuttx', 'std')
            or (native.get('runtime'), native.get('backend')) != ('nuttx', 'native')):
        raise ValueError('expected NuttX std/native control images')
    validate_cohort(std, native)
    if std.get('native_build_inputs') is None or std['native_build_inputs'] != native.get('native_build_inputs'):
        raise ValueError('unmatched deterministic NuttX compiler inputs')
    first, second = std.get('kernel_identity'), native.get('kernel_identity')
    if first is None or second is None or comparable_kernel(first) != comparable_kernel(second):
        raise ValueError('unmatched NuttX configuration, patches or kernel link inputs')
    raw_differences = [name for name, archive in first['archives'].items()
                       if archive['raw_sha256'] != second['archives'][name]['raw_sha256']]
    return dict(resolved_config_and_patches_equal=True, non_application_link_inputs_equal=True,
                raw_archives_identical=not raw_differences, raw_archive_differences=raw_differences,
                scope=first['scope'], final_image_layout_equal=False,
                thread_model='std::thread on both NuttX arms',
                matched_placement_qualified=False, physical_timing_qualified=False)


def inventory():
    files = []
    for directory in (HERE, ROOT / 'platform/build', ROOT / 'platform/build-support',
                      ROOT / 'upstream/rust-std', ROOT / 'upstream/nuttx',
                      ROOT / 'upstream/nuttx-apps', ROOT / 'upstream/esp-hal-3rdparty'):
        files.extend(p for p in directory.rglob('*') if p.is_file()
                     and not any(x in p.parts for x in ('target', '__pycache__', '.git'))
                     and (p.suffix in ('.rs', '.py', '.toml', '.lock', '.c', '.h', '.conf', '.overlay', '.patch', '.ld')
                          or p.name == 'CMakeLists.txt'))
    files.extend(p for p in (ROOT / 'platform/nuttx/std-app').rglob('*') if p.is_file())
    files.extend((ROOT / 'platform/nuttx/platforms' / f'{b}-mpsc.toml') for b in BOARDS)
    files.extend([ROOT / 'upstream/build-patches.json', ROOT / 'Cargo.toml', ROOT / 'Cargo.lock', ROOT / '.cargo/config.toml',
                  ROOT / 'platform/nuttx/linker/esp32s3-mpsc.ld',
                  HERE.parent / 'nuttx-std/channels/mpsc-fixture-iram.ld',
                  HERE.parent / 'rtos_harness/zephyr.py',
                  HERE.parent / 'event-services-comparison/image_package.py'])
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(set(files))}


def run(command, log, env, cwd=ROOT):
    with Path(log).open('w') as stream:
        subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=1800)


def load_image_tools():
    spec = importlib.util.spec_from_file_location('mpsc_image_package', HERE.parent / 'event-services-comparison/image_package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pack_image(data):
    """Add the ROM-prefix digest without moving any hidden flash mappings."""
    tool = load_image_tools()
    tool.analyze(data, 'simple-boot')
    if data[23] != 0:
        raise ValueError('expected an unmodified SIMPLE_BOOT input')
    offset = 24
    for _ in range(data[1]):
        _, size = struct.unpack_from('<II', data, offset)
        offset += 8 + size
    end = (offset + 16) // 16 * 16
    address, size = struct.unpack_from('<II', data, end)
    if address != 0 or size < 36 or size % 4:
        raise ValueError('missing digest padding')
    result = bytearray(data)
    result[23] = 1
    result[end:end + 32] = hashlib.sha256(result[:end]).digest()
    struct.pack_into('<II', result, end + 32, 0, size - 32)
    result = bytes(result)
    tool.analyze(result, 'simple-boot')
    if len(result) != len(data) or result[24:end] != data[24:end] or result[end + 40:] != data[end + 40:]:
        raise ValueError('packing changed application bytes or mappings')
    return result


def zephyr_loader(source):
    if hashlib.sha256(source).hexdigest() != '46fbaddc25cd8be2ff96aa88dd6ee156f40f5e3743d98db106ce72730ea80322':
        raise ValueError('unexpected Zephyr loader source')
    old = b'\t\t\toffset += (CHECKSUM_ALIGN - 1) - (offset % CHECKSUM_ALIGN) + 1;\n\t\t\tchecksum = true;'
    new = old.replace(b'\n\t\t\tchecksum', b'\n\t\t\tif (bootloader_image_hdr.hash_appended == 1) { offset += 32; }\n\t\t\tchecksum')
    if source.count(old) != 1:
        raise ValueError('ambiguous loader hook')
    return source.replace(old, new)


def validate_config(text, board, runtime):
    c = resolved_config(text)
    required = ({'CONFIG_USEC_PER_TICK': '10000', 'CONFIG_PTHREAD_STACK_DEFAULT': '4096',
                 'CONFIG_INIT_STACKSIZE': '8192'} if runtime == 'nuttx' else
                {'CONFIG_SYS_CLOCK_TICKS_PER_SEC': '100', 'CONFIG_MAIN_STACK_SIZE': '8192',
                 'CONFIG_MAIN_THREAD_PRIORITY': '20', 'CONFIG_SIZE_OPTIMIZATIONS': 'y'})
    if runtime == 'zephyr':
        required['CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC'] = str(BOARDS[board]['cpu-hz'])
    elif board == 'esp32s3':
        required['CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ'] = '240'
    else:
        required['CONFIG_RAM_SIZE'] = '532480'
    disabled = ('SMP', 'PRIORITY_INHERITANCE', 'SCHED_CRITMONITOR', 'SCHED_INSTRUMENTATION',
                'ESP32S3_SPIRAM', 'SCHED_TICKLESS') if runtime == 'nuttx' else (
                'SMP', 'TIMESLICING', 'TICKLESS_KERNEL', 'THREAD_RUNTIME_STATS', 'ESP_SPIRAM',
                'NETWORKING', 'WIFI', 'BT', 'PM')
    if any(c.get('CONFIG_' + name) == 'y' for name in disabled):
        raise ValueError('unmatched scheduling, accounting or resource configuration')
    for key, value in required.items():
        if c.get(key) != value:
            raise ValueError(f'{key}: expected {value}, got {c.get(key)}')


def build(args):
    board = BOARDS[args.board]
    backend = selected_backend(args.runtime, getattr(args, 'backend', None))
    tools = tomllib.loads(args.tools.read_text())[args.board]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    for key in ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'CARGO_BUILD_TARGET', 'RUSTC',
                'CARGO_BUILD_RUSTC', 'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER'):
        env.pop(key, None)
    if 'nuttx-tools' in tools:
        env['NXRS_QEMU_TOOLS_DIR'] = tools['nuttx-tools']
    prefix = tools['cross-prefix']
    env['PATH'] = os.pathsep.join([str(Path(prefix).parent), str(Path(tools['python']).parent),
                                  *tools.get('path', []), env['PATH']])
    env.update(RUSTC=tools['rustc'], RUSTC_BOOTSTRAP='1', MPSC_CLOCK_HZ=str(board['clock-hz']),
               MPSC_BACKEND=backend,
               CARGO_PROFILE_RELEASE_OPT_LEVEL=board['opt-level'],
               CARGO_PROFILE_RELEASE_LTO=board['lto'], CARGO_PROFILE_RELEASE_CODEGEN_UNITS='1',
               CARGO_PROFILE_RELEASE_PANIC='abort', CARGO_PROFILE_RELEASE_DEBUG='false',
               CARGO_PROFILE_RELEASE_STRIP='none', NXRS_NUTTX_NATIVE_TOOLS='1',
               NXRS_ESP32S3_NATIVE_TOOLS='1', NXRS_NUTTX_BUILD_JOBS='4')
    inputs = inventory()
    record = dict(schema=BUILD_SCHEMA, board=args.board, runtime=args.runtime, backend=backend,
                  source_sha256=inputs, board_contract=board, nonce=secrets.token_hex(16),
                  native_build_inputs=configure_native_inputs(env, out) if args.runtime == 'nuttx' else None,
                  status='failed', failure='incomplete build', physical_timing_qualified=False)
    record['build_id'] = canonical({k: record[k] for k in IDENTITY_FIELDS})
    env['MPSC_BUILD_ID'] = record['build_id']
    board_dir = HERE / 'boards' / args.board
    wrapper = out / 'sdk.c'
    wrapper.write_text(f'#include "{board_dir}/board.h"\n#include "{HERE}/sdk_{args.runtime}.c"\n')
    try:
        record['c_compiler_sha256'] = digest(prefix + 'gcc')
        record['c_compiler'] = subprocess.check_output([prefix + 'gcc', '--version'], env=env, text=True)
        if args.runtime == 'nuttx':
            firmware = out / 'firmware'
            command = ['cargo', 'nxrs', 'build', '--manifest-path', HERE / 'rust/Cargo.toml',
                       '--platform', board['nuttx-platform'], '--profile', args.board + '-mpsc', '--out', firmware,
                       '--abi-profile', 'active']
            for source in (wrapper, board_dir / 'clock.c', board_dir / 'console_nuttx.c'):
                command += ['--target-c-source', source]
            run(command, out / 'build.log', env)
            selected = json.loads((firmware / 'std-source.json').read_text())
            if selected['selected_source_matches'] is not True:
                raise ValueError('wrong std source selected')
            record['rust_compiler'] = subprocess.check_output([selected['rustc'], '-vV'], text=True)
            record['rust_compiler_sha256'] = digest(selected['rustc'])
            if args.board == 'pico2w':
                import placement
                placement.prepare(out, prefix, env, backend=backend)
            elf, config = firmware / 'nuttx/nuttx', firmware / 'resolved.config'
            image = firmware / 'nuttx/nuttx.bin'
            evidence = [firmware / name for name in ('std-source.json', 'std-patch.json', 'nuttx-patches.json', 'abi-report.json', 'provenance.txt')]
            if args.board == 'esp32s3':
                # The normal frontend applies the matching NuttX loader patch.
                loader = firmware / 'nuttx/arch/xtensa/src/common/espressif/esp_loader.c'
                if digest(loader) != 'a35533da7d7b800cbac6a6e6de2a307cff727284c0ced2db54269290691c00a9':
                    raise ValueError('unqualified NuttX digest loader')
                evidence.append(loader)
        else:
            zephyr = Path(tools['zephyr']).resolve()
            record['revisions'] = {'zephyr': check_checkout(zephyr, 'Zephyr', ZEPHYR_SHA)}
            modules = []
            for name, pin in board['modules'].items():
                path = Path(tools['modules'][name]).resolve()
                record['revisions'][name] = check_checkout(path, name, pin)
                modules.append(str(path))
            env.update(RUSTC=tools['rustc'], ZEPHYR_BASE=str(zephyr))
            record['rust_compiler'] = subprocess.check_output([tools['rustc'], '-vV'], text=True)
            record['rust_compiler_sha256'] = digest(tools['rustc'])
            target = board['zephyr-target']
            command = ['cargo', 'rustc', '--manifest-path', HERE / 'rust/Cargo.toml', '--locked',
                       '--release', '--lib', '--target', target, '--target-dir', out / 'rust',
                       '-Zbuild-std=core,compiler_builtins', '-Zbuild-std-features=compiler-builtins-mem' +
                       (',optimize_for_size' if args.board == 'pico2w' else '')]
            run(command, out / 'rust-build.log', env, HERE / 'rust')
            archive = out / 'rust' / target / 'release/libmpsc_comparison.a'
            firmware = out / 'firmware'
            command = ['cmake', '-GNinja', '-S', HERE / 'zephyr', '-B', firmware,
                       '-DBOARD=' + board['zephyr-board'], '-DZephyr_DIR=' + str(zephyr / 'share/zephyr-package/cmake'),
                       '-DPython3_EXECUTABLE=' + tools['python'], '-DZEPHYR_MODULES=' + ';'.join(modules),
                       '-DMPSC_BOARD_DIR=' + str(board_dir), '-DMPSC_SDK_SOURCE=' + str(wrapper),
                       '-DMPSC_RUST_ARCHIVE=' + str(archive), '-DZEPHYR_TOOLCHAIN_VARIANT=cross-compile',
                       '-DCROSS_COMPILE=' + prefix]
            evidence = [archive]
            if args.board == 'esp32s3':
                loader = out / 'loader.c'
                loader.write_bytes(zephyr_loader((zephyr / 'soc/espressif/common/loader.c').read_bytes()))
                layout = out / 'placement.ld'
                source = (zephyr / 'soc/espressif/esp32s3/default.ld').read_text()
                hook = '    *(.iram1 .iram1.*)'
                if source.count(hook) != 1:
                    raise ValueError('unrecognized Zephyr IRAM hook')
                source = source.replace('#include "memory.h"', f'#include "{zephyr}/soc/espressif/esp32s3/memory.h"')
                layout.write_text(source.replace(hook, hook + '\n' + (HERE.parent / 'nuttx-std/channels/mpsc-fixture-iram.ld').read_text()))
                command += ['-DMPSC_LOADER_SOURCE=' + str(loader), '-DSOC_LINKER_SCRIPT=' + str(layout)]
                evidence += [loader, layout]
            run(command, out / 'configure.log', env)
            run(['cmake', '--build', firmware, '--parallel', '4'], out / 'native-build.log', env)
            if args.board == 'pico2w':
                import placement
                placement.prepare(out, prefix, env, zephyr=True)
            elf, config, image = (firmware / 'zephyr' / name for name in ('zephyr.elf', '.config', 'zephyr.bin'))
        validate_config(config.read_text(), args.board, args.runtime)
        selection = out / 'transport-selection.json'
        transport_selection(elf, prefix, backend, selection)
        record['transport_selection_file'] = str(selection.relative_to(out))
        evidence.append(selection)
        if args.board == 'esp32s3':
            import placement
            placement.audit(elf, prefix, out / 'hot-audit.json', board=args.board, backend=backend)
        if args.runtime == 'nuttx':
            identity, kernel_evidence = kernel_identity(out, prefix, config)
            record['kernel_identity_file'] = str(identity.relative_to(out))
            evidence += [identity, *kernel_evidence]
        if args.board == 'esp32s3':
            packed = out / 'firmware.bin'
            packed.write_bytes(pack_image(image.read_bytes()))
            evidence.append(image)
            image = packed
        for key, path in (('elf', elf), ('image', image), ('config', config)):
            record[key + '_file'] = str(path.relative_to(out))
        evidence += [elf, image, config, wrapper]
        evidence += list(out.glob('hot-*.json'))
        record['artifacts_sha256'] = {str(p.relative_to(out)): digest(p) for p in evidence if p.is_file()}
        if inventory() != inputs:
            raise ValueError('source drift during build')
        record.update(status='complete', failure=None)
    except BaseException as error:
        record['failure'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        (out / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def receipt(root):
    root = Path(root).resolve()
    record = json.loads((root / 'build.json').read_text())
    if record.get('schema') != BUILD_SCHEMA or record.get('status') != 'complete' or record.get('failure') is not None:
        raise ValueError('incomplete build')
    selected_backend(record['runtime'], record['backend'])
    identity = canonical({k: record[k] for k in IDENTITY_FIELDS})
    if record['build_id'] != identity or record['source_sha256'] != inventory():
        raise ValueError('build identity/source drift')
    for name, sha in record['artifacts_sha256'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest(path) != sha:
            raise ValueError('artifact drift/escape: ' + name)
    for key in ('image', 'elf', 'config'):
        if record[key + '_file'] not in record['artifacts_sha256']:
            raise ValueError('unbound ' + key)
    name = record.get('transport_selection_file')
    if name not in record['artifacts_sha256']:
        raise ValueError('unbound transport selection')
    selection = json.loads((root / name).read_text())
    expected_roots = list(NATIVE_QUEUE_ROOTS) if record['backend'] == 'native' else []
    if (selection.get('schema') != 'mpsc-transport-selection-v1' or selection.get('passed') is not True
            or selection.get('elf_sha256') != record['artifacts_sha256'][record['elf_file']]
            or selection.get('backend') != record['backend']
            or selection.get('native_queue_roots') != list(NATIVE_QUEUE_ROOTS)
            or selection.get('retained_native_queue_roots') != expected_roots
            or selection.get('native_queue_roots_required') is not (record['backend'] == 'native')):
        raise ValueError('transport selection does not match build/ELF')
    validate_config((root / record['config_file']).read_text(), record['board'], record['runtime'])
    if record['runtime'] == 'nuttx':
        record['kernel_identity'] = read_kernel_identity(root, record)
    return record
