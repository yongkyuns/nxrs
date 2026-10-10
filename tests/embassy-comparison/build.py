#!/usr/bin/env python3
"""Build and record a reproducible ESP32-S3 Embassy comparison image."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
TARGET = 'xtensa-esp32s3-none-elf'
sys.path.insert(0, str(HERE.parent))
from rtos_harness.images import (assert_stack_section, image_header, parse_sections,
                                 esp_idf_section_accounting as section_accounting)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_inventory(root=HERE):
    """Hash every explicitly build-relevant local and shared Rust source."""
    root = Path(root)
    paths = [root / 'Cargo.toml', root / 'Cargo.lock', root / 'build.rs',
             root / 'stack.x', root / '.cargo' / 'config.toml']
    paths.extend(sorted((root / 'src').glob('*.rs')))
    paths.extend(root.parent / 'service-footprint' / 'src' / name
                 for name in ('payload_kernels.rs', 'packet_service.rs'))
    paths.append(root.parent / 'rtos_harness' / 'images.py')
    result = {}
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f'missing build input: {path}')
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = '../' + path.relative_to(root.parent).as_posix()
        result[label] = sha256(path)
    return result


def assert_stack_guard(sections, symbols):
    stack = assert_stack_section(sections)
    rows = re.findall(
        r'^\s*\d+:\s*([0-9a-fA-F]+)\s+\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+__stack_chk_guard(?:\s|$)',
        symbols, re.M)
    expected = stack['address'] + 64
    if not rows or any(int(value, 16) != expected for value in rows):
        actual = [int(value, 16) for value in rows]
        raise ValueError(f'ELF stack guard must be at .stack + 64 ({expected:#x}); got {actual}')


def run_logged(command, log_path, cwd=None):
    try:
        result = subprocess.run(list(map(str, command)), cwd=cwd, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = result.stdout
    except OSError as exc:
        output = f'{type(exc).__name__}: {exc}\n'
        Path(log_path).write_text(output)
        raise
    Path(log_path).write_text(output)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, output=output)
    return output


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path, help='fresh output directory')
    parser.add_argument('--mode', required=True, choices=('wire', 'packet', 'baseline'))
    parser.add_argument('--profile', required=True, choices=('release', 'size'))
    parser.add_argument('--cooperative-yield', action='store_true',
                        help='separate one-message-per-poll scheduling control')
    parser.add_argument('--espflash', required=True, type=Path, help='espflash executable')
    parser.add_argument('--readelf', required=True, type=Path, help='target readelf executable')
    parser.add_argument('--cargo-target-dir', type=Path,
                        help='Cargo build cache directory (defaults to OUT/cargo)')
    args = parser.parse_args(argv)
    args.out = args.out.resolve()
    if args.out.exists():
        parser.error(f'--out must be fresh: {args.out}')
    for name in ('espflash', 'readelf'):
        path = getattr(args, name)
        if not path.is_file() or not os.access(path, os.X_OK):
            parser.error(f'--{name} must name an executable file')
        setattr(args, name, path.resolve())
    return args


def cargo_build_command(mode, profile, target_dir, cooperative_yield=False):
    command = ['cargo', '+esp', 'build', '--locked', '--profile', profile,
               '--target-dir', target_dir, '--target', TARGET]
    features = [mode] if mode in ('packet', 'baseline') else []
    if cooperative_yield:
        features.append('cooperative-yield')
    if features:
        command.extend(['--features', ','.join(features)])
    return command


def build(args):
    out = args.out
    out.mkdir(parents=True)
    record = {
        'schema': 1, 'status': 'failed', 'failure': 'build did not complete',
        'mode': args.mode, 'profile': args.profile, 'target': TARGET,
        'cooperative_yield': args.cooperative_yield,
        'commands': {}, 'source_sha256': {}, 'tool_versions': {},
        'configuration': {
            'cpu_mhz': 240, 'cores': 1, 'flash_mode': 'DIO',
            'flash_frequency_mhz': 40, 'flash_size_mb': 16,
            'instruction_cache': {'kib': 16, 'ways': 8, 'line_bytes': 32},
            'data_cache': {'kib': 32, 'ways': 8, 'line_bytes': 32},
            'psram': False,
        },
    }
    provenance = out / 'build-provenance.json'
    provenance.write_text(json.dumps(record, indent=2) + '\n')
    try:
        record['source_sha256'] = source_inventory()
        versions = {
            'rustc': ['rustc', '+esp', '--version'],
            'cargo': ['cargo', '+esp', '--version'],
            'espflash': [args.espflash, '--version'],
        }
        for name, command in versions.items():
            try:
                record['tool_versions'][name] = run_logged(
                    command, out / f'{name}-version.log', cwd=HERE).strip()
            except (OSError, subprocess.SubprocessError) as exc:
                record['tool_versions'][name] = {'error': f'{type(exc).__name__}: {exc}'}

        cargo_target_dir = (args.cargo_target_dir.resolve() if args.cargo_target_dir
                            else out / 'cargo')
        record['cargo_target_dir'] = str(cargo_target_dir)
        cargo = cargo_build_command(args.mode, args.profile, cargo_target_dir, args.cooperative_yield)
        record['commands']['cargo_build'] = list(map(str, cargo))
        run_logged(cargo, out / 'cargo-build.log', cwd=HERE)
        built_elf = cargo_target_dir / TARGET / args.profile / 'embassy-scale'
        if not built_elf.is_file():
            raise FileNotFoundError(f'Cargo did not produce expected ELF: {built_elf}')
        elf = out / 'embassy.elf'
        shutil.copy2(built_elf, elf)

        image = out / 'embassy.bin'
        espflash = [args.espflash, 'save-image', '--chip', 'esp32s3', '--flash-mode', 'dio',
                    '--flash-freq', '40mhz', '--flash-size', '16mb', '--merge',
                    '--skip-padding', '--skip-update-check', elf, image]
        record['commands']['espflash'] = list(map(str, espflash))
        run_logged(espflash, out / 'espflash.log', cwd=HERE)
        if not image.is_file():
            raise FileNotFoundError(f'espflash did not produce merged image: {image}')
        record['image_header'] = image_header(image)

        readelf_command = [args.readelf, '-W', '-S', elf]
        record['commands']['readelf_sections'] = list(map(str, readelf_command))
        section_text = run_logged(readelf_command, out / 'readelf-sections.log', cwd=HERE)
        sections = parse_sections(section_text)
        assert_stack_section(sections)
        readelf_symbols = [args.readelf, '-W', '-s', elf]
        record['commands']['readelf_symbols'] = list(map(str, readelf_symbols))
        symbol_text = run_logged(readelf_symbols, out / 'readelf-symbols.log', cwd=HERE)
        assert_stack_guard(sections, symbol_text)
        section_report = {'sections': sections, 'accounting': section_accounting(sections),
                          'stack_bytes': 8192,
                          'stack_guard_address': next(
                              int(value, 16) for value in re.findall(
                                  r'^\s*\d+:\s*([0-9a-fA-F]+)\s+\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+__stack_chk_guard(?:\s|$)',
                                  symbol_text, re.M))}
        (out / 'elf-sections.json').write_text(json.dumps(section_report, indent=2) + '\n')
        record['artifacts'] = {elf.name: sha256(elf), image.name: sha256(image)}
        record['artifact_bytes'] = {elf.name: elf.stat().st_size,
                                    image.name: image.stat().st_size}
        record['artifact_size_note'] = (
            'embassy.bin is the merged image file length, including the bootloader and '
            'alignment padding emitted by espflash.')
        record['stack_bytes'] = 8192
        record['stack_guard_address'] = section_report['stack_guard_address']
        record['section_accounting'] = section_report['accounting']
        record['status'] = 'success'
        record['failure'] = None
    except Exception as exc:
        record['failure'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        provenance.write_text(json.dumps(record, indent=2) + '\n')
    return record


def main(argv=None):
    args = arguments(argv)
    try:
        build(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Embassy build failed: {exc}', file=sys.stderr)
        return 1
    print(f'EMBASSY_ARTIFACT_DIRECTORY={args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
