#!/usr/bin/env python3
"""Generate local development enrollment, never shared defaults or actual camera claims."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit


def initialize(directory: Path, media_origin: str):
    parts = urlsplit(media_origin)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ['', '/']
            or not media_origin.isascii() or any(c in media_origin for c in ['%', '\\', '#', '?'])
            or (parts.port is not None and not 1 <= parts.port <= 65535)):
        raise ValueError('an HTTPS media origin without credentials, path or query is required')
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    viewer, publisher = secrets.token_hex(32), secrets.token_hex(32)
    def write(name, text):
        with open(directory / name, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            stream.write(text)
    config = {
        'listen': '127.0.0.1:18080', 'auth_listen': '127.0.0.1:18081',
        'grant_ttl_seconds': 60, 'max_grants': 128, 'media_origins': [media_origin.rstrip('/')],
        'cameras': [{'camera_id': 'cam_reference', 'gateway_id': 'gw_reference', 'site_id': 'site_reference',
                     'name': 'SYNTHETIC reference - not a physical camera', 'source': 'synthetic',
                     'stream_path': 'reference', 'whep_endpoint': media_origin.rstrip('/') + '/reference/whep'}],
        'viewers': [{'viewer_id': 'viewer_reference', 'site_id': 'site_reference', 'camera_ids': ['cam_reference'],
                     'token_sha256': hashlib.sha256(viewer.encode()).hexdigest()}],
        'publishers': [{'username': 'publisher_reference', 'camera_id': 'cam_reference',
                        'token_sha256': hashlib.sha256(publisher.encode()).hexdigest()}],
    }
    write('viewer.token', viewer + '\n')
    write('publisher.token', publisher + '\n')
    write('config.json', json.dumps(config, indent=2) + '\n')
    return directory / 'config.json'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--media-origin', default='https://localhost:8889')
    args = parser.parse_args()
    try:
        config = initialize(args.directory, args.media_origin)
    except (ValueError, OSError):
        parser.exit(1, 'Initialization failed: use a new directory and valid HTTPS origin.\n')
    print(f'Created {config}; credentials remain in private files, not printed.')
