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


def services():
    """Reuse all base hardening and network dependencies; change lifecycle explicitly."""
    units = vpn.services()
    for name in ['nxrs-control', 'nxrs-media', 'nxrs-proxy']:
        unit = units[name + '.service']
        unit = replace_once(unit, '\n[Install]\nWantedBy=multi-user.target\n', '')
        unit = replace_once(unit, 'Restart=on-failure\n', 'Restart=no\n')
        # MountAPIVFS can preserve a writable /run mount despite ProtectSystem=strict.
        # Explicitly protect it; RuntimeDirectory supplies only each service's writable
        # exception. Keep the runtime write-denial probe, not just a property assertion.
        unit = replace_once(unit, 'ProtectSystem=strict\n',
                            'ProtectSystem=strict\nReadOnlyPaths=/run\n')
        unit += '\n[Unit]\nPartOf=nxrs-monitor.target\n'
        if name != 'nxrs-control':
            unit += 'BindsTo=nxrs-control.service\nAfter=nxrs-control.service\n'
        else:
            # A stable account owns mutable denials, avoiding DynamicUser state symlinks.
            unit = replace_once(unit, 'DynamicUser=yes\n', 'DynamicUser=no\n')
            unit = replace_once(unit,
                'ExecStart=/usr/local/libexec/nxrs-gateway-control %d/config\n',
                'ExecStart=/usr/local/libexec/nxrs-gateway-control %d/config --security /run/nxrs-control/security.json\n')
            # systemd may grant access to LoadCredential files using a named-user ACL,
            # reflected in group-mask mode bits. Do NOT relax private_read's rejection
            # of group-readable ordinary files. Stage only this immutable credential,
            # as the unprivileged owner, into a private ephemeral runtime directory.
            # This is not persistent identity initialization and never touches the deny list.
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
    return units


def media_config(address, ports=vpn.PORTS, management_port=MANAGEMENT_PORT):
    config = vpn.media_config(address, ports)
    config.update(api=True, apiAddress=f'127.0.0.1:{management_port}')
    return config


def generate(directory, host, gateway, peer, endpoint, peer_key):
    # Base validates inputs and refuses an existing destination before writing.
    vpn.generate(directory, host, gateway, peer, endpoint, peer_key)
    files = {
        **services(),
        'mediamtx.json': json.dumps(media_config(gateway), indent=2) + '\n',
        'security.json': json.dumps({'revocations_file': STATE,
            'media_api': f'127.0.0.1:{MANAGEMENT_PORT}',
            'media_api_token': secrets.token_hex(32)}, indent=2) + '\n',
        'nxrs-control.sysusers': 'u nxrs-control - "nxrs monitoring control" /var/lib/nxrs-control /usr/sbin/nologin\n',
    }
    for name, content in files.items():
        with open(directory / name, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
            f.write(content)
    # Written last: partial output is not a complete strict deployment.
    with open(directory / 'deployment.json', 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        json.dump({'version': 1, 'mode': 'strict', 'target': 'nxrs-monitor.target',
                   'state_must_be_provisioned_once': STATE}, f)
        f.write('\n')
    return directory


def initialize_state(directory):
    """Operator-only NEW enrollment. Never run at boot, ExecStartPre or recovery.

    Caller owns a pre-created private, empty directory. Exclusive creation and a
    retained failure marker prevent concurrent/repeated initialization or reset.
    """
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
    seed = commands.add_parser('init-state', help='FIRST enrollment only; never reset missing/corrupt deployed state')
    seed.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'render':
            generate(args.directory, args.host, args.gateway, args.peer, args.endpoint, args.peer_public_key)
        else:
            initialize_state(args.directory)
    except (ValueError, OSError, subprocess.SubprocessError):
        parser.exit(1, 'Operation failed. Preserve existing state; never deploy partial output or reset denials.\n')
    print('Completed explicitly requested local file operation. No services, routes, firewall or trust settings changed.')


if __name__ == '__main__':
    main()
