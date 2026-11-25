"""Install the generated dependencies and maintained source of the tracker demo."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import tarfile


GUIDE = 'website/guide/motion-tracker.md'
ROUTE = 'demos/motion-tracker/'
SOURCE = 'tests/browser/tracker-demo'
ARCHIVE = 'website/examples/motion-tracker/assets.tar.gz'
FILES = {
    'esp_emu.js', 'esp_emu_bg.wasm', 'esp_cosim_pb.js', 'jsQR.js',
    'nuttx.merged.bin', 'symbols.mjs', 'tls-client.mjs', 'tls-client.wasm',
    'licenses/ESP-EMU.txt', 'licenses/Mbed-TLS.txt', 'licenses/TF-PSA-Crypto.txt',
    'licenses/jsQR.txt', 'licenses/NXRS.txt', 'licenses/NuttX-LICENSE.txt',
    'licenses/NuttX-NOTICE.txt', 'licenses/NuttX-Apps-LICENSE.txt', 'licenses/NuttX-Apps-NOTICE.txt',
}
RUNTIME = (
    'index.html', 'style.css', 'app.mjs', 'worker.mjs', 'h4.mjs',
    'gnss.mjs', 'panel.mjs', 'secure-stream.mjs', 'touchscreen.mjs',
)


def install(root: Path, output: Path) -> dict:
    archive = root / ARCHIVE
    destination = output / ROUTE
    destination.mkdir(parents=True)
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        if (len(members) != len(FILES) or {member.name for member in members} != FILES
                or any(not member.isfile() or not 0 < member.size <= 8 * 1024 * 1024 for member in members)):
            raise ValueError('unexpected files in the Motion Tracker demo asset bundle')
        for member in members:
            file = destination / member.name
            file.parent.mkdir(exist_ok=True)
            with bundle.extractfile(member) as data, file.open('wb') as out:
                shutil.copyfileobj(data, out)
    for name in RUNTIME:
        shutil.copyfile(root / SOURCE / name, destination / name)
    shutil.copyfile(root / 'tests/browser/esp32s3-preemption/browser_devices.js',
                    destination / 'browser_devices.js')
    return {'asset_archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'firmware_sha256': hashlib.sha256((destination / 'nuttx.merged.bin').read_bytes()).hexdigest()}
