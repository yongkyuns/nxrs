"""Pinned Zephyr target setup and resolved configuration validation."""
from pathlib import Path
import re
import subprocess

BOARD = 'esp32s3_devkitm/esp32s3/procpu'
ZEPHYR_SHA = '75f67d766726351b30199f9a2bf55803d717a3be'
PINS = {
    'espressif': 'af6cfa2e3e7098b596062ab516b80a48a7ba7332',
    'xtensa': '3cc9e3a9360be5c96c956dce84064b85439b6769',
}


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
