"""Optional strict-profile ingest unit; no installation or mutable identity handling."""
import ipaddress
import re
import vpn

TLS_FILES = dict(ca_certificate_file='camera-ca.pem', camera_certificate_file='camera-peer.pem',
                 client_certificate_file='camera-client.pem', client_key_file='camera-client.key',
                 crl_file='camera-crl.pem')


def configuration(camera, width=640, height=480, acknowledge_http=False, detections=False, *, camera_tls=None):
    if type(acknowledge_http) is not bool or acknowledge_http == (camera_tls is not None):
        raise ValueError('choose TLS enrollment or explicit trusted-LAN HTTP acknowledgement')
    if not isinstance(camera, str):
        raise ValueError('literal IPv4:port camera required')
    host, separator, port = camera.rpartition(':')
    if not separator or not port.isascii() or not port.isdecimal():
        raise ValueError('literal IPv4:port camera required')
    address = ipaddress.IPv4Address(host)
    ranges = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8']
    if not any(address in ipaddress.IPv4Network(network) for network in ranges) or not 1 <= int(port) <= 65535:
        raise ValueError('private or loopback camera endpoint required')
    if (address == ipaddress.IPv4Address('127.0.0.1') and int(port) == 8554) \
            or any(type(v) is not int or v % 2 for v in [width, height]) \
            or not 2 <= width <= 1920 or not 2 <= height <= 1080:
        raise ValueError('invalid source, dimensions or publisher collision')
    if type(detections) is not bool:
        raise ValueError('explicit metadata option required')
    config = dict(camera=f'{address}:{int(port)}', allow_insecure_camera_http=acknowledge_http,
        rtsp='127.0.0.1:8554', stream_path='reference', publisher_user='publisher_reference',
        publisher_token_file='/run/nxrs-ingest/publisher.token', ffmpeg='/usr/bin/ffmpeg',
        publisher='/usr/local/libexec/nxrs-rtsp-publisher',
        width=width, height=height, frame_timeout_ms=3000)
    if camera_tls is not None:
        fields = {'site_id', 'camera_id', 'gateway_id', 'issuer_sha256', 'enrollment_generation'}
        if not isinstance(camera_tls, dict) or set(camera_tls) != fields \
                or any(not isinstance(camera_tls[k], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', camera_tls[k])
                       for k in ['site_id', 'camera_id', 'gateway_id']) \
                or camera_tls['camera_id'] != 'cam_reference' \
                or not isinstance(camera_tls['issuer_sha256'], str) \
                or not re.fullmatch(r'[0-9a-f]{64}', camera_tls['issuer_sha256']) \
                or type(camera_tls['enrollment_generation']) is not int \
                or not 1 <= camera_tls['enrollment_generation'] <= 1_000_000_000:
            raise ValueError('invalid TLS enrollment for the generated cam_reference catalog')
        config['camera_tls'] = {**camera_tls, **{k: '/run/nxrs-ingest/'+v for k, v in TLS_FILES.items()}}
    if detections:
        config['detections'] = dict(control='127.0.0.1:18081', camera_id='cam_reference')
    return config


def service(require_tls=False):
    # Reuse the base controller's restrictions, not the media-only Netlink exception.
    text = vpn.services()['nxrs-control.service']
    replacements = [
        ('User=nxrs-control\n', 'User=nxrs-ingest\n'),
        ('LoadCredential=config:/etc/nxrs-monitor/config.json\n',
         'LoadCredential=config:/etc/nxrs-monitor/ingest.json\nLoadCredential=publisher:/etc/nxrs-monitor/publisher.token\n'),
        ('ExecStart=/usr/local/libexec/nxrs-gateway-control %d/config\n',
         'RuntimeDirectory=nxrs-ingest\nRuntimeDirectoryMode=0700\n'
         'ExecStartPre=/usr/bin/install -m 0600 %d/config /run/nxrs-ingest/config.json\n'
         'ExecStartPre=/usr/bin/install -m 0600 %d/publisher /run/nxrs-ingest/publisher.token\n'
         'ExecStart=/usr/local/libexec/nxrs-camera-ingest /run/nxrs-ingest/config.json\n'),
        ('ProtectSystem=strict\n', 'ProtectSystem=strict\nReadOnlyPaths=/run\n'),
        ('Restart=on-failure\n', 'Restart=no\n'),
        ('\n[Install]\nWantedBy=multi-user.target\n', ''),
    ]
    for old, new in replacements:
        if text.count(old) != 1:
            raise ValueError('base service template changed; ingest policy needs review')
        text = text.replace(old, new, 1)
    if require_tls:
        text += '\n[Service]\n'
        for name in TLS_FILES.values():
            text += (f'LoadCredential={name}:/etc/nxrs-monitor/{name}\n'
                     f'ExecStartPre=/usr/bin/install -m 0600 %d/{name} /run/nxrs-ingest/{name}\n')
        command = '/usr/local/libexec/nxrs-camera-ingest /run/nxrs-ingest/config.json'
        text = text.replace('ExecStart='+command+'\n', 'ExecStart='+command+' --require-camera-tls\n')
        text += 'ExecStartPre='+command+' --require-camera-tls --check-config\n'
    return text + ('\n[Unit]\nPartOf=nxrs-monitor.target\n'
        'BindsTo=nxrs-control.service nxrs-media.service\nAfter=nxrs-control.service nxrs-media.service\n'
        '\n[Service]\nKillMode=control-group\nSendSIGKILL=yes\nLimitCORE=0\n')
