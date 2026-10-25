#!/usr/bin/env python3
"""Render a NEW encrypted-volume overlay for an existing private activity bundle.

No disk formatting, mounting, unlock, enrollment replacement or service operation.
The controller unit reuses the reviewed strict profile, preserving denial state.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import strict_vpn
from activity import write

MOUNT='/srv/nxrs'
MAPPING='nxrsrecordings'

def read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        meta=os.fstat(source.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink!=1 or meta.st_mode&0o077 or meta.st_size>65536:
            raise ValueError('private bounded regular configuration required')
        data=source.read(65537)
        if len(data)>65536: raise ValueError('configuration too large')
        return json.loads(data)

def units():
    base=strict_vpn.services()
    control=base['nxrs-control.service']
    result={}
    old=' --security /run/nxrs-control/security.json\n'
    control=strict_vpn.replace_once(control,old,old.rstrip('\n')+' --require-encrypted-storage\n')
    boundary=('\n[Unit]\nBindsTo=srv-nxrs.mount\nAfter=srv-nxrs.mount\nRequiresMountsFor=/srv/nxrs\n'
              '\n[Service]\nReadWritePaths=/srv/nxrs\n')
    start=next(line for line in control.splitlines() if line.startswith('ExecStart='))
    result['nxrs-control.service.d/encrypted-storage.conf']=boundary+'ExecStart=\n'+start+'\n'
    result['nxrs-activity.service']=(
        '[Unit]\nDescription=nxrs encrypted motion recording\n'
        'BindsTo=nxrs-control.service nxrs-media.service srv-nxrs.mount\n'
        'After=nxrs-control.service nxrs-media.service srv-nxrs.mount\n'
        'PartOf=nxrs-monitor.target\nRequiresMountsFor=/srv/nxrs\n'
        '[Service]\nType=simple\nUser=nxrs-control\nGroup=nxrs-control\nUMask=0077\n'
        'LoadCredential=config:/etc/nxrs-monitor/activity.json\n'
        'LoadCredential=recorder:/etc/nxrs-monitor/recorder.token\n'
        'RuntimeDirectory=nxrs-activity\nRuntimeDirectoryMode=0700\n'
        'ExecStartPre=/usr/bin/install -m 0600 %d/config /run/nxrs-activity/config.json\n'
        'ExecStartPre=/usr/bin/install -m 0600 %d/recorder /run/nxrs-activity/recorder.token\n'
        'ExecStartPre=/usr/local/libexec/nxrs-media-sandbox --check\n'
        'ExecStart=/usr/local/libexec/nxrs-camera-activity /run/nxrs-activity/config.json --require-encrypted-storage --require-media-isolation\n'
        'Restart=no\nTimeoutStopSec=35\nKillMode=control-group\n'
        'NoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n'
        'PrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\n'
        'ProtectControlGroups=yes\nRestrictSUIDSGID=yes\nCapabilityBoundingSet=\n'
        'RestrictAddressFamilies=AF_INET AF_UNIX\nReadOnlyPaths=/run\nReadWritePaths=/srv/nxrs\n'
        'MemoryMax=384M\nTasksMax=32\nLimitCORE=0\n')
    result['nxrs-monitor.target.d/encrypted-storage.conf']='[Unit]\nBindsTo=nxrs-activity.service srv-nxrs.mount\nAfter=nxrs-activity.service srv-nxrs.mount\n'
    result['srv-nxrs.mount']=(
        '[Unit]\nDescription=nxrs encrypted recording volume\n'
        'Requires=systemd-cryptsetup@nxrsrecordings.service\n'
        'After=systemd-cryptsetup@nxrsrecordings.service\n'
        'BindsTo=dev-mapper-nxrsrecordings.device\n'
        '[Mount]\nWhat=/dev/mapper/nxrsrecordings\nWhere=/srv/nxrs\n'
        'Type=ext4\nOptions=rw,nosuid,nodev,noexec\nTimeoutSec=30\n')
    return result

def generate(source,output,uuid):
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',uuid):
        raise ValueError('literal LUKS2 UUID required')
    source=Path(source).resolve(strict=True);output=Path(output).absolute()
    if source.stat().st_mode&0o077 or not source.is_dir(): raise ValueError('private activity bundle required')
    control=read(source/'control.json');worker=read(source/'activity.json')
    if worker['storage']!=control.get('activity') or not control.get('recorders'):
        raise ValueError('matching existing controller and recorder enrollment required')
    if worker['storage'].get('protection'): raise ValueError('new volume overlay required')
    if not isinstance(worker.get('ffmpeg'),str) or not Path(worker['ffmpeg']).is_absolute():
        raise ValueError('absolute selected media executable required')
    # Keep identities/digests and denial state; source and existing output are never rewritten.
    protection=dict(layout_version=2,mount=MOUNT,luks_uuid=uuid)
    storage=dict(worker['storage'],directory=MOUNT+'/activity',protection=protection)
    worker.update(storage=storage,token_file='/run/nxrs-activity/recorder.token',
                  reader='/usr/local/libexec/nxrs-rtsp-reader',media_sandbox='/usr/local/libexec/nxrs-media-sandbox')
    control['activity']=storage
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    write(output/'config.json',control);write(output/'activity.json',worker)
    for name,text in units().items():
        destination=output/name
        if destination.parent!=output:destination.parent.mkdir(mode=0o700,exist_ok=True)
        write(destination,text)
    write(output/'crypttab.fragment',f'{MAPPING} UUID={uuid} /run/nxrs-unlock/manual-only.key luks,noauto,keyfile-timeout=1s,timeout=60,tries=1,password-echo=no\n')
    write(output/'deployment.json',dict(version=1,requires_existing_strict_enrollment=True,
        preserves_denial_state=True,volume=MOUNT,luks_uuid=uuid,manual_unlock_required=True,protected_layout_version=2,maintenance_directory=MOUNT+'/maintenance',
        keys_copied=False,backup_configured=False,service_installation_required=True))
    return output

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--luks-uuid',required=True);a=p.parse_args();generate(a.source,a.output,a.luks_uuid)
    print('Encrypted storage overlay prepared; existing keys/state preserved; no disks or services changed.')
