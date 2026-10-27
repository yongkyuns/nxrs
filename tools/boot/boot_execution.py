"""Actual ESP32-S3 MCUboot execution in a private, network-disabled emulator.

A debugger observes the unmodified production ELF, its Rust policy return and
stock signature path, then an executable signed Rust application's entry/write.
No ROM fuse, protected-counter, full-stack or physical qualification is implied.
"""
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import tempfile
import time

import build_esp32s3 as builder
import qualify as process
import qualify_admission as admission
import boot_resources as resources
import sign
import target_plan as target

ROOT = target.ROOT
SPEC = "security/esp32s3-boot-emulator.json"
INPUTS = (SPEC, "tools/boot/boot_execution.py", "tools/boot/qualify_target.py",
          "tools/boot/qualify_admission.py", "tools/boot/sign.py", "tools/boot/qualify.py",
          "tests/boot/executable.rs", "tests/boot/executable.ld", "tests/boot/load_header.c",
          "tools/boot/test_boot_execution.py", "tools/boot/boot_resources.py", "tests/boot/runtime_layout.c", ".github/workflows/esp32s3-verified-boot.yml",
          "docs/mcuboot-policy-hook.md", "docs/verified-boot-qualification.md", "tools/boot/README.md")
# Trailing bytes are unused slot data, not part of the exact signed container.
ACCEPT = {"offline-signer-positive", "older-version-permitted-by-security-floor", "trailing-preconfirmation", "maximum-size-positive"}
SKIP = {"stored-security-floor": "no-protected-target-counter-backend"}


require = target.require


class Remote:
    """Bounded synchronous GDB RSP over an owner-private Unix socket."""
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def read(self, size):
        output = bytearray()
        while len(output) < size:
            remaining = self.deadline - time.monotonic()
            require(remaining > 0)
            self.connection.settimeout(min(remaining, 10))
            part = self.connection.recv(size - len(output))
            require(part)
            output.extend(part)
        return bytes(output)

    @staticmethod
    def decode(raw):
        decoded = bytearray()
        index = 0
        while index < len(raw):
            value = raw[index]
            index += 1
            if value == ord('}'):
                require(index < len(raw))
                decoded.append(raw[index] ^ 0x20)
                index += 1
            elif value == ord('*'):
                require(decoded and index < len(raw) and 32 <= raw[index] <= 126)
                count = raw[index] - 29
                require(count >= 3)
                decoded.extend(bytes([decoded[-1]]) * count)
                index += 1
            else:
                decoded.append(value)
            require(len(decoded) <= 8192)
        return bytes(decoded)

    def packet(self, command):
        raw = command.encode('ascii')
        require(len(raw) <= 4096 and not any(c in raw for c in b'$#}*'))
        self.connection.sendall(b'$' + raw + b'#' + f'{sum(raw) % 256:02x}'.encode())
        require(self.read(1) == b'+')
        return self.receive()

    def receive(self):
        require(self.read(1) == b'$')
        output = bytearray()
        while True:
            char = self.read(1)
            if char == b'#':
                break
            output.extend(char)
            require(len(output) <= 8192)
        check = self.read(2)
        require(re.fullmatch(rb'[0-9a-fA-F]{2}', check)
                and int(check, 16) == sum(output) % 256)
        self.connection.sendall(b'+')
        return self.decode(output)

    def interrupt(self):
        # Bounded failure diagnosis only; never turns a timed-out run into a pass.
        self.deadline = time.monotonic() + 3
        self.connection.sendall(b'\x03')
        return self.receive()

    def word(self, command):
        raw = self.packet(command)
        require(re.fullmatch(rb'[0-9a-f]{8}', raw))
        return int.from_bytes(bytes.fromhex(raw.decode()), 'little')

    def memory(self, address):
        return self.word(f'm{address:x},4')

    def register(self, number):
        return self.word(f'p{number:x}')

    def window(self, number, mapping):
        base = self.register(mapping['windowbase'])
        require(0 <= base < 16 and 0 <= number < 16)
        return self.register(mapping['ar0'] + (base * 4 + number) % 64)


def register_mapping(text):
    # The pin binds this table to the exact binary release and gdbstub behavior.
    values = {}
    for match in re.finditer(r'^\s*XTREG\(([^\n]+)', text, re.M):
        fields = [field.strip() for field in match[1].split(',')]
        if len(fields) >= 11 and fields[10] in ('pc', 'windowbase', 'ar0', 'ar63'):
            values[fields[10]] = (int(fields[0]), int(fields[3]), int(fields[8]))
    require(values == {'pc': (0, 4, 9), 'ar0': (1, 4, 1), 'ar63': (64, 4, 1),
                       'windowbase': (69, 4, 2)})
    return {name: values[name][0] for name in ('pc', 'windowbase', 'ar0')}


def instructions(text):
    result = []
    for line in text.splitlines():
        match = re.fullmatch(r'\s*([0-9a-f]+):\s+[0-9a-f]+\s+([a-z0-9.]+)(?:\s+(.*))?', line)
        if match:
            result.append((int(match[1], 16), match[2], match[3] or ''))
    require(result and len({row[0] for row in result}) == len(result))
    return result


def fail_loop(main):
    calls = [pc for pc, op, arg in main if op.startswith('call') and '<boot_go>' in arg]
    require(len(calls) == 1)
    loops = [pc for pc, op, arg in main if op == 'j' and pc > calls[0]
             and re.match(r'^' + f'{pc:x}' + r'\s', arg)]
    # The first post-boot_go panic is the actual no-bootable-image branch;
    # the later one is an unexpected return from do_boot.
    require(len(loops) == 2)
    return loops[0]


def install(work, cache, environment, library_path):
    folder = work / 'emulator'
    folder.mkdir(mode=0o700)
    spec = json.loads((ROOT / SPEC).read_bytes())
    require(spec['schema'] == 1)
    entry = spec['hosts'][platform.system() + '-' + platform.machine()]
    runtime = builder.archive(entry, folder, 'qemu', cache, 128 * 1024 * 1024)
    source = folder / 'source'
    source.mkdir(mode=0o700)
    require(spec['capabilities'] == {'rtc_watchdog': False})
    for value in [*spec['debugger_sources'].values(), *spec['peripheral_sources'].values()]:
        process.fetch(value, source)
    mapping = register_mapping((source / 'target_xtensa_core-esp32s3_gdb-config.inc.c').read_text())
    executable = runtime / 'bin/qemu-system-xtensa'
    env = dict(environment, HOME=str(work / 'home'))
    if library_path is not None:
        require(platform.system() == 'Darwin')
        # Explicit operator-selected private dependencies, never inherited DYLD_*.
        parts = library_path.split(':')
        require(parts and all(Path(p).is_absolute() and Path(p).is_dir() for p in parts))
        env['DYLD_LIBRARY_PATH'] = library_path
    status, version = process.run([str(executable), '--version'], work, env, work / 'logs', 'qemu-version', 30)
    require(status == 0 and 'QEMU emulator version ' + spec['version'] + '\n' in version)
    return executable, env, mapping, {
        'version': version.splitlines()[0], 'archive': entry,
        'executable_sha256': process.digest(executable),
        'rom_sha256': process.digest(runtime / 'share/qemu/esp32s3_rev0_rom.bin'),
        'debugger_sources': spec['debugger_sources'], 'register_mapping': mapping,
        'peripheral_sources': spec['peripheral_sources'], 'capabilities': spec['capabilities'],
        'host_dynamic_dependency_advisory_closure': False,
    }


def build_payload(boot, work, report, settings, environment):
    compiler = next((boot / 'compiler-source').iterdir()) / 'bin/xtensa-esp32s3-elf-gcc'
    rust = boot / 'rust/runtime/bin/rustc'
    require(process.digest(rust) == report['rust_build']['rustc_sha256'])
    deps = boot / 'rust/target/xtensa-esp32s3-none-elf/release/deps'
    libraries = {}
    for name in ('core', 'compiler_builtins'):
        candidates = list(deps.glob('lib' + name + '-*.rlib'))
        require(len(candidates) == 1)
        component = next(c for c in report['rust_build']['components'] if c['name'] == name)
        require(process.digest(candidates[0]) == component['artifact_sha256'])
        libraries[name] = candidates[0]
    env = dict(environment, RUSTC_BOOTSTRAP='1')
    def run(command, label):
        status, output = process.run([str(arg) for arg in command], work, env, work / 'logs', label, 120)
        require(status == 0)
        return output
    command = [rust, ROOT / 'tests/boot/executable.rs', '--edition', '2021', '--crate-name', 'nxrs_boot_execution',
               '--crate-type', 'staticlib', '--target', 'xtensa-esp32s3-none-elf',
               '-L', 'dependency=' + str(deps), '-C', 'panic=abort', '-C', 'opt-level=z', '-C', 'lto=fat']
    for name, path in libraries.items():
        command += ['--extern', name + '=' + str(path)]
    run([*command, '-o', work / 'application.a'], 'application-rust')
    run([compiler, '-nostdlib', '-Wl,--gc-sections', '-T', ROOT / 'tests/boot/executable.ld',
         work / 'application.a', '-lgcc', '-o', work / 'application.elf'], 'application-link')
    nm = compiler.with_name('xtensa-esp32s3-elf-nm')
    objdump = compiler.with_name('xtensa-esp32s3-elf-objdump')
    objcopy = compiler.with_name('xtensa-esp32s3-elf-objcopy')
    syms = symbol_table(run([nm, '-S', work / 'application.elf'], 'application-symbols'))
    section_text = run([objdump, '-h', work / 'application.elf'], 'application-sections')
    sections = {}
    for match in re.finditer(r'^\s*\d+\s+[.](iram|dram|bss)\s+([0-9a-f]+)\s+([0-9a-f]+)\s', section_text, re.M):
        require(match[1] not in sections)
        sections[match[1]] = (int(match[2], 16), int(match[3], 16))
    require(set(sections) <= {'iram', 'dram', 'bss'} and {'iram', 'dram'} <= set(sections)
            and sections.get('bss', (0, 0))[0] == 0)
    segments = []
    for name, low in (('iram', 0x40378000), ('dram', 0x3fc90000)):
        size, address = sections[name]
        require(address == low and 0 < size <= 65536 and size % 4 == 0)
        run([objcopy, '-O', 'binary', '--only-section=.' + name, work / 'application.elf', work / (name + '.bin')], 'extract-' + name)
        raw = (work / (name + '.bin')).read_bytes()
        require(len(raw) == size)
        segments.append(raw)
    entry = syms['fixture_entry'][0]
    park = syms['fixture_park'][0]
    marker = syms['NXRS_ENTRY_WITNESS'][0]
    require(sections['iram'][1] <= entry < sections['iram'][1] + sections['iram'][0]
            and sections['iram'][1] <= park < sections['iram'][1] + sections['iram'][0]
            and sections['dram'][1] <= marker <= sections['dram'][1] + sections['dram'][0] - 4)
    source = next((boot / 'mcuboot-source').iterdir())
    defines = {'NXRS_ENTRY': entry, 'NXRS_IRAM_ADDRESS': sections['iram'][1], 'NXRS_IRAM_BYTES': len(segments[0]),
               'NXRS_DRAM_ADDRESS': sections['dram'][1], 'NXRS_DRAM_BYTES': len(segments[1]),
               'NXRS_MCUBOOT_HEADER': settings['layout']['header_bytes']}
    run([compiler, '-c', '-I' + str(source / 'boot/espressif/hal/include'),
         *['-D' + key + '=' + str(value) for key, value in defines.items()],
         ROOT / 'tests/boot/load_header.c', '-o', work / 'load-header.o'], 'load-header')
    run([objcopy, '-O', 'binary', '--only-section=.load_header', work / 'load-header.o', work / 'load-header.bin'], 'extract-header')
    header = (work / 'load-header.bin').read_bytes()
    require(len(header) == 96)
    body = header + b''.join(segments)
    (work / 'payload.bin').write_bytes(body)
    return body, {'entry': entry, 'park': park, 'marker': marker,
                  'payload_sha256': sign.sha(body), 'elf_sha256': process.digest(work / 'application.elf'),
                  'core_archives': {name: process.digest(path) for name, path in libraries.items()}}


def symbol_table(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([0-9a-f]+) ([0-9a-f]+) [TDBR] ([A-Za-z0-9_.$]+)', line)
        if match:
            require(match[3] not in result)
            result[match[3]] = (int(match[1], 16), int(match[2], 16))
    return result


def absolute_symbol(text, name):
    matches = re.findall(r'^([0-9a-f]+)(?: [0-9a-f]+)? A ' + re.escape(name) + r'$', text, re.M)
    require(len(matches) == 1)
    return int(matches[0], 16)


def boot_sites(boot, work, environment):
    compiler = next((boot / 'compiler-source').iterdir()) / 'bin/xtensa-esp32s3-elf-objdump'
    elf = boot / 'build/mcuboot_esp32s3.elf'
    def run(command, label):
        status, output = process.run([str(arg) for arg in command], work, environment, work / 'logs', label, 30)
        require(status == 0)
        return output
    table = run([compiler.with_name('xtensa-esp32s3-elf-nm'), '-S', elf], 'boot-symbols')
    syms = symbol_table(table)
    hook = instructions(run([compiler, '-d', '--disassemble=boot_image_check_hook', elf], 'boot-hook-disassembly'))
    returns = [pc for pc, op, arg in hook if op == 'retw.n']
    require(len(returns) == 1 and hook[0][0] == syms['boot_image_check_hook'][0])
    main = instructions(run([compiler, '-d', '--disassemble=main', elf], 'boot-main-disassembly'))
    return {'watchdog-init': syms['bootloader_config_wdt'][0], 'watchdog-hal-init': syms['wdt_hal_init'][0],
            'boot-go': syms['boot_go'][0], 'hook': syms['boot_image_check_hook'][0], 'hook-return': returns[0],
            'watchdog-progress': syms['nxrs_boot_watchdog_feed'][0],
            'stock-check': syms['boot_check_image'][0], 'stock-signature': syms['bootutil_verify_sig'][0],
            'no-image': fail_loop(main)}, syms['FIH_BOOT_HOOK_REGULAR'][0], absolute_symbol(table, '_dram_end')


def check_observation(label, events, regular):
    kinds = [event['kind'] for event in events]
    require(len(kinds) == len(set(kinds)) and kinds and kinds[-1] in ('park', 'no-image'))
    require(kinds.index('watchdog-init') < kinds.index('watchdog-hal-init') < kinds.index('boot-go')
            < len(kinds) - 1)
    accepted = label in ACCEPT
    require(('park' in kinds) == accepted and ('entry' in kinds) == accepted)
    if accepted:
        require(kinds.index('hook') < kinds.index('hook-return') < kinds.index('stock-check')
                < kinds.index('stock-signature') < kinds.index('entry') < kinds.index('park'))
        require(next(e['value'] for e in events if e['kind'] == 'hook-return') == regular)
        require(events[-1]['marker'] == 0x4e585253)
        require(next(e['marker'] for e in events if e['kind'] == 'entry') == 0x11223344)
        require('watchdog-progress' in kinds)
    if label.startswith('signed-wrong-') and label != 'signed-wrong-pss-salt':
        require('hook' in kinds and 'hook-return' in kinds and 'stock-check' not in kinds)
        require(next(e['value'] for e in events if e['kind'] == 'hook-return') != regular)
    if label in ('signature-corruption', 'signed-wrong-pss-salt', 'maximum-size-signature-corruption'):
        require('stock-signature' in kinds and 'hook-return' in kinds)
        require(next(e['value'] for e in events if e['kind'] == 'hook-return') == regular)
    return {'label': label, 'accepted': accepted, 'events': events}


def execute(label, image, boot, work, executable, environment, mapping, sites, regular_address, witness, layout,
            contract, data_end, *, corrupt_stack_guard=False):
    path = work / (label + '-flash.bin')
    raw = bytearray(b'\xff' * layout['flash_bytes'])
    binary = (boot / 'build/mcuboot_esp32s3.bin').read_bytes()
    require(len(binary) <= 0xf000 and len(image) <= layout['slot_bytes'])
    raw[:len(binary)] = binary
    offset = layout['primary_offset']
    raw[offset:offset + len(image)] = image
    path.write_bytes(raw)
    before = sign.sha(raw)
    del raw
    events = []
    stack = None
    stack_result = None
    log_path = work / 'logs' / (label + '-emulator.log')
    with tempfile.TemporaryDirectory(prefix='nxrs-boot-gdb-') as temporary:
        endpoint = Path(temporary) / 'socket'
        require(len(os.fsencode(endpoint)) < 100 and ',' not in str(path))
        command = [str(executable), '-L', str(executable.parent.parent / 'share/qemu'), '-machine', 'esp32s3',
                   '-smp', '2', '-display', 'none', '-serial', 'null', '-monitor', 'none', '-nic', 'none',
                   '-no-reboot', '-snapshot', '-drive', 'file=' + str(path) + ',if=mtd,format=raw',
                   '-S', '-gdb', 'unix:' + str(endpoint) + ',server=on,wait=off']
        with log_path.open('xb') as log:
            child = subprocess.Popen(command, cwd=work, env=environment, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True)
            try:
                deadline = time.monotonic() + 20
                while not endpoint.exists():
                    require(child.poll() is None and time.monotonic() < deadline and log_path.stat().st_size < 1048576)
                    time.sleep(.02)
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(endpoint))
                    remote = Remote(connection, deadline)
                    require(b'PacketSize=' in remote.packet('qSupported:multiprocess+'))
                    require(remote.packet('?').startswith(b'T05thread:p01.01;'))
                    points = dict(sites, entry=witness['entry'], park=witness['park'])
                    require(len(set(points.values())) == len(points))
                    reverse = {address: kind for kind, address in points.items()}
                    for address in points.values():
                        require(remote.packet(f'Z0,{address:x},3') == b'OK')
                    regular = None
                    for _ in range(len(points)):
                        stop = remote.packet('c')
                        require(stop.startswith(b'T05thread:p01.01;'))
                        pc = remote.register(mapping['pc'])
                        require(pc in reverse and child.poll() is None and log_path.stat().st_size < 1048576)
                        kind = reverse.pop(pc)
                        sp = remote.window(1, mapping)
                        require(contract['stack_top'] - contract['stack_bytes'] <= sp <= contract['stack_top'])
                        event = {'kind': kind, 'pc': pc, 'sp': sp}
                        if kind == 'watchdog-hal-init':
                            event['instance'] = remote.window(11, mapping)
                            require(event['instance'] == contract['watchdog_instance'])
                        if kind == 'boot-go':
                            stack = resources.StackProbe(remote, contract, sp, data_end)
                            if corrupt_stack_guard:
                                stack.write(stack.low, bytes([stack.paint[0] ^ 1]) + stack.paint[1:4])
                        if kind == 'hook-return':
                            event['value'] = remote.window(2, mapping)
                            regular = remote.memory(regular_address)
                        if kind in ('entry', 'park'):
                            event['marker'] = remote.memory(witness['marker'])
                        events.append(event)
                        if kind in ('park', 'no-image'):
                            require(stack is not None)
                            stack_result = stack.finish()
                            break
                        require(remote.packet(f'z0,{pc:x},3') == b'OK')
                    require(events and events[-1]['kind'] in ('park', 'no-image'))
                    # If an invalid header prevented the callback, fetch the
                    # SDK value now from the already initialized real boot image.
                    if regular is None:
                        regular = remote.memory(regular_address)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
                require(process.digest(path) == before)
    result = check_observation(label, events, regular)
    result.update(image_sha256=sign.sha(image), flash_sha256=before, input_flash_unchanged=True,
                  stack_observation=stack_result)
    return result


def qualify(boot, settings, pin, signing, work, cache=None, library_path=None):
    work.mkdir(mode=0o700)
    for name in ('logs', 'home', 'images'):
        (work / name).mkdir(mode=0o700)
    inputs = {name: process.digest(ROOT / name) for name in INPUTS}
    record = boot / 'boot-build.json'
    report = json.loads(record.read_bytes())
    require(report['boot_policy_hook_linked'] and report['cross_build_qualified']
            and report['public_policy_sha256'] == pin and report['configuration_sha256'] == settings['configuration_sha256']
            and process.digest(boot / 'build/mcuboot_esp32s3.bin') == report['binary_sha256']
            and process.digest(boot / 'build/mcuboot_esp32s3.elf') == report['elf_sha256'])
    environment = {'PATH': os.environ['PATH'], 'HOME': str(work / 'home'), 'PYTHONNOUSERSITE': '1'}
    body, witness = build_payload(boot, work, report, settings, environment)
    _, labels, _ = admission.fixtures(work / 'images', settings['target'], settings=settings, body=body, signing=signing)
    maximum = resources.maximum_fixture(body, signing, settings, pin, work / 'images')
    labels.extend(('maximum-size-positive', 'maximum-size-signature-corruption'))
    executable, env, mapping, emulator = install(work, cache, environment, library_path)
    sites, regular, data_end = boot_sites(boot, work, environment)
    contract = report['runtime_contract']
    require(contract['sdk_watchdog_enabled'] and contract['restored_positive'])
    observations = []
    for label in labels:
        if label in SKIP:
            continue
        observations.append(execute(label, (work / 'images' / (label + '.bin')).read_bytes(),
                                    boot, work, executable, env, mapping, sites, regular, witness, settings['layout'], contract, data_end))
        print('Emulated boot case passed: ' + label, flush=True)
    require(len(observations) == len(labels) - len(SKIP) and ACCEPT <= set(labels))
    control = work / 'stack-guard-control'
    control.mkdir(mode=0o700)
    (control / 'logs').mkdir(mode=0o700)
    guard_refused = False
    try:
        execute('offline-signer-positive', (work / 'images/offline-signer-positive.bin').read_bytes(),
                boot, control, executable, env, mapping, sites, regular, witness, settings['layout'],
                contract, data_end, corrupt_stack_guard=True)
    except resources.StackGuardError:
        guard_refused = True
    require(guard_refused)
    # A fresh unmodified emulator must still execute the positive after refusal.
    restored = work / 'stack-guard-restored'
    restored.mkdir(mode=0o700)
    (restored / 'logs').mkdir(mode=0o700)
    restoration = execute('offline-signer-positive', (work / 'images/offline-signer-positive.bin').read_bytes(),
                         boot, restored, executable, env, mapping, sites, regular, witness, settings['layout'],
                         contract, data_end)
    require(restoration['accepted'])
    require(process.digest(executable) == emulator['executable_sha256']
            and process.digest(executable.parent.parent / 'share/qemu/esp32s3_rev0_rom.bin') == emulator['rom_sha256']
            and process.digest(boot / 'build/mcuboot_esp32s3.bin') == report['binary_sha256']
            and process.digest(boot / 'build/mcuboot_esp32s3.elf') == report['elf_sha256'])
    require(inputs == {name: process.digest(ROOT / name) for name in INPUTS})
    require(process.digest(record) == sign.sha(target.canonical(report) + b'\n'))
    status, head = process.run(['git', 'rev-parse', 'HEAD'], ROOT, environment, work / 'logs', 'repository-head', 10)
    require(status == 0)
    status, changes = process.run(['git', 'status', '--porcelain'], ROOT, environment, work / 'logs', 'repository-status', 10)
    require(status == 0)
    return {'schema': 1, 'scope': 'actual-esp32s3-emulated-production-boot-execution', 'qualified': True,
            'repository_head': head.strip(), 'repository_has_changes': bool(changes.strip()), 'inputs': inputs,
            'boot_build_sha256': process.digest(record), 'bootloader_sha256': report['binary_sha256'],
            'public_policy_sha256': pin, 'emulator': emulator, 'witness': witness,
            'observations': observations, 'skipped': SKIP, 'target_hook_executed': True,
            'maximum_fixture': maximum, 'runtime_contract': contract, 'stack_guard_corruption_refused': guard_refused,
            'restored_stack_positive': restoration, 'watchdog_sdk_initialization_observed': True,
            'rtc_watchdog_reset_model_supported': emulator['capabilities']['rtc_watchdog'],
            'stock_signature_execution_observed': True, 'signed_application_write_observed': True,
            'network_disabled': True, 'snapshot_flash': True, 'physical_qualified': False,
            'rom_authenticated': False, 'protected_device_counter': False, 'boot_stack_qualified': False,
            'watchdog_timing_qualified': False, 'activation_recovery_qualified': False, 'deployment_permitted': False}
