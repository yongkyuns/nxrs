#!/usr/bin/env python3
"""Render an opt-in, single-host IPv4 WireGuard deployment. Never apply host changes."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vpn_peer
from vpn_peer import private_address, public_key

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from init_reference import initialize

PORTS = dict(https=8443, media=8889, ice=8189, rtsp=8554, api=18080, auth=18081, vpn=51820)


def hostname(value):
    if len(value) > 253 or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', value):
        raise ValueError('invalid host')
    if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in value.split('.')):
        raise ValueError('invalid host')
    return value


def safe_path(path):
    text = str(path)
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', text) or '..' in Path(text).parts:
        raise ValueError('unsafe configuration path')
    return text


def nginx_config(host, address, ports=PORTS, cert='/run/credentials/nxrs-proxy.service/server.crt',
                 key='/run/credentials/nxrs-proxy.service/server.key', runtime='/run/nxrs-proxy'):
    hostname(host)
    ipaddress.IPv4Address(address)
    cert, key, runtime = map(safe_path, [cert, key, runtime])
    def route(pattern, method, upstream, cap):
        return f'''        location {pattern} {{
            if ($request_method != {method}) {{ return 405; }}
            client_max_body_size {cap};
            proxy_pass http://127.0.0.1:{upstream};
        }}\n'''
    routes = route('= /healthz', 'GET', ports['api'], '1k')
    routes += route('= /v1/cameras', 'GET', ports['api'], '1k')
    routes += route('~ "^/v1/cameras/[A-Za-z0-9_-]{1,64}/playback-grants$"', 'POST', ports['api'], '16k')
    # Exact public recording API; camera/owner authorization remains in Rust.
    # Do not expose a general /v1 prefix or any filesystem/admin endpoint.
    activity='/v1/cameras/[A-Za-z0-9_-]{1,64}/activity'
    event='[0-9a-f]{32}'
    for suffix,method in [('', 'GET'),('/before/'+event, 'GET'),('/kept', 'GET'),('/kept/before/'+event, 'GET'),
        ('/'+event+'/clip', 'GET'),('/'+event+'/clip/parts/(?:[0-9]|[1-5][0-9]|6[0-3])', 'GET'),
        ('/'+event+'/thumbnail', 'GET'),('/'+event+'/keep', 'POST'),('/'+event, 'DELETE'),('/'+event+'/delete', 'POST')]:
        routes += route('~ "^'+activity+suffix+'$"',method,ports['api'],'1k')
    routes += route('= /reference/whep', 'POST', ports['media'], '256k')
    routes += route('~ "^/reference/whep/[A-Za-z0-9_-]{1,128}$"', 'DELETE', ports['media'], '1k')
    return f'''# Native-client profile: no browser UI, WHIP publishing, admin, redirects or query credentials.
# Run unprivileged via systemd. No request/error log may retain capability URLs.
daemon off;
master_process off;
worker_processes 1;
pid {runtime}/nginx.pid;
error_log /dev/null emerg;
events {{ worker_connections 256; }}
http {{
    access_log off;
    server_tokens off;
    client_body_temp_path {runtime}/body;
    proxy_temp_path {runtime}/proxy;
    # Compiled modules initialize these directories even without matching routes.
    fastcgi_temp_path {runtime}/fastcgi;
    uwsgi_temp_path {runtime}/uwsgi;
    scgi_temp_path {runtime}/scgi;
    client_header_timeout 5s;
    client_body_timeout 5s;
    send_timeout 10s;
    keepalive_timeout 10s;
    keepalive_requests 100;
    large_client_header_buffers 2 16k;
    limit_conn_zone $binary_remote_addr zone=per_peer:1m;
    limit_req_zone $binary_remote_addr zone=requests:1m rate=5r/s;
    server {{
        listen {address}:{ports['https']} ssl default_server;
        server_name {host};
        ssl_certificate {cert};
        ssl_certificate_key {key};
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;
        merge_slashes off;
        if ($host != {host}) {{ return 421; }}
        if ($request_uri != $uri) {{ return 400; }}
        limit_conn per_peer 16;
        limit_conn_status 429;
        limit_req zone=requests burst=20 nodelay;
        limit_req_status 429;
        add_header Cache-Control "no-store" always;
        add_header X-Content-Type-Options "nosniff" always;
        proxy_hide_header Cache-Control;
        proxy_hide_header X-Content-Type-Options;
        proxy_hide_header Server;
        proxy_hide_header Set-Cookie;
        proxy_http_version 1.1;
        proxy_pass_request_headers off;
        proxy_set_header Host "{host}:{ports['https']}";
        proxy_set_header Authorization $http_authorization;
        proxy_set_header Content-Type $http_content_type;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_request_buffering on;
        proxy_max_temp_file_size 0;
        proxy_connect_timeout 2s;
        proxy_read_timeout 10s;
        proxy_send_timeout 10s;
        proxy_next_upstream off;
        proxy_redirect off;
{routes}        location / {{ return 404; }}
    }}
}}
'''


def media_config(address, ports=PORTS):
    return dict(logLevel='error', logDestinations=['stdout'],
        authMethod='http', authHTTPAddress=f"http://127.0.0.1:{ports['auth']}/internal/mediamtx/auth", authHTTPExclude=[],
        api=False, metrics=False, pprof=False, playback=False, rtmp=False, hls=False, srt=False, moq=False,
        rtsp=True, rtspAddress=f"127.0.0.1:{ports['rtsp']}", rtspTransports=['tcp'], rtspAuthMethods=['basic'],
        webrtc=True, webrtcAddress=f"127.0.0.1:{ports['media']}", webrtcEncryption=False,
        webrtcLocalUDPAddress=f"{address}:{ports['ice']}", webrtcLocalTCPAddress='',
        webrtcIPsFromInterfaces=False, webrtcAdditionalHosts=[address], webrtcICEServers2=[],
        paths={'reference': dict(source='publisher', overridePublisher=False, maxReaders=4)})


def firewall(address, peer):
    return f'''# Atomic/idempotent replacement of ONLY our table; never flush the host ruleset.
add table inet nxrs_monitor
flush table inet nxrs_monitor
table inet nxrs_monitor {{
    chain input {{
        type filter hook input priority -10; policy accept;
        iifname "lo" accept
        ip daddr {address} iifname != "wg-nxrs" drop
        iifname "wg-nxrs" ip saddr {peer} ip daddr {address} tcp dport 8443 accept
        iifname "wg-nxrs" ip saddr {peer} ip daddr {address} udp dport 8189 accept
        iifname "wg-nxrs" drop
    }}
    chain output {{
        type filter hook output priority -10; policy accept;
        ip saddr {address} oifname != "lo" oifname != "wg-nxrs" drop
    }}
    chain forward {{
        type filter hook forward priority -10; policy accept;
        iifname "wg-nxrs" drop
        oifname "wg-nxrs" drop
    }}
}}
'''


def services():
    common = '''[Unit]
After=network-online.target wg-quick@wg-nxrs.service nxrs-firewall.service
Requires=wg-quick@wg-nxrs.service nxrs-firewall.service
BindsTo=wg-quick@wg-nxrs.service
[Service]
Type=simple
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_INET AF_UNIX
CapabilityBoundingSet=
UMask=0077
LimitNOFILE=512
TasksMax=128
MemoryMax=256M
Restart=on-failure
RestartSec=3
TimeoutStopSec=10
'''
    specs = {
        'nxrs-control': 'LoadCredential=config:/etc/nxrs-monitor/config.json\nExecStart=/usr/local/libexec/nxrs-gateway-control %d/config\n',
        'nxrs-media': 'LoadCredential=config:/etc/nxrs-monitor/mediamtx.json\nExecStart=/usr/local/libexec/nxrs-mediamtx %d/config\nStandardOutput=null\nStandardError=null\n',
        'nxrs-proxy': 'RuntimeDirectory=nxrs-proxy\nRuntimeDirectoryMode=0700\nUnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GLIBC_TUNABLES\nLoadCredential=config:/etc/nxrs-monitor/nginx.conf\nLoadCredential=server.crt:/etc/nxrs-monitor/tls/fullchain.pem\nLoadCredential=server.key:/etc/nxrs-monitor/tls/privkey.pem\nExecStartPre=/usr/local/libexec/nxrs-gateway-proxy --selection /usr/local/lib/nxrs-proxy/active.json --runtime /run/nxrs-proxy --config %d/config --check\nExecStart=/usr/local/libexec/nxrs-gateway-proxy --selection /usr/local/lib/nxrs-proxy/active.json --runtime /run/nxrs-proxy --config %d/config\nKillSignal=SIGQUIT\n'
    }
    result = {name+'.service': common + f'User={name}\n' + spec + '\n[Install]\nWantedBy=multi-user.target\n' for name, spec in specs.items()}
    # Pion transport's stdnet.NewNet queries net.Interfaces even with explicit ICE
    # hosts. Go uses unprivileged RTM_GETLINK/GETADDR netlink requests on Linux.
    # Permit the socket family only for media; keep all capabilities empty, no
    # AF_PACKET/raw IP/IPv6 sockets, and no network-administration privileges.
    result['nxrs-media.service'] = result['nxrs-media.service'].replace(
        'RestrictAddressFamilies=AF_INET AF_UNIX\n',
        'RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK\n')
    result['nxrs-firewall.service'] = '''[Unit]
Before=wg-quick@wg-nxrs.service nxrs-proxy.service nxrs-media.service nxrs-control.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nxrs-monitor/firewall.nft
# Deliberately no ExecStop: stopping services must not open the host or LAN.
[Install]
WantedBy=multi-user.target
'''
    return result


def network_admission():
    return '''[Service]
ExecStartPre=/usr/local/libexec/nxrs-vpn-admission --config /etc/wireguard/wg-nxrs.conf --history /etc/wireguard/vpn-peers.json --firewall /etc/nxrs-monitor/firewall.nft
UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GLIBC_TUNABLES
'''


def generate(directory, host, gateway, peer, endpoint, peer_key):
    host = hostname(host)
    gateway, peer = private_address(gateway), private_address(peer)
    if gateway == peer:
        raise ValueError('distinct addresses required')
    hostname(endpoint)
    public_key(peer_key)
    if directory.exists() or directory.is_symlink():
        raise FileExistsError('destination exists')
    def wg(args, data=None):
        return subprocess.run(['wg']+args, input=data, capture_output=True, check=True, timeout=5).stdout
    secret = wg(['genkey'])
    server_public = wg(['pubkey'], secret).decode().strip()
    public_key(server_public)
    wireguard = vpn_peer.configuration(gateway, peer, secret.decode('ascii').strip(), peer_key)
    history = vpn_peer.initial_history(wireguard.encode(), server_public)
    initialize(directory, f'https://{host}:{PORTS["https"]}')
    files = {
        'nginx.conf': nginx_config(host, gateway),
        'mediamtx.json': json.dumps(media_config(gateway), indent=2)+'\n',
        'firewall.nft': firewall(gateway, peer),
        'wg-nxrs.conf': wireguard,
        'wg-nxrs-admission.conf': network_admission(),
        'phone-settings.txt': f'Keep the private key generated ON THE PHONE. Do not replace it with the gateway key.\nInterface address: {peer}/32\nMTU: 1380\nPeer public key: {server_public}\nEndpoint: {endpoint}:51820\nAllowed IPs: {gateway}/32\nPersistent keepalive: 25\nApp gateway and media origin: https://{host}:8443\nNo DNS override or LAN/default route is installed.\n',
        **services(),
    }
    for name, text in files.items():
        with open(directory/name, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write(text)
    # First enrollment only: generation refuses an existing bundle. Recovery and
    # rotation retain this history and cannot silently seed a missing file.
    vpn_peer.write_private(directory / vpn_peer.HISTORY, vpn_peer.encoded(history))
    vpn_peer.sync_directory(directory)
    return directory


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--host', required=True, help='TLS name resolving to the gateway tunnel address, or that IPv4 address')
    parser.add_argument('--gateway', default='10.77.0.1')
    parser.add_argument('--peer', default='10.77.0.2')
    parser.add_argument('--endpoint', required=True, help='Public home WireGuard DNS name or IPv4 address (no port)')
    parser.add_argument('--peer-public-key', required=True, help='Public key exported from the phone; never its private key')
    args = parser.parse_args()
    try:
        generate(args.directory, args.host, args.gateway, args.peer, args.endpoint, args.peer_public_key)
    except (ValueError, OSError, subprocess.SubprocessError):
        parser.exit(1, 'Generation failed; verify inputs, wg installation and a NEW private destination. Partial output must not be deployed.\n')
    print('Private deployment files generated. No services, firewall, routes or trust settings were changed.')
