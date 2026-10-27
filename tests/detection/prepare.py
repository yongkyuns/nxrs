#!/usr/bin/env python3
"""Reproducible balanced COCO-val2017 Visual Wake Words sample, private outputs.

Only selected images are downloaded. Read the official ZIP's central directory
and validation annotation member with HTTP ranges rather than fetching all train
annotations. Individual source-image licenses remain applicable; no images are
redistributed by nxrs. Requires Pillow for camera-equivalent baseline JPEGs.
"""
import argparse
import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

# Official COCO S3 bucket through its TLS-valid path endpoint. The historical
# images.cocodataset.org alias currently presents a mismatched certificate.
ANNOTATIONS = 'https://s3.amazonaws.com/images.cocodataset.org/annotations/annotations_trainval2017.zip'
IMAGES = 'https://s3.amazonaws.com/images.cocodataset.org/val2017/'
ANNOTATIONS_SHA256 = 'e8c7f7908f1d7278341fae127d0da654f102f11bd7b21d8aeefa635b8c810b6f'


class RemoteZip(io.RawIOBase):
    def __init__(self, url):
        self.url = url
        with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=60) as response:
            self.size = int(response.headers['Content-Length'])
        self.position = 0

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset + (0 if whence == 0 else self.position if whence == 1 else self.size)
        if self.position < 0:
            raise ValueError('invalid ZIP seek')
        return self.position

    def read(self, size=-1):
        size = min(size if size >= 0 else self.size - self.position, self.size - self.position)
        if size == 0:
            return b''
        if not 0 < size <= 32 * 1024 * 1024:
            raise ValueError('unexpected annotation member size')
        start, end = self.position, self.position + size - 1
        request = urllib.request.Request(self.url, headers={'Range': f'bytes={start}-{end}'})
        with urllib.request.urlopen(request, timeout=90) as response:
            if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{self.size}':
                raise ValueError('server did not honor exact annotation range')
            data = response.read(size + 1)
        if len(data) != size:
            raise ValueError('incomplete annotation range')
        self.position += size
        return data


def select(data, count, skip=0, excluded=frozenset()):
    # Match TensorFlow's official _filter_annotations implementation: COCO's
    # annotated object area, NOT the rectangle's width*height.
    dimensions = {v['id']: v['width'] * v['height'] for v in data['images']}
    largest = {}
    for annotation in data['annotations']:
        if annotation['category_id'] == 1:
            image_id = annotation['image_id']
            largest[image_id] = max(largest.get(image_id, 0), annotation['area'] / dimensions[image_id])
    positive = {image_id for image_id, fraction in largest.items() if fraction > 0.005}
    rank = lambda v: hashlib.sha256(f"nxrs-vww-v1:{v['id']}".encode()).digest()
    chosen = []
    for label in (0, 1):
        group = sorted((v for v in data['images'] if v['id'] not in excluded and int(v['id'] in positive) == label), key=rank)
        if len(group) < skip + count // 2:
            raise ValueError('sample exceeds available class')
        chosen.extend(dict(v, label=label, largest_person_fraction=largest.get(v['id'], 0),
                           has_small_person=largest.get(v['id'], 0) > 0 and v['id'] not in positive)
                      for v in group[skip:skip + count // 2])
    return sorted(chosen, key=lambda v: v['id'])


def camera_jpeg(image, output, expected_dimensions=None):
    """Shared camera-equivalent input preparation; dataset labels stay local."""
    from PIL import Image
    with Image.open(io.BytesIO(image)) as decoded:
        dimensions = decoded.size
        if dimensions[0] * dimensions[1] > 16_000_000:
            raise ValueError('source image pixel bound exceeded')
        if expected_dimensions is not None and dimensions != expected_dimensions:
            raise ValueError('annotation/image dimension mismatch')
        jpeg = decoded.convert('RGB').resize((320, 240), Image.Resampling.LANCZOS)
        jpeg.save(output, 'JPEG', quality=81, progressive=False)
    return dimensions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--count', type=int, default=200)
    parser.add_argument('--skip-per-class', type=int, default=0, help='reserve an independent follow-up sample')
    parser.add_argument('--exclude-dataset', type=Path, help='exclude all IDs from an exploratory dataset manifest')
    parser.add_argument('--annotations', type=Path, help='optional pre-downloaded official instances_val2017.json')
    args = parser.parse_args()
    if args.count < 20 or args.count > 1000 or args.count % 2:
        parser.error('count must be even, 20..1000')
    if args.skip_per_class < 0:
        parser.error('skip-per-class must be nonnegative')
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    if args.annotations:
        raw = args.annotations.read_bytes()
    else:
        with zipfile.ZipFile(RemoteZip(ANNOTATIONS)) as archive:
            raw = archive.read('annotations/instances_val2017.json')  # ZIP CRC is verified.
    if hashlib.sha256(raw).hexdigest() != ANNOTATIONS_SHA256:
        raise ValueError('official validation annotation identity mismatch')
    data = json.loads(raw)
    excluded = {v['id'] for v in json.loads(args.exclude_dataset.read_text())['images']} if args.exclude_dataset else set()
    records = select(data, args.count, args.skip_per_class, excluded)
    def download(record):
        request = urllib.request.Request(IMAGES + record['file_name'])
        with urllib.request.urlopen(request, timeout=60) as response:
            image = response.read(8 * 1024 * 1024 + 1)
        if len(image) > 8 * 1024 * 1024:
            raise ValueError('image download bound exceeded')
        output = args.out / record['file_name']
        camera_jpeg(image, output, (record['width'], record['height']))
        return dict(record, source_sha256=hashlib.sha256(image).hexdigest(),
                    jpeg_sha256=hashlib.sha256(output.read_bytes()).hexdigest())

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        records = list(executor.map(download, records))
    manifest = {'dataset': 'COCO val2017 / VWW annotated object area >0.005',
                'label_source': 'TensorFlow models/research/slim/datasets/download_and_convert_visualwakewords_lib.py _filter_annotations',
                'selection': f'balanced SHA256 nxrs-vww-v1 ranks {args.skip_per_class}..{args.skip_per_class + args.count // 2} per class',
                'excluded_image_ids': sorted(excluded),
                'annotations_sha256': hashlib.sha256(raw).hexdigest(), 'image_base': IMAGES,
                'licenses': data['licenses'], 'camera_preparation': 'Pillow RGB resize 320x240 LANCZOS, JPEG quality=81',
                'images': records}
    (args.out / 'dataset.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (args.out / 'manifest.tsv').write_text(''.join(f"{r['label']}\t320\t240\t{args.out.resolve() / r['file_name']}\n" for r in records))
    print(json.dumps({'images': len(records), 'positive': sum(r['label'] for r in records),
                      'annotations_sha256': manifest['annotations_sha256']}))


if __name__ == '__main__':
    main()
