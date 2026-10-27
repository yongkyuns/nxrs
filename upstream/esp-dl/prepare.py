#!/usr/bin/env python3
"""Fetch and verify the pinned numerical SDK; ordinary Cargo builds never fetch it."""
from __future__ import annotations
import argparse, hashlib, json, pathlib, subprocess, tempfile, shutil
ROOT = pathlib.Path(__file__).resolve().parents[2]
LEDGER = pathlib.Path(__file__).with_name('source.json')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def prepare(destination, checkout=None):
    record = json.loads(LEDGER.read_text())
    destination = pathlib.Path(destination).resolve()
    if destination.exists():
        verify(destination)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pico-prepare-', dir=destination.parent) as temporary:
        temporary = pathlib.Path(temporary)
        if checkout is None:
            checkout = temporary / 'git'
            subprocess.run(['git', 'clone', '--depth', '1', '--branch', record['tag'],
                            record['repository'], str(checkout)], check=True)
        checkout = pathlib.Path(checkout)
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
        if revision != record['revision']:
            raise ValueError('SDK checkout revision differs from pinned source')
        staged = temporary / 'prepared'
        staged.mkdir()
        for name, expected in record['files'].items():
            data = subprocess.check_output(['git', 'show', revision + ':' + name], cwd=checkout)
            if digest(data) != expected:
                raise ValueError('SDK source hash differs: ' + name)
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Exclude SDK-owned task launching. Rust supplies the single worker;
        # a future attempt to split SDK work must fail instead of emulating an RTOS.
        path = staged / 'esp-dl/dl/module/include/dl_module_base.hpp'
        text = path.read_text()
        start = text.index('/**\n * @brief The data struct of module task.')
        end = text.index('#pragma GCC diagnostic pop', start) + len('#pragma GCC diagnostic pop')
        path.write_text(text[:start] + 'static void module_forward_dual_core(Module *, void *, void *) { abort(); }\n' + text[end:])
        # SDK headers assume a 32-bit pointer even in a scalar host build.
        path = staged / 'esp-dl/dl/base/dl_base_pool2d.hpp'
        path.write_text(path.read_text().replace('(unsigned)&args.', '(uintptr_t)&args.'))
        # These numerical sources use printf; avoid an unused global iostream initializer.
        for path in staged.rglob('*'):
            if path.suffix in ('.cpp', '.hpp'):
                text = path.read_text()
                if '#include <iostream>' in text:
                    path.write_text(text.replace('#include <iostream>', ''))
        identity = {'ledger_sha256': digest(LEDGER.read_bytes()), 'files': {
            name: digest((staged / name).read_bytes()) for name in record['files']}}
        if identity['files'] != record['prepared_files']:
            raise ValueError('SDK port patch output differs from reviewed hashes')
        (staged / 'prepared.json').write_text(json.dumps(identity, indent=2) + '\n')
        staged.rename(destination)

def verify(destination):
    destination = pathlib.Path(destination)
    record = json.loads(LEDGER.read_text())
    identity = json.loads((destination / 'prepared.json').read_text())
    if identity['ledger_sha256'] != digest(LEDGER.read_bytes()) or identity['files'] != record['prepared_files']:
        raise ValueError('prepared SDK inventory differs from pinned ledger')
    for name, expected in identity['files'].items():
        if digest((destination / name).read_bytes()) != expected:
            raise ValueError('prepared SDK changed: ' + name)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=pathlib.Path, default=ROOT / 'target/pico-sdk')
    parser.add_argument('--checkout', type=pathlib.Path)
    args = parser.parse_args()
    prepare(args.out, args.checkout)
    print(args.out.resolve())
