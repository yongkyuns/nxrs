#!/usr/bin/env python3
"""Render strict VPN services or explicitly seed NEW state. Never install/start services."""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess

import vpn

STATE = '/var/lib/nxrs-control/revocations.json'
MANAGEMENT_PORT = 9997


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('base deployment template changed; strict profile needs review')
    return text.replace(old, new, 1)


def services(include_ingest=False):
    """Reuse base hardening. Ingest failure leaves control available for diagnosis."""
    units = vpn.services()
    for name in ['nxrs-control', 'nxrs-media', 'nxrs-proxy']:
        unit = units[name + '.service']
        unit = replace_once(unit, '\n[Install]\nWantedBy=multi-user.target\n', '')
        unit = replace_once(unit, 'Restart=on-failure\n', 'Restart=no\n')
        # Explicit /run isolation; RuntimeDirectory is the narrow writable exception.
        unit = replace_once(unit, 'ProtectSystem=strict\n',
                            'ProtectSystem=strict\nReadOnlyPaths=/run\n')
        unit += '\n[Unit]\nPartOf=nxrs-monitor.target\n'
        if name != 'nxrs-control':
            unit += 'BindsTo=nxrs-control.service\nAfter=nxrs-control.service\n'
        else:
            unit = replace_once(unit, 'DynamicUser=yes\n', 'DynamicUser=no\n')
            unit = replace_once(unit,
                'ExecStart=/usr/local/libexec/nxrs-gateway-control %d/config\n',
                'ExecStart=/usr/local/libexec/nxrs-gateway-control %d/config --security /run/nxrs-control/security.json\n')
            # Loaded credentials may have named-user ACL mode bits. Stage ONLY the
            # immutable credential; never relax ordinary-file checks or reset denials.
            unit += ('\n[Service]\nLoadCredential=security:/etc/nxrs-monitor/security.json\n'
                     'RuntimeDirectory=nxrs-control\nRuntimeDirectoryMode=0700\n'
                     'ExecStartPre=/usr/bin/install -m 0600 %d/security /run/nxrs-control/security.json\n'
                     'StateDirectory=nxrs-control\nStateDirectoryMode=0700\n')
        units[name + '.service'] = unit
    units['nxrs-monitor.target'] = (
        '[Unit]\nDescription=nxrs strict monitoring service group\n'
        'BindsTo=nxrs-control.service nxrs-media.service nxrs-proxy.service\n'
        'After=nxrs-control.service nxrs-media.service nxrs-proxy.service\n'
        '[Install]\nWantedBy=multi-user.target\n')
    if include_ingest:
        import ingest_service
        units['nxrs-ingest.service'] = ingest_service.service()
        units['nxrs-monitor.target'] += '\n[Unit]\nWants=nxrs-ingest.service\n'
    return units


def media_config(address, ports=vpn.PORTS, management_port=MANAGEMENT_PORT):
    config = vpn.media_config(address, ports)
    config.update(api=True, apiAddress=f'127.0.0.1:{management_port}')
    return config


def generate(directory, host, gateway, peer, endpoint, peer_key, *, camera=None,
             width=640, height=480, allow_insecure_camera_http=False, source_kind='synthetic'):
    ingest = None
    if camera is not None:
        import ingest_service
        ingest = ingest_service.configuration(camera, width, height, allow_insecure_camera_http)
    elif allow_insecure_camera_http:
        raise ValueError('camera endpoint required with HTTP acknowledgement')
    if source_kind not in ['synthetic', 'physical'] or (source_kind == 'physical' and camera is None):
        raise ValueError('source classification requires an explicit camera')
    # Validate before creating keys/files. Base refuses an existing destination.
    vpn.generate(directory, host, gateway, peer, endpoint, peer_key)
    files = {
        **services(include_ingest=ingest is not None),
        'mediamtx.json': json.dumps(media_config(gateway), indent=2) + '\n',
        'security.json': json.dumps({'revocations_file': STATE,
            'media_api': f'127.0.0.1:{MANAGEMENT_PORT}',
            'media_api_token': secrets.token_hex(32)}, indent=2) + '\n',
        'nxrs-control.sysusers': 'u nxrs-control - "nxrs monitoring control" /var/lib/nxrs-control /usr/sbin/nologin\n',
    }
    if ingest is not None:
        files['ingest.json'] = json.dumps(ingest, indent=2) + '\n'
        catalog = json.loads((directory/'config.json').read_text())
        catalog['cameras'][0]['source'] = source_kind
        catalog['cameras'][0]['name'] = ('Configured nxrs camera' if source_kind == 'physical'
                                        else 'SYNTHETIC Rust camera-stream reference')
        files['config.json'] = json.dumps(catalog, indent=2) + '\n'
    for name, content in files.items():
        with open(directory / name, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
            f.write(content)
    # Written last: partial output is never a completed strict deployment.
    with open(directory / 'deployment.json', 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        json.dump({'version': 1, 'mode': 'strict', 'target': 'nxrs-monitor.target',
                   'state_must_be_provisioned_once': STATE, 'includes_ingest': ingest is not None}, f)
        f.write('\n')
    return directory


def initialize_state(directory):
    """Operator-only NEW enrollment. Never run at boot, ExecStartPre or recovery."""
    directory = Path(directory)
    if os.name != 'posix' or not directory.is_absolute():
        raise ValueError('absolute Unix state directory required')
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise ValueError('state directory must be private and owned by the executing service user')
        if os.listdir(fd):
            raise FileExistsError('state initialization requires an EMPTY directory')
        marker = os.open('.nxrs-initializing', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        os.close(marker)
        os.fsync(fd)
        snapshot = os.open('revocations.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        with os.fdopen(snapshot, 'w') as f:
            f.write('{"version":1,"revoked":[]}\n')
            f.flush()
            os.fsync(f.fileno())
        os.fsync(fd)
        os.unlink('.nxrs-initializing', dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    render = commands.add_parser('render', help='generate a NEW private strict deployment bundle')
    render.add_argument('directory', type=Path)
    render.add_argument('--host', required=True)
    render.add_argument('--gateway', default='10.77.0.1')
    render.add_argument('--peer', default='10.77.0.2')
    render.add_argument('--endpoint', required=True)
    render.add_argument('--peer-public-key', required=True)
    render.add_argument('--camera', help='optional literal private IPv4:port; one existing /stream reader')
    render.add_argument('--width', type=int, default=640)
    render.add_argument('--height', type=int, default=480)
    render.add_argument('--allow-insecure-camera-http', action='store_true')
    render.add_argument('--source-kind', choices=['synthetic', 'physical'], default='synthetic')
    seed = commands.add_parser('init-state', help='FIRST enrollment only; never reset deployed state')
    seed.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'render':
            generate(args.directory, args.host, args.gateway, args.peer, args.endpoint, args.peer_public_key,
                     camera=args.camera, width=args.width, height=args.height,
                     allow_insecure_camera_http=args.allow_insecure_camera_http, source_kind=args.source_kind)
        else:
            initialize_state(args.directory)
    except (ValueError, OSError, subprocess.SubprocessError):
        parser.exit(1, 'Operation failed. Preserve existing state; never deploy partial output or reset denials.\n')
    print('Completed local file operation. No services, routes, firewall or trust settings changed.')


if __name__ == '__main__':
    main()
