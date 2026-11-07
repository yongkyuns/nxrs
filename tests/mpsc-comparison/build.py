"""Board wiring around the normal NuttX frontend and pinned Zephyr CMake build."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
from rtos_harness.zephyr import ZEPHYR_SHA, check_checkout, resolved_config

BOARDS = tomllib.loads((HERE / 'boards.toml').read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


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
               CARGO_PROFILE_RELEASE_OPT_LEVEL=board['opt-level'],
               CARGO_PROFILE_RELEASE_LTO=board['lto'], CARGO_PROFILE_RELEASE_CODEGEN_UNITS='1',
               CARGO_PROFILE_RELEASE_PANIC='abort', CARGO_PROFILE_RELEASE_DEBUG='false',
               CARGO_PROFILE_RELEASE_STRIP='none', NXRS_NUTTX_NATIVE_TOOLS='1',
               NXRS_ESP32S3_NATIVE_TOOLS='1', NXRS_NUTTX_BUILD_JOBS='4')
    inputs = inventory()
    record = dict(schema='mpsc-comparison-build-v2', board=args.board, runtime=args.runtime,
                  source_sha256=inputs, board_contract=board, nonce=secrets.token_hex(16),
                  status='failed', failure='incomplete build', physical_timing_qualified=False)
    record['build_id'] = canonical({k: record[k] for k in ('schema', 'board', 'runtime', 'source_sha256', 'board_contract', 'nonce')})
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
            if args.board == 'pico2w':
                import placement
                placement.prepare(out, prefix, env)
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
    if record.get('schema') != 'mpsc-comparison-build-v2' or record.get('status') != 'complete' or record.get('failure') is not None:
        raise ValueError('incomplete build')
    identity = canonical({k: record[k] for k in ('schema', 'board', 'runtime', 'source_sha256', 'board_contract', 'nonce')})
    if record['build_id'] != identity or record['source_sha256'] != inventory():
        raise ValueError('build identity/source drift')
    for name, sha in record['artifacts_sha256'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest(path) != sha:
            raise ValueError('artifact drift/escape: ' + name)
    for key in ('image', 'elf', 'config'):
        if record[key + '_file'] not in record['artifacts_sha256']:
            raise ValueError('unbound ' + key)
    validate_config((root / record['config_file']).read_text(), record['board'], record['runtime'])
    return record
