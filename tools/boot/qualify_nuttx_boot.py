"""Actual signed NuttX image through MCUboot into native startup/handoff.

No ELF injection, patched guest instructions, console substitution or peripheral
model changes. This gate ends after early watchdog handoff, before unsupported
camera/USB/RAM initialization; it does not qualify full kernel/app startup.
"""
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import tempfile
import time

import boot_execution as boot
import build_esp32s3 as builder
import native_inputs
import qualify as process
import sign
import target_plan as target

ROOT = target.ROOT
require = target.require
INPUTS = ('tools/boot/qualify_nuttx_boot.py', 'tests/boot/nuttx_load_layout.c',
          'tools/boot/boot_execution.py', 'tools/boot/sign.py', 'tools/boot/target_plan.py',
          'tools/boot/qualify_target.py', 'tools/build-nuttx-std-app.sh', 'tools/apply-nuttx-patches.py',
          'upstream/nuttx/release.json', 'upstream/nuttx/patches/0092-esp32s3-unlock-early-watchdog-disable.patch',
          'security/esp32s3-boot-emulator.json', '.github/workflows/esp32s3-verified-boot.yml',
          'tools/boot/test_nuttx_boot.py', 'docs/mcuboot-policy-hook.md', 'docs/verified-boot-qualification.md')
FIELDS = ('magic', 'header_bytes', 'word_bytes', 'magic_offset', 'entry_offset',
          'iram_address_offset', 'iram_flash_offset', 'iram_size_offset',
          'dram_address_offset', 'dram_flash_offset', 'dram_size_offset',
          'iram_low', 'iram_high', 'dram_low', 'dram_high')
POSITIVE = 'signed-nuttx-positive'


class Failure(ValueError):
    def __init__(self, category, events=(), pc=None):
        require(category in ('debug-timeout', 'unexpected-stop', 'code-mismatch', 'image-binding', 'execution-order'))
        self.category, self.events, self.pc = category, list(events), pc
        super().__init__(category)


def sdk_layout(boot_dir, work, environment):
    entries = json.loads((boot_dir / 'build/compile_commands.json').read_bytes())
    matches = [e for e in entries if e['file'].endswith('/image_validate.c')]
    require(len(matches) == 1)
    entry = matches[0]
    arguments = builder.compiler_command(entry)
    compiler = next((boot_dir / 'compiler-source').iterdir()) / 'bin/xtensa-esp32s3-elf-gcc'
    require(os.path.samefile(arguments[0], compiler))
    status, _ = process.run([*arguments, '-fdata-sections', '-c', str(ROOT / 'tests/boot/nuttx_load_layout.c'),
                            '-o', str(work / 'load-layout.o')], Path(entry['directory']), environment,
                           work / 'logs', 'nuttx-load-layout', 30)
    require(status == 0)
    data = work / 'load-layout.bin'
    status, _ = process.run([str(compiler.with_name('xtensa-esp32s3-elf-objcopy')),
                            '--only-section', '.rodata.nxrs_nuttx_load_layout', '-O', 'binary',
                            str(work / 'load-layout.o'), str(data)], work, environment,
                           work / 'logs', 'nuttx-load-layout-data', 30)
    require(status == 0 and data.stat().st_size == len(FIELDS) * 4)
    result = dict(zip(FIELDS, struct.unpack('<' + str(len(FIELDS)) + 'I', data.read_bytes())))
    require(result['header_bytes'] == 96 and result['word_bytes'] == 4)
    offsets = [result[name] for name in FIELDS if name.endswith('_offset')]
    require(len(set(offsets)) == len(offsets) and all(0 <= offset <= 92 and offset % 4 == 0 for offset in offsets))
    return result


def payload_geometry(body, layout, header_bytes):
    require(type(body) is bytes and len(body) >= layout['header_bytes'])
    def word(name):
        return struct.unpack_from('<I', body, layout[name])[0]
    require(word('magic_offset') == layout['magic'])
    result = {'entry': word('entry_offset')}
    for name in ('iram', 'dram'):
        address = word(name + '_address_offset')
        offset = word(name + '_flash_offset')
        size = word(name + '_size_offset')
        require(layout[name + '_low'] <= address < address + size < layout[name + '_high']
                and header_bytes + layout['header_bytes'] <= offset
                and offset - header_bytes + size <= len(body))
        result[name] = {'address': address, 'image_offset': offset, 'bytes': size}
    require(result['iram']['address'] <= result['entry'] < result['iram']['address'] + result['iram']['bytes'])
    return result


def read_elf_code(elf, address, size):
    matches = [s for s in elf.sections if s.addr <= address and address + size <= s.addr + len(s.data)]
    require(len(matches) == 1 and 0 < size <= 256)
    section = matches[0]
    return section.data[address - section.addr:address - section.addr + size]


def application_sites(application, record, work, compiler, layout, body, header_bytes, environment):
    from esptool.bin_image import ELFFile
    path = application / 'nuttx/nuttx'
    raw = native_inputs.bounded(path, 128 * 1024 * 1024)
    require(sign.sha(raw) == record['elf_sha256'] and raw[:6] == b'\x7fELF\x01\x01'
            and struct.unpack_from('<H', raw, 18)[0] == 94)
    elf = ELFFile(raw)
    status, text = process.run([str(compiler.with_name('xtensa-esp32s3-elf-nm')), '-S', str(path)],
                               work, environment, work / 'logs', 'nuttx-symbols', 30)
    require(status == 0)
    symbols = boot.symbol_table(text)
    names = {'nuttx-entry': '__start', 'nuttx-chip-start': '__esp32s3_start',
             'nuttx-wdt-entry': 'esp32s3_wdt_early_deinit', 'nuttx-after-wdt': 'esp_flash_app_init'}
    require(all(name in symbols for name in [*names.values(), 'g_idlestack']))
    sites = {kind: symbols[name][0] for kind, name in names.items()}
    geometry = payload_geometry(body, layout, header_bytes)
    require(elf.entrypoint == geometry['entry'] == sites['nuttx-entry'])
    # The entry must be physically present in the actual IRAM load segment.
    entry_size = min(symbols['__start'][1], 64)
    offset = geometry['iram']['image_offset'] - header_bytes + sites['nuttx-entry'] - geometry['iram']['address']
    require(body[offset:offset + entry_size] == read_elf_code(elf, sites['nuttx-entry'], entry_size))
    status, assembly = process.run([str(compiler.with_name('xtensa-esp32s3-elf-objdump')), '-d',
        '--disassemble=esp32s3_wdt_early_deinit', str(path)], work, environment,
        work / 'logs', 'nuttx-handoff-disassembly', 30)
    require(status == 0)
    returns = [pc for pc, op, _ in boot.instructions(assembly) if op == 'retw.n']
    require(len(returns) == 1)
    sites['nuttx-wdt-return'] = returns[0]
    codes = {kind: read_elf_code(elf, address, min(symbols[names[kind]][1], 64))
             for kind, address in sites.items() if kind in names}
    stack_low, stack_bytes = symbols['g_idlestack']
    require(layout['dram_low'] <= stack_low < stack_low + stack_bytes <= layout['dram_high'] and stack_bytes >= 1024)
    return sites, codes, geometry, {'low': stack_low, 'bytes': stack_bytes}


def check_events(label, events, regular, idle_stack):
    kinds = [event['kind'] for event in events]
    require(len(kinds) == len(set(kinds)))
    initial = ['watchdog-init', 'watchdog-hal-init', 'boot-go']
    require(all(kind in kinds for kind in initial) and [kinds.index(kind) for kind in initial] == sorted(kinds.index(kind) for kind in initial))
    if label == POSITIVE:
        required = ['hook', 'hook-return', 'stock-check', 'stock-signature', 'image-load', 'nuttx-entry',
                    'nuttx-chip-start', 'nuttx-wdt-entry', 'nuttx-wdt-return', 'nuttx-after-wdt']
        require(all(kind in kinds for kind in required) and kinds[-1] == required[-1] and 'no-image' not in kinds)
        positions = [kinds.index(kind) for kind in [*initial, *required]]
        require(positions == sorted(positions) and next(e['value'] for e in events if e['kind'] == 'hook-return') == regular)
        require(all(e.get('code_matches_elf') is True for e in events if e['kind'] in ('nuttx-entry', 'nuttx-chip-start', 'nuttx-wdt-entry', 'nuttx-after-wdt')))
        for event in events:
            if event['kind'] in ('nuttx-wdt-entry', 'nuttx-wdt-return', 'nuttx-after-wdt'):
                require(idle_stack['low'] <= event['sp'] <= idle_stack['low'] + idle_stack['bytes'])
    else:
        require(label in ('signed-nuttx-wrong-target', 'signed-nuttx-signature-corruption', 'unsigned-nuttx'))
        require(kinds[-1] == 'no-image' and 'image-load' not in kinds and not any(kind.startswith('nuttx-') for kind in kinds))
        if label == 'signed-nuttx-wrong-target':
            require('hook-return' in kinds and 'stock-check' not in kinds
                    and next(e['value'] for e in events if e['kind'] == 'hook-return') != regular)
        if label == 'signed-nuttx-signature-corruption':
            require('stock-signature' in kinds and next(e['value'] for e in events if e['kind'] == 'hook-return') == regular)
    return {'label': label, 'startup_handoff_observed': label == POSITIVE, 'events': events}


def run_image(label, image, boot_dir, work, executable, environment, mapping, sites, regular_address,
              codes, idle_stack, configuration, contract):
    flash = work / (label + '-flash.bin')
    binary = (boot_dir / 'build/mcuboot_esp32s3.bin').read_bytes()
    require(len(image) <= configuration['slot_bytes'] and len(binary) <= 0xf000)
    raw = bytearray(b'\xff' * configuration['flash_bytes'])
    raw[:len(binary)] = binary
    start = configuration['primary_offset']
    raw[start:start + len(image)] = image
    flash.write_bytes(raw)
    digest = sign.sha(raw)
    del raw
    events, regular, pc = [], None, None
    log = work / 'logs' / (label + '-emulator.log')
    with tempfile.TemporaryDirectory(prefix='nxrs-nuttx-boot-') as temporary:
        endpoint = Path(temporary) / 'socket'
        require(len(os.fsencode(endpoint)) < 100 and ',' not in str(flash))
        command = [str(executable), '-L', str(executable.parent.parent / 'share/qemu'), '-machine', 'esp32s3',
                   '-smp', '2', '-display', 'none', '-serial', 'null', '-monitor', 'none', '-nic', 'none',
                   '-no-reboot', '-snapshot', '-drive', 'file=' + str(flash) + ',if=mtd,format=raw',
                   '-S', '-gdb', 'unix:' + str(endpoint) + ',server=on,wait=off']
        with log.open('xb') as output:
            child = subprocess.Popen(command, cwd=work, env=environment, stdout=output, stderr=output,
                                     start_new_session=True, close_fds=True)
            try:
                deadline = time.monotonic() + 30
                while not endpoint.exists():
                    require(child.poll() is None and time.monotonic() < deadline and log.stat().st_size < 1048576)
                    time.sleep(.02)
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(endpoint))
                    remote = boot.Remote(connection, deadline)
                    require(b'PacketSize=' in remote.packet('qSupported:multiprocess+'))
                    require(remote.packet('?').startswith(b'T05thread:p01.01;'))
                    boot_points = {kind: address for kind, address in sites.items() if not kind.startswith('nuttx-')}
                    require(len(set(boot_points.values())) == len(boot_points))
                    remaining = {address: kind for kind, address in boot_points.items()}
                    for address in remaining:
                        require(remote.packet(f'Z0,{address:x},3') == b'OK')
                    for _ in range(len(sites)):
                        try:
                            stopped = remote.packet('c')
                        except (socket.timeout, TimeoutError):
                            pc = None
                            try:
                                remote.interrupt()
                                require(remote.packet('Hgp01.01') == b'OK')
                                pc = remote.register(mapping['pc'])
                            except (OSError, ValueError):
                                pass
                            raise Failure('debug-timeout', events, pc)
                        require(stopped.startswith(b'T05thread:p01.01;'))
                        pc = remote.register(mapping['pc'])
                        if pc not in remaining:
                            raise Failure('unexpected-stop', events, pc)
                        kind = remaining.pop(pc)
                        event = {'kind': kind, 'pc': pc, 'sp': remote.window(1, mapping)}
                        if kind == 'watchdog-hal-init':
                            event['instance'] = remote.window(11, mapping)
                            require(event['instance'] == contract['watchdog_instance'])
                        if kind == 'hook-return':
                            event['value'] = remote.window(2, mapping)
                            regular = remote.memory(regular_address)
                        if kind == 'image-load':
                            address = sites['nuttx-entry']
                            require(address not in remaining and remote.packet(f'Z0,{address:x},3') == b'OK')
                            remaining[address] = 'nuttx-entry'
                        if kind == 'nuttx-entry':
                            # Loaded application RAM can replace bootloader code;
                            # install each phase's breakpoints only after ownership changes.
                            for address in tuple(remaining):
                                require(remote.packet(f'z0,{address:x},3') == b'OK')
                                del remaining[address]
                            app_points = {kind: address for kind, address in sites.items()
                                          if kind.startswith('nuttx-') and kind != 'nuttx-entry'}
                            require(len(set(app_points.values())) == len(app_points))
                            for app_kind, address in app_points.items():
                                require(remote.packet(f'Z0,{address:x},3') == b'OK')
                                remaining[address] = app_kind
                        if kind in codes:
                            expected = codes[kind]
                            value = remote.packet(f'm{pc:x},{len(expected):x}')
                            if value != expected.hex().encode():
                                raise Failure('code-mismatch', events, pc)
                            event['code_matches_elf'] = True
                        events.append(event)
                        if kind in ('no-image', 'nuttx-after-wdt'):
                            if regular is None:
                                regular = remote.memory(regular_address)
                            break
                        require(child.poll() is None and log.stat().st_size < 1048576)
                        require(remote.packet(f'z0,{pc:x},3') == b'OK')
            except Failure:
                raise
            except (ValueError, OSError) as error:
                raise Failure('unexpected-stop', events, pc) from error
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
                require(process.digest(flash) == digest)
    try:
        result = check_events(label, events, regular, idle_stack)
    except ValueError as error:
        raise Failure('execution-order', events, pc) from error
    result.update(image_sha256=sign.sha(image), flash_sha256=digest, input_flash_unchanged=True)
    return result


def qualify(boot_dir, application, settings, pin, signing, work, execution_dir, environment):
    work.mkdir(mode=0o700)
    (work / 'logs').mkdir(mode=0o700)
    inputs = {name: process.digest(ROOT / name) for name in INPUTS}
    boot_record = json.loads((boot_dir / 'boot-build.json').read_bytes())
    record_path = application / 'payload-build.json'
    record = json.loads(record_path.read_bytes())
    body = sign.untrusted_read(application / 'payload.bin', settings['layout']['slot_bytes'])
    require(record['policy_sha256'] == pin == boot_record['public_policy_sha256']
            and record['configuration_sha256'] == settings['configuration_sha256'] == boot_record['configuration_sha256']
            and record['payload_sha256'] == sign.sha(body) and record['scope'] == 'unsigned-application-build-only')
    layout = sdk_layout(boot_dir, work, environment)
    compiler = next((boot_dir / 'compiler-source').iterdir()) / 'bin/xtensa-esp32s3-elf-gcc'
    app_sites, codes, geometry, idle_stack = application_sites(application, record, work, compiler,
                                                          layout, body, settings['layout']['header_bytes'], environment)
    base_sites, regular, _ = boot.boot_sites(boot_dir, work, environment)
    table = boot.symbol_table((work / 'logs/boot-symbols.out').read_text())
    require('esp_app_image_load' in table)
    sites = dict(base_sites, **app_sites, **{'image-load': table['esp_app_image_load'][0]})
    public, policy, key = signing
    metadata = sign.manifest(policy, pin, body, '2.3.4+5', 7)
    valid, _ = sign.produce(body, metadata, policy, key)
    sign.verify_image(valid, public, pin, settings['target'], settings['configuration_sha256'], 1)
    bad = bytearray(valid)
    bad[-1] ^= 1
    metadata = dict(metadata, target='other-camera')
    wrong, _ = sign.produce(body, metadata, policy, key)
    execution_record_path = execution_dir.parent / 'boot-execution-qualification.json'
    execution_record_hash = process.digest(execution_record_path)
    previous = json.loads(execution_record_path.read_bytes())
    require(previous['qualified'] and previous['target_hook_executed'] and previous['public_policy_sha256'] == pin
            and previous['boot_build_sha256'] == process.digest(boot_dir / 'boot-build.json'))
    executable = execution_dir / 'emulator/qemu-source/qemu/bin/qemu-system-xtensa'
    rom = executable.parent.parent / 'share/qemu/esp32s3_rev0_rom.bin'
    require(executable.is_file() and process.digest(executable) == previous['emulator']['executable_sha256']
            and process.digest(rom) == previous['emulator']['rom_sha256'])
    mapping = previous['emulator']['register_mapping']
    require(mapping == {'pc': 0, 'windowbase': 69, 'ar0': 1})
    env = dict(environment, HOME=str(work))
    # This Linux-only stage uses the same pinned emulator already authenticated
    # by the mandatory executable-corpus qualification, with no inherited loaders.
    require(not any(name.startswith(('DYLD_', 'LD_')) for name in env))
    corpus = [(POSITIVE, valid), ('signed-nuttx-wrong-target', wrong),
              ('signed-nuttx-signature-corruption', bytes(bad)), ('unsigned-nuttx', body)]
    observations = []
    try:
        for label, image in corpus:
            observations.append(run_image(label, image, boot_dir, work, executable, env,
                mapping, sites, regular, codes, idle_stack, settings['layout'], boot_record['runtime_contract']))
            print('Actual NuttX startup/handoff case passed: ' + label, flush=True)
    except Failure as error:
        diagnostic = {'schema': 1, 'scope': 'signed-nuttx-startup-handoff-failure',
                      'category': error.category, 'events': error.events, 'pc': error.pc,
                      'qualified': False, 'deployment_permitted': False}
        (work / 'failure.json').write_bytes(target.canonical(diagnostic) + b'\n')
        print('Signed NuttX boot refused: ' + error.category + '; observed=' + ','.join(e['kind'] for e in error.events), flush=True)
        if error.pc is not None and 0 <= error.pc <= 0xffffffff:
            print('Signed NuttX stopped PC: 0x' + f'{error.pc:08x}', flush=True)
        raise
    require(process.digest(executable) == previous['emulator']['executable_sha256']
            and process.digest(rom) == previous['emulator']['rom_sha256']
            and process.digest(execution_record_path) == execution_record_hash
            and process.digest(boot_dir / 'build/mcuboot_esp32s3.bin') == boot_record['binary_sha256']
            and process.digest(boot_dir / 'build/mcuboot_esp32s3.elf') == boot_record['elf_sha256'])
    require(inputs == {name: process.digest(ROOT / name) for name in INPUTS})
    require(record == json.loads(record_path.read_bytes()) and process.digest(application / 'payload.bin') == record['payload_sha256'])
    status, head = process.run(['git', 'rev-parse', 'HEAD'], ROOT, environment, work / 'logs', 'repository-head', 10)
    require(status == 0)
    status, changes = process.run(['git', 'status', '--porcelain'], ROOT, environment, work / 'logs', 'repository-status', 10)
    require(status == 0)
    return {'schema': 1, 'scope': 'actual-signed-nuttx-image-native-startup-and-watchdog-handoff', 'qualified': True,
            'repository_head': head.strip(), 'repository_has_changes': bool(changes.strip()), 'inputs': inputs,
            'boot_build_sha256': process.digest(boot_dir / 'boot-build.json'),
            'application_build_sha256': process.digest(record_path), 'boot_execution_sha256': execution_record_hash, 'elf_sha256': record['elf_sha256'],
            'payload_sha256': record['payload_sha256'], 'public_policy_sha256': pin,
            'native_load_header_layout': layout, 'payload_geometry': geometry, 'idle_stack': idle_stack,
            'observations': observations, 'signed_nuttx_entry_observed': True, 'native_watchdog_handoff_executed': True,
            'network_disabled': True, 'snapshot_flash': True, 'physical_qualified': False,
            'rtc_watchdog_effect_qualified': False, 'protected_device_counter': False,
            'full_kernel_startup_qualified': False, 'application_completed': False, 'deployment_permitted': False}
