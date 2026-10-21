#!/usr/bin/env python3
"""Small, reproducible Open Images sample with verified present/absent labels.

This is an any-person image-label task, distinct from VWW's minimum object area.
Only rotation-zero, CC-BY-2.0 validation images are eligible. Images stay private;
the manifest retains their attribution and original/prepared byte hashes.
"""
import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request

PERSON = '/m/01g317'
LICENSE = 'https://creativecommons.org/licenses/by/2.0/'
IMAGE_BASE = 'https://open-images-dataset.s3.amazonaws.com/validation/'
METADATA = {
    'images': ('https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv',
               'ed93a0e121fe345effdfc7359b848dbc64a1ff6778c8c73563157cb500b33a17'),
    'labels': ('https://storage.googleapis.com/openimages/v5/validation-annotations-human-imagelabels-boxable.csv',
               'd5ff236d3cdbba36dac1ee6f7fdf9a26934205cbec5b933b0872722e2b6d3417'),
    'classes': ('https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions-boxable.csv',
                '1839e0e7e84130ae281f7f67413768601b031581c0c42e7fc17527b8e2a99aa9'),
}


def select(images, annotations, count):
    labels = {}
    for row in annotations:
        if (row['LabelName'] == PERSON and row['Source'] in ('verification', 'crowdsource-verification')
                and row['Confidence'] in ('0', '1')):
            labels.setdefault(row['ImageID'], set()).add(int(row['Confidence']))
    eligible = []
    for row in images:
        values = labels.get(row['ImageID'], set())
        if (len(values) != 1 or row['License'] != LICENSE or row['Rotation'] != '0.0'
                or not re.fullmatch('[0-9a-f]{16}', row['ImageID'])):
            continue
        eligible.append(dict(row, label=next(iter(values))))
    rank = lambda row: hashlib.sha256(('nxrs-oi-person-v1:' + row['ImageID']).encode()).digest()
    selected = []
    for label in (0, 1):
        group = sorted((row for row in eligible if row['label'] == label), key=rank)
        if len(group) < count // 2:
            raise ValueError('not enough verified labels in eligible images')
        selected.extend(group[:count // 2])
    return sorted(selected, key=lambda row: row['ImageID'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--count', default=200, type=int)
    parser.add_argument('--metadata-dir', type=Path, help='optional cache of the three verified CSV files')
    args = parser.parse_args()
    if args.count < 20 or args.count > 1000 or args.count % 2:
        parser.error('count must be even, 20..1000')
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    metadata = {}
    for name, (url, expected) in METADATA.items():
        if args.metadata_dir:
            raw = (args.metadata_dir / f'{name}.csv').read_bytes()
        else:
            with urllib.request.urlopen(url, timeout=60) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f'official {name} metadata identity mismatch')
        metadata[name] = list(csv.DictReader(io.StringIO(raw.decode('utf8'))))
    if not any(row['LabelName'] == PERSON and row['DisplayName'] == 'Person' for row in metadata['classes']):
        raise ValueError('person class identity mismatch')
    selected = select(metadata['images'], metadata['labels'], args.count)
    from prepare import camera_jpeg

    def download(row):
        image_id = row['ImageID']
        with urllib.request.urlopen(IMAGE_BASE + image_id + '.jpg', timeout=60) as response:
            image = response.read(8 * 1024 * 1024 + 1)
        if len(image) > 8 * 1024 * 1024:
            raise ValueError('image download bound exceeded')
        output = args.out / f'{image_id}.jpg'
        width, height = camera_jpeg(image, output)
        return {'id': image_id, 'label': row['label'], 'width': width, 'height': height,
                'license': row['License'], 'author': row['Author'],
                'author_profile': row['AuthorProfileURL'], 'original_landing_url': row['OriginalLandingURL'],
                'source_sha256': hashlib.sha256(image).hexdigest(),
                'jpeg_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        records = list(executor.map(download, selected))
    manifest = {'dataset': 'Open Images V7 validation / verified any-person image labels',
                'label_source': 'Official human image labels for Person /m/01g317: confidence 0 or 1',
                'selection': 'balanced SHA256 nxrs-oi-person-v1 ranks; rotation 0.0; CC-BY-2.0; no conflicting labels',
                'annotations_sha256': METADATA['labels'][1],
                'metadata': {name: {'url': url, 'sha256': digest} for name, (url, digest) in METADATA.items()},
                'camera_preparation': 'Pillow RGB resize 320x240 LANCZOS, JPEG quality=81', 'images': records}
    (args.out / 'dataset.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (args.out / 'manifest.tsv').write_text(''.join(
        f"{row['label']}\t320\t240\t{args.out.resolve() / (row['id'] + '.jpg')}\n" for row in records))
    print(json.dumps({'images': len(records), 'positive': sum(row['label'] for row in records)}))


if __name__ == '__main__':
    main()
