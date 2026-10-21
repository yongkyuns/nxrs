"""Link production Rust backend behavior into the native SDK witnesses."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def static_backend(directory, source, *, test=False):
    wrapper = directory / 'radio-backend.rs'
    wrapper.write_text("#![no_std]\nextern crate std;\n" + source)
    library = directory / 'libradio_backend.a'
    result = subprocess.run([
        os.environ.get('RUSTC', 'rustc'), '--edition=2021', '--crate-type=staticlib',
        '-Cpanic=abort', '-Copt-level=1', '--print=native-static-libs',
        *(['--cfg', 'test', '--cfg', 'nxrs_native_witness'] if test else []),
        str(wrapper), '-o', str(library),
    ], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError(result.stderr)
    native = re.search(r'native-static-libs: (.+)', result.stderr)
    return library, shlex.split(native[1]) if native else []


def nuttx_gatt(directory):
    raw = json.dumps(str(ROOT / 'hal/ble/src/providers/raw_gatt.rs'))
    behavior = json.dumps(str(ROOT / 'hal/ble/src/providers/nuttx/gatt.rs'))
    return static_backend(directory, f'''
mod providers {{ #[path = {raw}] pub mod raw_gatt; }}
#[path = {behavior}] mod behavior;
''')


def macos_wifi(directory):
    behavior = json.dumps(str(ROOT / 'hal/wifi/src/providers/native/macos.rs'))
    return static_backend(directory, f'''
#[repr(C)]
struct Row {{ ssid_length: u32, security: u32, rssi_dbm: i32, ssid: [u8;32] }}
#[path = {behavior}] mod behavior;
''', test=True)


def macos_ble_scan(directory):
    behavior = json.dumps(str(ROOT / 'hal/ble/src/providers/native/macos_scan.rs'))
    uuid = json.dumps(str(ROOT / 'hal/ble/src/providers/native/macos_uuid.rs'))
    return static_backend(directory, f"""
#[repr(C)] #[derive(Clone, Copy)]
struct Row {{ peer: [u8;16], rssi_dbm: i32, matches_filter: u32 }}
#[path = {uuid}] mod macos_uuid;
#[path = {behavior}] mod behavior;
""", test=True)


def macos_ble_gatt(directory):
    raw = json.dumps(str(ROOT / 'hal/ble/src/providers/raw_gatt.rs'))
    uuid = json.dumps(str(ROOT / 'hal/ble/src/providers/native/macos_uuid.rs'))
    behavior = json.dumps(str(ROOT / 'hal/ble/src/providers/native/macos_gatt.rs'))
    return static_backend(directory, f"""
mod providers {{ #[path = {raw}] pub mod raw_gatt; }}
#[path = {uuid}] mod macos_uuid;
#[path = {behavior}] mod behavior;
""", test=True)


def linux_wifi(directory):
    gio = json.dumps(str(ROOT / 'hal/wifi/src/providers/native/gio.rs'))
    behavior = json.dumps(str(ROOT / 'hal/wifi/src/providers/native/linux.rs'))
    return static_backend(directory, f"""
#[repr(C)] struct Row {{ ssid_length: u32, security: u32, rssi_dbm: i32, ssid: [u8;32] }}
#[path = {gio}] mod gio;
#[path = {behavior}] mod behavior;
""", test=True)


def linux_ble(directory):
    modules = {name: json.dumps(str(ROOT / ('hal/ble/src/providers/native/' + name + '.rs')))
               for name in ('gio', 'linux_identity', 'linux_scan', 'linux_gatt')}
    raw = json.dumps(str(ROOT / 'hal/ble/src/providers/raw_gatt.rs'))
    source = '\n'.join(f'#[path = {path}] mod {name};' for name, path in modules.items())
    return static_backend(directory, f"""
mod providers {{ #[path = {raw}] pub mod raw_gatt; }}
#[repr(C)] #[derive(Clone, Copy)] struct Row {{ peer: [u8;16], rssi_dbm: i32, matches_filter: u32 }}
{source}
""", test=True)
