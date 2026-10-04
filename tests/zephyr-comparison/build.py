#!/usr/bin/env python3
"""Build a pinned Zephyr comparison image without changing its input checkouts."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
_PROVENANCE_SPEC = importlib.util.spec_from_file_location(
    'zephyr_provenance', HERE / 'provenance.py')
_PROVENANCE = importlib.util.module_from_spec(_PROVENANCE_SPEC)
_PROVENANCE_SPEC.loader.exec_module(_PROVENANCE)
verify_firmware_sources = _PROVENANCE.verify_firmware_sources
BOARD = 'esp32s3_devkitm/esp32s3/procpu'
ZEPHYR_SHA = '75f67d766726351b30199f9a2bf55803d717a3be'
PINS = {'espressif': 'af6cfa2e3e7098b596062ab516b80a48a7ba7332',
        'xtensa': '3cc9e3a9360be5c96c956dce84064b85439b6769'}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def check_checkout(path, name, expected=None):
    path = Path(path).resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f'{name} is not a directory: {path}')
    root = Path(git(path, 'rev-parse', '--show-toplevel')).resolve()
    if root != path:
        raise ValueError(f'{name} must be the checkout root: {path}')
    dirty = git(path, 'status', '--porcelain', '--untracked-files=no')
    if dirty:
        raise ValueError(f'{name} has modified tracked input files')
    revision = git(path, 'rev-parse', 'HEAD')
    if expected and revision != expected:
        raise ValueError(f'{name} revision mismatch: expected {expected}, got {revision}')
    return revision


def input_revisions(args):
    zephyr = check_checkout(args.zephyr, 'Zephyr')
    release = git(args.zephyr, 'rev-parse', 'v4.3.1^{commit}')
    if zephyr != release or zephyr != ZEPHYR_SHA:
        raise ValueError(f'Zephyr must be v4.3.1 at {ZEPHYR_SHA}; got {zephyr}')
    return {'zephyr': zephyr,
            'hal_espressif': check_checkout(args.espressif, 'hal_espressif', PINS['espressif']),
            'hal_xtensa': check_checkout(args.xtensa, 'hal_xtensa', PINS['xtensa'])}


def parse_sections(output):
    """Parse GNU/LLVM readelf -W -S rows without depending on column spacing."""
    sections = []
    row = re.compile(r'^\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+'
                     r'([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+'
                     r'([0-9a-fA-F]+)\s+\S+\s+(\S+)')
    for line in output.splitlines():
        match = row.match(line)
        if match:
            name, kind, address, offset, size, flags = match.groups()
            sections.append(dict(name=name, type=kind, address=int(address, 16),
                                  offset=int(offset, 16), size=int(size, 16), flags=flags))
    if not sections:
        raise ValueError('readelf produced no parseable ELF section rows')
    return sections


def section_accounting(sections):
    allocated = [s for s in sections if 'A' in s['flags']]
    padding_names = {'.dram0.dummy', '.drom0.dummy',
                     '.flash.text_dummy', '.flash.rodata_dummy'}
    padding = [s for s in allocated if s['name'].lower() in padding_names]
    counted = [s for s in allocated if s['name'].lower() not in padding_names]
    flash = [s for s in counted if s['type'] != 'NOBITS']
    def resident_ram(section):
        address = section['address']
        return (0x40370000 <= address < 0x40400000 or  # ESP32-S3 IRAM
                0x3FC80000 <= address < 0x3FD00000 or  # internal DRAM
                0x50000000 <= address < 0x50100000 or  # RTC slow memory
                0x600FE000 <= address < 0x60100000 or  # RTC fast memory
                0x3D000000 <= address < 0x3E000000)    # external RAM, if linked

    ram = [s for s in counted if s['name'].lower() not in {'.heap', '.heap.noinit'}
           and resident_ram(s)]
    return dict(loadbearing_flash_bytes=sum(s['size'] for s in flash),
                resident_ram_bytes=sum(s['size'] for s in ram),
                flash_sections=[s['name'] for s in flash],
                resident_ram_sections=[s['name'] for s in ram],
                excluded_dummy_padding=[dict(name=s['name'], size=s['size']) for s in padding],
                note=('Complete allocated ELF sections remain listed above. Loadbearing flash '
                      'excludes NOBITS, debug and ESP dummy padding sections; resident RAM counts '
                      'IRAM, DRAM, BSS and noinit sections once, excluding dummy padding and '
                      'unused heap/address gaps. zephyr.bin file length also includes image headers '
                      'and alignment padding, so it can exceed loadbearing section bytes.'))


def resolved_config(text):
    values = {}
    for line in text.splitlines():
        match = re.match(r'(CONFIG_[A-Z0-9_]+)=(.*)$', line)
        if match:
            values[match.group(1)] = match.group(2).strip('"')
        else:
            match = re.match(r'# (CONFIG_[A-Z0-9_]+) is not set$', line)
            if match:
                values[match.group(1)] = 'n'
    return values


def assert_config(text):
    config = resolved_config(text)
    required = {'CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC': '240000000',
                'CONFIG_MINIMAL_LIBC': 'y', 'CONFIG_ESP_SIMPLE_BOOT': 'y',
                'CONFIG_MAIN_STACK_SIZE': '8192', 'CONFIG_ISR_STACK_SIZE': '4096',
                'CONFIG_SIZE_OPTIMIZATIONS': 'y', 'CONFIG_TIMESLICING': 'y',
                'CONFIG_TIMESLICE_SIZE': '10', 'CONFIG_TIMESLICE_PRIORITY': '0',
                'CONFIG_ESPTOOLPY_FLASHMODE_DIO': 'y',
                'CONFIG_HEAP_MEM_POOL_SIZE': '0',
                'CONFIG_HEAP_MEM_POOL_ADD_SIZE_BOARD': '4096',
                'CONFIG_SYS_HEAP_RUNTIME_STATS': 'y', 'CONFIG_ESP_SPIRAM': 'n'}
    for key, value in required.items():
        if config.get(key) != value:
            raise ValueError(f'resolved config requires {key}={value}, got {config.get(key)!r}')
    disabled = ('CONFIG_SMP', 'CONFIG_NETWORKING', 'CONFIG_WIFI', 'CONFIG_BT',
                'CONFIG_SHELL', 'CONFIG_ESP_SPIRAM')
    invalid = [key for key in disabled if config.get(key) != 'n']
    if invalid:
        raise ValueError('resolved config must explicitly disable ' + ', '.join(invalid))
    return config


def assert_project_stacks(root=HERE):
    text = (Path(root) / 'native_adapter.c').read_text()
    if not re.search(r'K_THREAD_STACK_DEFINE\s*\(\s*collector_stack\s*,\s*6144\s*\)', text):
        raise ValueError('project must define the collector thread stack as 6144 bytes')
    if not re.search(r'K_THREAD_STACK_ARRAY_DEFINE\s*\(\s*role_stacks\s*,[^,]+,\s*4096\s*\)', text):
        raise ValueError('project must define role thread stacks as 4096 bytes')


def config_report(text):
    config = resolved_config(text)
    selected = {key: value for key, value in config.items()
                if any(term in key for term in ('FLASH', 'CACHE', 'FREQ', 'CLOCK'))}
    mode = 'DIO' if config.get('CONFIG_ESPTOOLPY_FLASHMODE_DIO') == 'y' else 'other/unresolved'
    frequency = next((key.removeprefix('CONFIG_ESPTOOLPY_FLASHFREQ_')
                      for key, value in config.items()
                      if key.startswith('CONFIG_ESPTOOLPY_FLASHFREQ_') and value == 'y'),
                     'other/unresolved')
    def choice(prefix, options):
        return next((option for option in options if config.get(prefix + option) == 'y'), None)

    cache = {
        'instruction_kib': choice('CONFIG_ESP32S3_INSTRUCTION_CACHE_', ['16KB', '32KB']),
        'instruction_ways': choice('CONFIG_ESP32S3_INSTRUCTION_CACHE_', ['4WAYS', '8WAYS']),
        'instruction_line_bytes': choice('CONFIG_ESP32S3_INSTRUCTION_CACHE_LINE_', ['16B', '32B']),
        'data_kib': choice('CONFIG_ESP32S3_DATA_CACHE_', ['16KB', '32KB', '64KB']),
        'data_ways': choice('CONFIG_ESP32S3_DATA_CACHE_', ['4WAYS', '8WAYS']),
        'data_line_bytes': choice('CONFIG_ESP32S3_DATA_CACHE_LINE_', ['16B', '32B', '64B']),
    }
    cache = {key: int(re.match(r'\d+', value).group()) if value else None
             for key, value in cache.items()}
    zephyr_flash_mhz = int(frequency[:-1]) if frequency.endswith('M') else None
    nuttx = {'cpu_mhz': 240, 'flash_mode': 'DIO', 'flash_frequency_mhz': 40,
             'instruction_kib': 16, 'instruction_ways': 8,
             'instruction_line_bytes': 32, 'data_kib': 32,
             'data_ways': 8, 'data_line_bytes': 32, 'rr_interval_ms': 10,
             'resolved_config_sha256': '936ecb48e8c728347d3f9a82aebe11d14a571c4cda5e0ac1e7fb474cd2e39e97'}
    differences = {}
    for key, zephyr_value, nuttx_value in [
            ('flash_mode', mode, nuttx['flash_mode']),
            ('flash_frequency_mhz', zephyr_flash_mhz, nuttx['flash_frequency_mhz']),
            *[(key, value, nuttx[key]) for key, value in cache.items()]]:
        if zephyr_value != nuttx_value:
            differences[key] = {'zephyr': zephyr_value, 'nuttx': nuttx_value}
    return dict(flash_mode=mode, flash_frequency_mhz=zephyr_flash_mhz,
                flash_cache_symbols=selected, cache=cache, nuttx_reference=nuttx,
                differences_vs_nuttx=differences,
                heap={'general_pool_config_bytes': int(config['CONFIG_HEAP_MEM_POOL_SIZE']),
                      'board_added_kernel_heap_bytes': int(config['CONFIG_HEAP_MEM_POOL_ADD_SIZE_BOARD']),
                      'runtime_statistics': config['CONFIG_SYS_HEAP_RUNTIME_STATS'] == 'y',
                      'application_allocation_model': 'fixed static queues and stacks; no workload heap allocation',
                      'note': 'The 4 KiB board-added kernel heap arena is linked into BSS and is '
                              'counted in resident RAM. CONFIG_HEAP_MEM_POOL_SIZE=0 does not mean '
                              'the Zephyr kernel has no heap.'},
                timing={'zephyr_timeslice_ms': int(config['CONFIG_TIMESLICE_SIZE']),
                        'nuttx_rr_interval_ms': nuttx['rr_interval_ms'],
                        'primary_counter': 'Xtensa CCOUNT; kernel uptime tick is coarse and used only for wrap checks'})


def readelf_for(sdk):
    sdk = Path(sdk).resolve(strict=True)
    tool = (sdk / 'xtensa-espressif_esp32s3_zephyr-elf' / 'bin' /
            'xtensa-espressif_esp32s3_zephyr-elf-readelf')
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise ValueError(f'expected ESP32-S3 SDK readelf executable is missing: {tool}')
    return tool


def source_hashes(root, mode, variant=None):
    """Return only explicitly compiled/configured firmware input digests."""
    return verify_firmware_sources(None, mode, variant, root)['firmware_sources']


def run_logged(command, log, cwd=None, env=None):
    result = subprocess.run(list(map(str, command)), cwd=cwd, text=True,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    Path(log).write_text(result.stdout)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout)
    return result.stdout


def cmake_command(args, out):
    command = ['cmake', '-GNinja', '-S', HERE, '-B', out, f'-DBOARD={BOARD}',
               f'-DZEPHYR_BASE={args.zephyr.resolve()}',
               f'-DZephyr_DIR={args.zephyr.resolve()}/share/zephyr-package/cmake',
               f'-DZEPHYR_MODULES={args.espressif.resolve()};{args.xtensa.resolve()}',
               f'-DZEPHYR_SDK_INSTALL_DIR={args.sdk.resolve()}',
               f'-DPython3_EXECUTABLE={args.python.absolute()}', f'-DNXRS_MODE={args.mode}',
               f'-DNXRS_APP_SPEED={"ON" if args.profile == "speed" else "OFF"}']
    overlays = []
    if args.cross_compile:
        command += ['-DZEPHYR_TOOLCHAIN_VARIANT=cross-compile',
                    f'-DCROSS_COMPILE={args.cross_compile.absolute()}']
    if args.native_defaults:
        overlays.append(str(HERE / 'native-defaults.conf'))
    if args.lean:
        overlays.append(str(HERE / 'lean.conf'))
    if overlays:
        command.append('-DEXTRA_CONF_FILE=' + ';'.join(overlays))
    return command


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zephyr', required=True, type=Path, help='pinned Zephyr checkout')
    parser.add_argument('--espressif', required=True, type=Path, help='hal_espressif checkout')
    parser.add_argument('--xtensa', required=True, type=Path, help='hal_xtensa checkout')
    parser.add_argument('--sdk', required=True, type=Path)
    parser.add_argument('--python', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--profile', required=True, choices=['size', 'speed'])
    parser.add_argument('--mode', required=True, choices=['wire', 'packet', 'baseline'])
    parser.add_argument('--native-defaults', action='store_true',
                        help='test sensitivity to the board 80 MHz flash default')
    parser.add_argument('--lean', action='store_true',
                        help='apply lean.conf sensitivity settings; not the matched primary config')
    parser.add_argument('--cross-compile', type=Path,
                        help='optional compiler prefix for the compiler-matched control')
    args = parser.parse_args(argv)
    if not args.python.is_file() or not os.access(args.python, os.X_OK):
        parser.error('--python must name an existing executable file')
    if not args.sdk.is_dir():
        parser.error('--sdk must name an existing SDK directory')
    if args.cross_compile and not Path(str(args.cross_compile) + 'gcc').is_file():
        parser.error('--cross-compile must be a prefix with an existing gcc executable')
    args.out = args.out.resolve()
    if args.out.exists():
        parser.error(f'--out must not exist: {args.out}')
    return args


def build(args):
    out = args.out
    out.mkdir(parents=True)
    record = {'schema': 1, 'status': 'failed', 'mode': args.mode,
              'profile': args.profile, 'board': BOARD, 'commands': {},
              'toolchain_variant': 'cross-compile' if args.cross_compile else 'zephyr-sdk',
              'failure': 'build did not complete'}
    provenance = out / 'build-provenance.json'
    provenance.write_text(json.dumps(record, indent=2) + '\n')
    try:
        provenance_data = verify_firmware_sources(
            None, args.mode,
            {'native_defaults': args.native_defaults, 'lean': args.lean}, HERE)
        record['source_sha256'] = provenance_data['firmware_sources']
        record['tool_sha256'] = provenance_data['current_tools']['source_sha256']
        record['generated_control'] = provenance_data['generated_control']
        (out / 'source-hashes.json').write_text(
            json.dumps(record['source_sha256'], indent=2) + '\n')
        revisions = input_revisions(args)
        record['revisions'] = revisions
        python = args.python.absolute()
        record['python'] = str(python)
        record['sdk'] = str(args.sdk.resolve())
        cmake = cmake_command(args, out)
        record['commands']['configure'] = list(map(str, cmake))
        env = dict(os.environ)
        env['ZEPHYR_BASE'] = str(args.zephyr.resolve())
        env['PATH'] = str(python.parent) + os.pathsep + env.get('PATH', '')
        record['python_tools_path'] = str(python.parent)
        run_logged(cmake, out / 'configure.log', env=env)
        configured_provenance = verify_firmware_sources(
            out, args.mode,
            {'native_defaults': args.native_defaults, 'lean': args.lean}, HERE)
        record['generated_control'] = configured_provenance['generated_control']
        cache = (out / 'CMakeCache.txt').read_text()
        config_path = out / 'zephyr/.config'
        config_text = config_path.read_text()
        assert_config(config_text)
        assert_project_stacks()
        configuration = config_report(config_text)
        expected_frequency = 80 if args.native_defaults else 40
        if configuration['flash_frequency_mhz'] != expected_frequency:
            raise ValueError(f'resolved flash frequency must be {expected_frequency} for this profile')
        configuration['variant'] = {'native_defaults': args.native_defaults,
                                    'lean': args.lean,
                                    'assertion_matched': not args.lean,
                                    'hardware_matched': not args.native_defaults}
        (out / 'configuration-report.json').write_text(
            json.dumps(configuration, indent=2) + '\n')
        shutil.copy2(config_path, out / 'resolved.config')
        compiler_match = re.search(r'^CMAKE_C_COMPILER:FILEPATH=(.+)$', cache, re.M)
        if not compiler_match:
            compiler_match = re.search(r'^CMAKE_C_COMPILER:STRING=(.+)$', cache, re.M)
        if not compiler_match:
            raise ValueError('CMakeCache.txt does not identify CMAKE_C_COMPILER')
        compiler = compiler_match.group(1)
        compiler_info = subprocess.check_output([compiler, '--version'], text=True, env=env)
        record['compiler'] = {'path': compiler, 'version': compiler_info}
        command = ['cmake', '--build', out, '--parallel', '2']
        record['commands']['build'] = list(map(str, command))
        run_logged(command, out / 'build.log', env=env)
        artifact_dir = out / 'zephyr'
        elf, image, mapfile = artifact_dir / 'zephyr.elf', artifact_dir / 'zephyr.bin', artifact_dir / 'zephyr.map'
        for artifact in (elf, image, mapfile):
            if not artifact.is_file():
                raise ValueError(f'missing expected build artifact: {artifact}')
        readelf = readelf_for(args.sdk)
        section_output = subprocess.check_output([readelf, '-W', '-S', elf], text=True, env=env)
        (out / 'readelf-sections.txt').write_text(section_output)
        sections = parse_sections(section_output)
        (out / 'elf-sections.json').write_text(json.dumps(
            dict(sections=sections, accounting=section_accounting(sections)), indent=2) + '\n')
        shutil.copy2(image, out / 'zephyr.bin')
        shutil.copy2(mapfile, out / 'zephyr.map')
        record.update(status='success', zephyr_bin_bytes=image.stat().st_size,
                      zephyr_bin_sha256=sha256(image), elf_sha256=sha256(elf),
                      map_sha256=sha256(mapfile), resolved_config_sha256=sha256(config_path),
                      readelf=str(readelf.resolve()), configuration=configuration)
    except Exception as exc:
        record['failure'] = f'{type(exc).__name__}: {exc}'
        provenance.write_text(json.dumps(record, indent=2) + '\n')
        raise
    else:
        record['failure'] = None
        record.pop('source_sha256')
        provenance.write_text(json.dumps(record, indent=2) + '\n')
        return record


def main(argv=None):
    args = arguments(argv)
    try:
        build(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'zephyr build failed: {exc}', file=sys.stderr)
        return 1
    print(f'ZEPHYR_ARTIFACT_DIRECTORY={args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
