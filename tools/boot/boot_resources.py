"""Exact SDK configuration and bounded boot-resource observations; no device I/O."""
from pathlib import Path
import os
import struct

import native_inputs
import qualify as process
import target_plan as target

ROOT = target.ROOT
require = target.require
MIN_STACK_GUARD = 1024


def watchdog_defines(text):
    values = native_inputs.defines(text)
    required = {'CONFIG_ESP_MCUBOOT_WDT_ENABLE': '1', 'CONFIG_BOOTLOADER_WDT_ENABLE': '1',
                'CONFIG_BOOTLOADER_WDT_TIME_MS': '9000'}
    require(all(values.get(name) == value for name, value in required.items()))
    return required


def sdk_contract(work, source, compiler, entry, arguments, environment):
    require(entry['file'].endswith('/components/bootloader_support/src/bootloader_init.c'))
    logs = work / 'logs'
    def preprocess(label, extra=()):
        status, output = process.run([*arguments, *extra, '-dM', '-E', entry['file']],
                                     Path(entry['directory']), environment, logs, label, 30)
        require(status == 0)
        return output
    positive = watchdog_defines(preprocess('runtime-defines'))
    controls = []
    for label, extra in [('unset-product-watchdog', ['-UCONFIG_ESP_MCUBOOT_WDT_ENABLE']),
                         ('disabled-product-watchdog', ['-UCONFIG_ESP_MCUBOOT_WDT_ENABLE', '-DCONFIG_ESP_MCUBOOT_WDT_ENABLE=0'])]:
        text = preprocess(label, extra)
        try:
            watchdog_defines(text)
        except ValueError:
            controls.append(label)
        else:
            require(False)
    # Reproduce the actual defect with the real, otherwise identical SDK header.
    header = source / 'boot/espressif/hal/include/esp32s3/sdkconfig.h'
    original = header.read_bytes()
    mapping = b'#define CONFIG_BOOTLOADER_WDT_ENABLE 1'
    require(original.count(mapping) == 1)
    try:
        header.write_bytes(original.replace(mapping, b'/* actual SDK watchdog mapping removed */'))
        refused = False
        try:
            watchdog_defines(preprocess('unmapped-sdk-watchdog'))
        except ValueError:
            refused = True
        require(refused)
        controls.append('actual-sdk-mapping-removed')
    finally:
        header.write_bytes(original)
    require(header.read_bytes() == original and watchdog_defines(preprocess('restored-runtime-defines')) == positive)
    obj = work / 'runtime-layout.o'
    status, _ = process.run([*arguments, '-fdata-sections', '-c', str(ROOT / 'tests/boot/runtime_layout.c'), '-o', str(obj)],
                            Path(entry['directory']), environment, logs, 'runtime-layout', 30)
    require(status == 0)
    data = work / 'runtime-layout.bin'
    status, _ = process.run([str(compiler / 'bin/xtensa-esp32s3-elf-objcopy'), '--only-section', '.rodata.nxrs_runtime_layout',
                            '-O', 'binary', str(obj), str(data)], work, environment, logs, 'runtime-layout-data', 30)
    require(status == 0 and data.stat().st_size == 48)
    fields = ('stack_top', 'stack_bytes', 'watchdog_timeout_ms', 'watchdog_config_address', 'watchdog_enable_mask',
              'watchdog_stage0_mask', 'watchdog_stage0_shift', 'watchdog_reset_rtc_action', 'watchdog_hold_address', 'watchdog_instance', 'watchdog_protect_address', 'watchdog_unlock_key')
    result = dict(zip(fields, struct.unpack('<12I', data.read_bytes())))
    require(result['stack_top'] == 0x3fceb710 and result['stack_bytes'] == 8192
            and result['watchdog_timeout_ms'] == 9000)
    result.update(schema=1, scope='actual-esp32s3-sdk-headers-and-bootloader-init-configuration',
                  source_sha256=process.digest(Path(entry['file'])), effective_defines=positive,
                  actual_refusal_controls=controls, restored_positive=True,
                  sdk_watchdog_enabled=True, hardware_reset_qualified=False, full_stack_bound_qualified=False)
    return result


class StackGuardError(ValueError):
    """Only an actual modified reserved guard can produce this refusal."""


class StackProbe:
    """Write high-water observation from boot_go to terminal; not a static bound."""
    def __init__(self, remote, contract, sp, data_end):
        top, size = contract['stack_top'], contract['stack_bytes']
        self.low = top - size
        self.high = (sp - 64) & ~3
        # Preserve the active frame and Xtensa window spill area. The SDK's
        # fixed crypto heap/data must be below the entire painted stack range.
        require(size == 8192 and 0 < data_end < self.low < self.high < sp <= top
                and self.high - self.low >= MIN_STACK_GUARD + 4096)
        self.top, self.sp = top, sp
        self.paint = os.urandom(self.high - self.low)
        self.remote = remote
        for offset in range(0, len(self.paint), 256):
            data = self.paint[offset:offset + 256]
            self.write(self.low + offset, data)
        require(self.read() == self.paint)

    def write(self, address, data):
        require(self.remote.packet(f'M{address:x},{len(data):x}:' + data.hex()) == b'OK')

    def read(self):
        observed = bytearray()
        for offset in range(0, len(self.paint), 256):
            size = min(256, len(self.paint) - offset)
            raw = self.remote.packet(f'm{self.low + offset:x},{size:x}')
            require(len(raw) == size * 2)
            try:
                observed.extend(bytes.fromhex(raw.decode('ascii')))
            except (ValueError, UnicodeDecodeError):
                require(False)
        return bytes(observed)

    def finish(self):
        return stack_observation(self.paint, self.read(), self.low, self.top, self.sp)


def stack_observation(paint, observed, low, top, sp):
    require(type(paint) is bytes and type(observed) is bytes and len(paint) == len(observed)
            and len(paint) >= MIN_STACK_GUARD + 4096 and low + len(paint) == ((sp - 64) & ~3))
    changes = [index for index, (a, b) in enumerate(zip(paint, observed)) if a != b]
    require(changes)
    if min(changes) < MIN_STACK_GUARD:
        raise StackGuardError()
    return {'scope': 'boot-go-to-terminal-observed-stack-writes', 'stack_low': low, 'stack_top': top,
            'initial_sp': sp, 'paint_high': low + len(paint), 'paint_sha256': target.digest(paint),
            'untouched_guard_bytes': min(changes), 'minimum_required_guard_bytes': MIN_STACK_GUARD,
            'observed_stack_write_bytes_from_top': top - low - min(changes),
            'full_startup_or_static_bound_qualified': False}


def maximum_fixture(body, signing, settings, pin, work):
    """Largest exact producer-permitted container, not an arbitrary large sample."""
    import sign
    public, policy, key = signing
    metadata = sign.manifest(policy, pin, body, '2.3.4+5', 7)
    layout = settings['layout']
    trailer = 3 * layout['max_sectors'] * layout['write_align'] + 48
    overhead = layout['header_bytes'] + 16 + len(sign.canonical(metadata)) + 464
    maximum = layout['slot_bytes'] - trailer - overhead
    require(len(body) < maximum < layout['slot_bytes'])
    padded = body + bytes([0x5a]) * (maximum - len(body))
    metadata = sign.manifest(policy, pin, padded, '2.3.4+5', 7)
    require(len(sign.canonical(metadata)) + layout['header_bytes'] + 16 + 464 == overhead)
    valid, _ = sign.produce(padded, metadata, policy, key)
    require(len(valid) + trailer == layout['slot_bytes'])
    sign.verify_image(valid, public, pin, settings['target'], settings['configuration_sha256'], 1)
    path = work / 'maximum-size-positive.bin'
    path.write_bytes(valid)
    refused = False
    try:
        excess = padded + b'x'
        sign.produce(excess, sign.manifest(policy, pin, excess, '2.3.4+5', 7), policy, key)
    except ValueError:
        refused = True
    require(refused)
    corrupted = bytearray(valid)
    corrupted[-1] ^= 1
    (work / 'maximum-size-signature-corruption.bin').write_bytes(corrupted)
    return {'payload_bytes': maximum, 'container_bytes': len(valid), 'reserved_trailer_bytes': trailer,
            'slot_bytes': layout['slot_bytes'], 'one_byte_over_budget_refused': True,
            'payload_sha256': sign.sha(padded), 'container_sha256': sign.sha(valid)}
