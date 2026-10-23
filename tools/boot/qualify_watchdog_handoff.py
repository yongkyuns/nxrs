"""Frozen NuttX early-handoff function under a protected-register host model.

This executes the real source function with modeled SDK operations, not target
MMIO. Linux target qualification separately compiles/links the full patched SDK.
"""
import json
from pathlib import Path
import re
import shutil

import qualify as process
import target_plan as target

ROOT = target.ROOT
PATCH = 'upstream/nuttx/patches/0092-esp32s3-unlock-early-watchdog-disable.patch'
SOURCE = 'arch/xtensa/src/esp32s3/esp32s3_wdt.c'
INPUTS = (PATCH, 'tools/apply-nuttx-patches.py', 'upstream/nuttx/release.json',
          'security/esp32s3-boot-emulator.json', 'tools/boot/qualify_watchdog_handoff.py',
          'tests/boot/watchdog_handoff_model.c', 'tools/boot/qualify_target.py',
          'tools/boot/boot_resources.py', 'tests/boot/runtime_layout.c')
require = target.require


def early_function(text):
    values = re.findall(r'void esp32s3_wdt_early_deinit\(void\)\s*\{[^{}]*\}', text)
    require(len(values) == 1)
    return values[0] + '\n'


def qualify(work, contract, environment):
    work.mkdir(mode=0o700)
    for name in ('logs', 'source', 'applied'):
        (work / name).mkdir(mode=0o700)
    inputs = {name: process.digest(ROOT / name) for name in INPUTS}
    entry = json.loads((ROOT / 'security/esp32s3-boot-emulator.json').read_bytes())['nuttx_handoff_source']
    revision = json.loads((ROOT / 'upstream/nuttx/release.json').read_bytes())['revision']
    require(entry['commit'] == revision)
    frozen = process.fetch(entry, work / 'source')
    destination = work / 'applied' / SOURCE
    destination.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(frozen, destination)
    status, _ = process.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(ROOT / PATCH)],
                            work / 'applied', environment, work / 'logs', 'handoff-patch', 30)
    require(status == 0)
    original = early_function(frozen.read_text())
    patched = early_function(destination.read_text())
    first, second = 'rwdt_ll_write_protect_disable(rwdt);', 'rwdt_ll_disable(rwdt);'
    require(patched.count(first) == patched.count(second) == 1)
    reordered = patched.replace(first, 'NXRS_REORDERED_CALL;').replace(second, first).replace('NXRS_REORDERED_CALL;', second)
    values = {'RTC_CNTL_WDTCONFIG0_REG': contract['watchdog_config_address'],
              'RTC_CNTL_WDTWPROTECT_REG': contract['watchdog_protect_address'],
              'RTC_CNTL_WDT_EN': contract['watchdog_enable_mask'],
              'RWDT_LL_WDT_WKEY_VALUE': contract['watchdog_unlock_key']}
    compiler = shutil.which('cc', path=environment['PATH'])
    require(compiler is not None)
    controls = []
    for name, body, expected, marker in (
            ('original', original, 1, 'WDT_HANDOFF_PROTECTED_WRITE_REFUSED'),
            ('patched', patched, 0, 'WDT_HANDOFF_MODEL_PASSED 4'),
            ('reordered', reordered, 1, 'WDT_HANDOFF_PROTECTED_WRITE_REFUSED'),
            ('restored', patched, 0, 'WDT_HANDOFF_MODEL_PASSED 4')):
        folder = work / name
        folder.mkdir(mode=0o700)
        (folder / 'constants.h').write_text(''.join('#define ' + key + ' ' + str(value) + 'u\n' for key, value in values.items()))
        (folder / 'early-function.c').write_text(body)
        status, _ = process.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2', '-I', str(folder),
                                str(ROOT / 'tests/boot/watchdog_handoff_model.c'), '-o', str(folder / 'witness')],
                                work, environment, work / 'logs', name + '-compile', 30)
        require(status == 0)
        status, output = process.run([str(folder / 'witness')], work, environment, work / 'logs', name + '-execute', 10)
        require(status == expected and output == marker + '\n')
        controls.append({'name': name, 'status': status, 'required_marker': marker})
    require(inputs == {name: process.digest(ROOT / name) for name in INPUTS})
    return {'schema': 1, 'scope': 'actual-frozen-nuttx-startup-function-protected-register-host-model',
            'qualified': True, 'inputs': inputs, 'source': entry, 'original_function_sha256': target.digest(original.encode()),
            'patched_function_sha256': target.digest(patched.encode()), 'runtime_contract_sha256': target.digest(target.canonical(contract)),
            'controls': controls, 'physical_qualified': False, 'target_abi_qualified': False,
            'watchdog_reset_timing_qualified': False, 'deployment_permitted': False}
