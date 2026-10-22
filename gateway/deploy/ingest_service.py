"""Optional strict-profile ingest unit; no installation or mutable identity handling."""
import ipaddress
import vpn


def configuration(camera, width=640, height=480, acknowledge_http=False):
    if acknowledge_http is not True or not isinstance(camera, str):
        raise ValueError('explicit trusted-LAN HTTP acknowledgement required')
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
    return dict(camera=f'{address}:{int(port)}', allow_insecure_camera_http=True,
        rtsp='127.0.0.1:8554', stream_path='reference', publisher_user='publisher_reference',
        publisher_token_file='/run/nxrs-ingest/publisher.token', ffmpeg='/usr/bin/ffmpeg',
        width=width, height=height, frame_timeout_ms=3000)


def service(camera_tls=False):
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
    if camera_tls:
        credentials = ['camera-ca.pem','camera-peer.pem','camera-client.pem','camera-client.key','camera-crl.pem']
        for name in credentials:
            text += ('\n[Service]\nLoadCredential='+name+':/etc/nxrs-monitor/'+name+'\n'
                     'ExecStartPre=/usr/bin/install -m 0600 %d/'+name+' /run/nxrs-ingest/'+name+'\n')
        command = 'ExecStart=/usr/local/libexec/nxrs-camera-ingest /run/nxrs-ingest/config.json\n'
        if text.count(command)!=1: raise ValueError('ingest command changed')
        text = text.replace(command,command.rstrip('\n')+' --require-camera-tls\n',1)
    return text + ('\n[Unit]\nPartOf=nxrs-monitor.target\n'
        'BindsTo=nxrs-control.service nxrs-media.service\nAfter=nxrs-control.service nxrs-media.service\n'
        '\n[Service]\nKillMode=control-group\nSendSIGKILL=yes\nLimitCORE=0\n')
