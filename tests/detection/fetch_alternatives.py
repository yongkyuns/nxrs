#!/usr/bin/env python3
"""Fetch exact public screening artifacts into a new private directory.

Never rebuild or silently substitute a model. Edge Impulse's generated SDK is
not extracted: only the two standalone WASM files or the bare TFLite member.
No artifacts or dataset images are added to production or uploaded anywhere.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile


def download(url, expected):
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('artifact size or identity differs from screening pin')
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    pins = json.loads(Path(__file__).with_name('alternatives.json').read_text())
    for model in pins['models']:
        identifier = model['public_snapshot_id']
        for export in model['wasm_exports']:
            data = download(export['url'], export['archive_sha256'])
            dest = args.out / f"fomo-{identifier}-{export['variant']}"
            dest.mkdir(mode=0o700)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name, key in [('node/edge-impulse-standalone.wasm', 'wasm_sha256'),
                                  ('node/edge-impulse-standalone.js', 'loader_sha256')]:
                    blob = archive.read(name)
                    if hashlib.sha256(blob).hexdigest() != export[key]:
                        raise ValueError('member identity')
                    target = dest / name
                    target.parent.mkdir(mode=0o700, exist_ok=True)
                    target.write_bytes(blob)
        pin = model['bare_tflite']
        data = download(pin['container_url'], pin['container_sha256'])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            blob = archive.read(pin['member'])
        if hashlib.sha256(blob).hexdigest() != pin['model_sha256']:
            raise ValueError('TFLite identity')
        (args.out / f'fomo-{identifier}-int8.tflite').write_bytes(blob)
    nano = pins['nanodet']
    (args.out / 'nanodet-m-0.5x.ckpt').write_bytes(download(nano['checkpoint_url'], nano['checkpoint_sha256']))
    print('Exact pinned artifacts verified; clone the pinned NanoDet source separately.')


if __name__ == '__main__':
    main()
