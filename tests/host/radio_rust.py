"""Link production Rust backend behavior into the native SDK witnesses."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def static_backend(directory, source, *, test=False, capability=None):
    extra = []
    if capability:
        common = directory / "libhal_common.rlib"
        subprocess.run([os.environ.get("RUSTC", "rustc"), "--edition=2021", "--crate-name=hal_common", "--crate-type=rlib", str(ROOT / "hal/common/src/lib.rs"), "-o", str(common)], check=True, capture_output=True)
        extra = ["--extern", "hal_common=" + str(common)]
        source = f'#[path = {json.dumps(str(ROOT / ("hal/" + capability + "/src/api.rs")))}] mod api; pub use api::*;\n' + source
    if capability == 'ble':
        controller = json.dumps(str(ROOT / 'hal/ble/src/controller.rs'))
        client = json.dumps(str(ROOT / 'hal/ble/src/gatt/client.rs'))
        source = f'#[path = {controller}] mod controller; pub use controller::*;\n#[path = {client}] pub mod gatt;\n' + source
    wrapper = directory / 'radio-backend.rs'
    wrapper.write_text("#![no_std]\n#![allow(dead_code)] // SDK witnesses intentionally use a subset of the real capability API.\nextern crate std;\n" + source)
    library = directory / 'libradio_backend.a'
    result = subprocess.run([
        os.environ.get('RUSTC', 'rustc'), '--edition=2021', '--crate-type=staticlib',
        '-Cpanic=abort', '-Copt-level=1', '--print=native-static-libs',
        *(['--cfg', 'test', '--cfg', 'nxrs_native_witness'] if test else []),
        *extra, str(wrapper), '-o', str(library),
    ], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError(result.stderr)
    native = re.search(r'native-static-libs: (.+)', result.stderr)
    return library, shlex.split(native[1]) if native else []


def nuttx_gatt(directory):
    raw = json.dumps(str(ROOT / 'hal/ble/src/providers/gatt_backend.rs'))
    behavior = json.dumps(str(ROOT / 'hal/ble/src/providers/nuttx/gatt.rs'))
    return static_backend(directory, f'''
mod providers {{ #[path = {raw}] pub mod gatt_backend; }}
#[path = {behavior}] mod behavior;
''', test=True, capability='ble')


def wifi_backend(directory, platform):
    behavior = json.dumps(str(ROOT / f'hal/wifi/src/providers/native/{platform}/station.rs'))
    contract = json.dumps(str(ROOT / 'hal/wifi/src/providers/native/station.rs'))
    shim = json.dumps(str(ROOT / 'tests/host/radio_shims/wifi.rs'))
    gio = json.dumps(str(ROOT / 'hal/wifi/src/providers/native/linux/gio.rs'))
    modules = f'#[path = {gio}] mod gio;' if platform == 'linux' else ''
    return static_backend(directory, f"""
#[path = {contract}] mod contract;
use contract::Station;
{modules}
#[path = {behavior}] mod behavior;
#[path = {shim}] mod witness;
""", test=True, capability='wifi')


def macos_wifi(directory):
    return wifi_backend(directory, 'macos')


def scan_contract():
    contract = json.dumps(str(ROOT / 'hal/ble/src/providers/native/scan.rs'))
    worker = json.dumps(str(ROOT / 'hal/ble/src/worker.rs'))
    return f"""
#[path = {worker}] mod worker;
#[path = {contract}] mod scan_contract;
use scan_contract::{{ScanBackend, Report}};
"""


def macos_ble_scan(directory):
    behavior = json.dumps(str(ROOT / 'hal/ble/src/providers/native/macos/scan.rs'))
    uuid = json.dumps(str(ROOT / 'hal/ble/src/providers/native/macos/uuid.rs'))
    return static_backend(directory, scan_contract() + f"""
#[path = {uuid}] mod uuid;
#[path = {behavior}] mod behavior;
""", test=True, capability='ble')


def macos_ble_gatt(directory):
    raw = json.dumps(str(ROOT / 'hal/ble/src/providers/gatt_backend.rs'))
    uuid = json.dumps(str(ROOT / 'hal/ble/src/providers/native/macos/uuid.rs'))
    behavior = json.dumps(str(ROOT / 'hal/ble/src/providers/native/macos/gatt.rs'))
    return static_backend(directory, f"""
mod providers {{ #[path = {raw}] pub mod gatt_backend; }}
#[path = {uuid}] mod uuid;
#[path = {behavior}] mod behavior;
""", test=True, capability='ble')


def linux_wifi(directory):
    return wifi_backend(directory, 'linux')


def linux_ble(directory):
    modules = {name: json.dumps(str(ROOT / ('hal/ble/src/providers/native/linux/' + name + '.rs')))
               for name in ('gio', 'identity', 'scan', 'gatt')}
    raw = json.dumps(str(ROOT / 'hal/ble/src/providers/gatt_backend.rs'))
    source = '\n'.join(f'#[path = {path}] mod {"backend_impl" if name == "gatt" else name};' for name, path in modules.items())
    return static_backend(directory, scan_contract() + f"""
mod providers {{ #[path = {raw}] pub mod gatt_backend; }}
{source}
""", test=True, capability='ble')
