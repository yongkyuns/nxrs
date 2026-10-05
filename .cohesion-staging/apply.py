#!/usr/bin/env python3
"""Apply a hash-guarded documentation delta, or verify its exact qualified result."""
from pathlib import Path, PurePosixPath
import argparse, hashlib, json, lzma, subprocess

ROOT = Path.cwd()
STAGING = ROOT / '.cohesion-staging'
BASE = 'f6fa783d75aab6080ae3385934aaa25637469e84'
ARCHIVE_SHA = '7347af04e926507578eb6045ffe416e19689f1ce5cdfd8eaad61151da5d76a8a'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def checked_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not name.startswith('docs/references/'):
        raise ValueError(f'Out-of-scope path: {name}')
    return ROOT / path

def load_payload():
    archive = b''.join((STAGING / f'payload.{i}').read_bytes() for i in range(1, 5))
    if digest(archive) != ARCHIVE_SHA:
        raise ValueError('Archive integrity failure')
    payload = json.loads(lzma.decompress(archive))
    if payload['base'] != BASE or len(payload['expected']) != 22:
        raise ValueError('Unexpected base or scope')
    for name in list(payload['expected']) + list(payload['svg_hashes']):
        checked_path(name)
    return payload

def apply(payload):
    # Read every original from the pinned commit before writing. The new PX4
    # atlas deliberately derives from the OLD PX4 README, not its replacement.
    originals = {}
    for op in payload['operations']:
        checked_path(op['source']); checked_path(op['path'])
        if op['path'] not in payload['expected']:
            raise ValueError('Unexpected edit target')
        source = subprocess.run(['git', 'show', f"{BASE}:{op['source']}"], capture_output=True)
        original = source.stdout if source.returncode == 0 else b''
        if digest(original) != op['sha256']:
            raise ValueError(f"Original mismatch: {op['source']}")
        originals[op['path']] = original.decode('utf-8')
    for op in payload['operations']:
        lines = originals[op['path']].splitlines(keepends=True)
        previous = len(lines)
        for start, end, replacement in reversed(op['edits']):
            if not 0 <= start <= end <= previous:
                raise ValueError(f"Invalid edit range: {op['path']}")
            lines[start:end] = replacement
            previous = start
        result = ''.join(lines).encode('utf-8')
        if digest(result) != payload['expected'][op['path']]:
            raise ValueError(f"Delta result mismatch: {op['path']}")
        target = checked_path(op['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(result)
    print(f"Applied {len(payload['operations'])} hash-verified documentation inputs")

def verify(payload):
    for name, sha in {**payload['expected'], **payload['svg_hashes']}.items():
        if digest(checked_path(name).read_bytes()) != sha:
            raise ValueError(f'Qualified output mismatch: {name}')
    changed = subprocess.check_output(['git', 'diff', '--name-only', '-z', 'HEAD']).decode().split('\0')
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z']).decode().split('\0')
    names = set(filter(None, changed + untracked))
    # Generated reports are explicitly isolated and will never enter the PR.
    names = {p for p in names if not p.startswith('.cohesion-staging/')}
    if names != set(payload['expected']):
        raise ValueError(f'Scope mismatch: {sorted(names ^ set(payload["expected"]))}')
    report = {'base': BASE, 'changed_files': len(payload['expected']), 'matched_svg_hashes': len(payload['svg_hashes']),
              'status': 'passed', 'limits': 'Hosted deterministic rendering and byte identity; browser geometry was qualified locally against these exact SVG bytes.'}
    (STAGING / 'qualification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    subprocess.run(['git', 'diff', '--check'], check=True)
    subprocess.run(['git', 'add', '--', *sorted(payload['expected'])], check=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    data = load_payload()
    verify(data) if args.verify else apply(data)
