#!/usr/bin/env python3
"""Compatibility CLI for the Rust deployment preflight."""
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def validate(app_manifest, platform_name, target_os):
    result = subprocess.run(['cargo', 'nxrs', 'check', '--manifest-path', str(app_manifest),
                             '--profile', platform_name, '--target-os', target_os],
                            cwd=ROOT, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return json.loads(result.stdout)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-manifest', type=Path, required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--target-os', required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    try:
        result = validate(args.app_manifest, args.platform, args.target_os)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Deployment: {error}\n')
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded)
    print(encoded, end='')
