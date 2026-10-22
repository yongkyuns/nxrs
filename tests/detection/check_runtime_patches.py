#!/usr/bin/env python3
"""Rebuild the retained MicroFlow capsule from its pinned archive and patches."""
import argparse
import hashlib
import io
import json
import re
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='use an existing pinned crate archive offline')
    args = parser.parse_args()
    source = (ROOT / 'hal/detector/src/platform/std/person.rs').read_text()
    pin = re.search(r'pub const MODEL_SHA256: &str = "([0-9a-f]{64})";', source)
    if pin is None or hashlib.sha256((ROOT / 'hal/detector/models/person_detect.tflite').read_bytes()).hexdigest() != pin[1]:
        raise ValueError('embedded model identity disagrees with its recorded pin')
    ledger = json.loads((ROOT / 'upstream/microflow/source.json').read_text())
    if args.archive:
        archive = args.archive.read_bytes()
    else:
        request = urllib.request.Request(ledger['url'], headers={'User-Agent': 'nxrs-qualification/1'})
        with urllib.request.urlopen(request, timeout=30) as response:
            archive = response.read(1024 * 1024 + 1)
    if len(archive) > 1024 * 1024 or hashlib.sha256(archive).hexdigest() != ledger['archive_sha256']:
        raise ValueError('upstream runtime archive identity mismatch')
    with tempfile.TemporaryDirectory(prefix='nxrs-runtime-replay-') as directory:
        target = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source:
            # Read only named regular files. Never extract archive paths,
            # symlinks or traversal components into the filesystem.
            for recorded in ledger['files']:
                relative = Path(recorded).relative_to('vendor/microflow')
                try:
                    member = source.getmember(f"microflow-{ledger['version']}/{relative.as_posix()}")
                except KeyError:
                    continue  # A reviewed patch introduces this file.
                if not member.isfile() or member.size > 256 * 1024:
                    raise ValueError('invalid retained archive member')
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.extractfile(member).read())
        for patch in ledger['patches']:
            subprocess.run(['git', 'apply', str(ROOT / 'upstream/microflow' / patch)],
                           cwd=target, check=True, capture_output=True)
        actual = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in target.rglob('*') if p.is_file()}
        expected = {Path(p).relative_to('vendor/microflow').as_posix(): digest
                    for p, digest in ledger['files'].items()}
        if actual != expected:
            raise ValueError('patch replay does not reproduce the recorded runtime capsule')
    print('PASS: pinned archive plus reviewed patches reproduces every runtime file')


if __name__ == '__main__':
    main()
