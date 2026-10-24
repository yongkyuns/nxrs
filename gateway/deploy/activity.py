#!/usr/bin/env python3
"""Prepare private motion-recording configuration. Never start services or replace enrollment.

The output controller config retains existing viewer/publisher identities. Only
the explicitly named owner gains recording management. Recorder keys are new,
camera-scoped backend credentials and must never be copied to an iPhone.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import stat


def identifier(value):
    return isinstance(value, str) and 1 <= len(value) <= 64 and all(c.isascii() and (c.isalnum() or c in '_-') for c in value)


def write(path, value):
    data = value if isinstance(value, bytes) else (value if isinstance(value, str) else json.dumps(value, indent=2)+'\n').encode()
    with open(path, 'xb', opener=lambda p,f: os.open(p,f,0o600)) as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def generate(source, output, *, camera, owner, worker, ffmpeg, rtsp='127.0.0.1:8554',
             reader='/usr/local/libexec/nxrs-rtsp-reader', media_sandbox=None):
    import ipaddress
    source, output = Path(source).absolute(), Path(output).absolute()
    if not Path(reader).is_absolute() or (media_sandbox is not None and not Path(media_sandbox).is_absolute()):
        raise ValueError('absolute reader/isolation binaries required')
    if not identifier(camera) or not identifier(owner) or not Path(worker).is_absolute() or not Path(ffmpeg).is_absolute():
        raise ValueError('explicit camera/owner identifiers and absolute binaries required')
    host, separator, port = rtsp.rpartition(':')
    if not separator or not ipaddress.IPv4Address(host).is_loopback or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError('loopback RTSP endpoint required')
    meta = source.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_nlink != 1 or meta.st_size > 65536:
        raise ValueError('private bounded gateway configuration required')
    config = json.loads(source.read_text())
    cameras = [c for c in config['cameras'] if c['camera_id'] == camera]
    owners = [v for v in config['viewers'] if v['viewer_id'] == owner and camera in v['camera_ids']]
    if len(cameras) != 1 or len(owners) != 1 or config.get('activity') or config.get('recorders'):
        raise ValueError('one enrolled camera/owner and a new activity deployment required')
    if not output.is_absolute() or '..' in output.parts or any(c in str(output) for c in ['\n','\r',chr(39),chr(34),',']):
        raise ValueError('absolute new output directory required')
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    output = output.resolve()
    token = secrets.token_hex(32)
    storage = dict(directory=str(output/'footage'), camera_ids=[camera], retention_seconds=3*86400,
                   max_bytes=1024*1024*1024, max_events=1000)
    config['activity'] = storage
    config['recorders'] = [dict(username='recorder_'+camera[:48], camera_id=camera,
                                token_sha256=hashlib.sha256(token.encode()).hexdigest())]
    owners[0]['manage_recordings'] = True
    observer = dict(storage=storage, camera_id=camera, rtsp=rtsp, stream_path=cameras[0]['stream_path'],
                    username=config['recorders'][0]['username'], token_file=str(output/'recorder.token'),
                    ffmpeg=str(ffmpeg), reader=str(reader), media_sandbox=str(media_sandbox) if media_sandbox else None, motion_percent=3, pre_roll_seconds=6, post_roll_seconds=8, max_clip_seconds=30)
    write(output/'control.json',config); write(output/'activity.json',observer); write(output/'recorder.token',token+'\n')
    label = 'org.nxrs.home.activity'
    agent = dict(Label=label, ProgramArguments=[str(worker),str(output/'activity.json')],
                 RunAtLoad=True, KeepAlive=dict(SuccessfulExit=False), ThrottleInterval=5,
                 StandardOutPath=str(output/'activity.log'), StandardErrorPath=str(output/'activity.log'),
                 Umask=0o077, ProcessType='Background')
    write(output/(label+'.plist'),plistlib.dumps(agent))
    write(output/'nxrs-activity.service',
        '[Unit]\nDescription=Nxrs home motion recording\nAfter=nxrs-media.service nxrs-control.service\n'
        '[Service]\nType=simple\nUser=nxrs-control\nGroup=nxrs-control\nUMask=0077\n'
        'ExecStartPre=/usr/local/libexec/nxrs-media-sandbox --check\n'
        'ExecStart=/usr/local/libexec/nxrs-camera-activity /etc/nxrs-monitor/activity.json --require-media-isolation\n'
        'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=35\nKillMode=control-group\n'
        'NoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n'
        'RestrictAddressFamilies=AF_INET AF_UNIX\nReadWritePaths=/var/lib/nxrs-activity\n'
        'MemoryMax=384M\nTasksMax=32\nLimitCORE=0\n[Install]\nWantedBy=multi-user.target\n')
    # Completion marker last. The rendered Linux paths require an explicit installation mapping.
    write(output/'deployment.json', dict(version=1, camera_id=camera, includes_private_recorder_key=True,
        controller_config='control.json', activity_config='activity.json', original_enrollment_preserved=True,
        service_installation_required=True, linux_state_directory='/var/lib/nxrs-activity'))
    return output


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',type=Path); p.add_argument('output',type=Path)
    p.add_argument('--camera',required=True); p.add_argument('--owner',required=True)
    p.add_argument('--worker',type=Path,required=True); p.add_argument('--ffmpeg',type=Path,required=True)
    p.add_argument('--reader',type=Path,default=Path('/usr/local/libexec/nxrs-rtsp-reader'))
    p.add_argument('--media-sandbox',type=Path)
    p.add_argument('--rtsp',default='127.0.0.1:8554')
    a=p.parse_args()
    generate(a.source,a.output,camera=a.camera,owner=a.owner,worker=a.worker,ffmpeg=a.ffmpeg,rtsp=a.rtsp,reader=a.reader,media_sandbox=a.media_sandbox)
    print('Private activity bundle prepared; enrollment preserved; no services started.')
