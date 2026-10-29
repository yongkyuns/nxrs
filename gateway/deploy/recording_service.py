"""Opt-in encrypted recording/maintenance backup configuration; no host mutation."""
import json
from pathlib import Path
import uuid

ROOT = '/var/lib/nxrs-recordings'
MOUNT = r'var-lib-nxrs\x2drecordings.mount'
CRYPT = r'systemd-cryptsetup@nxrs\x2drecordings.service'
CHECK = 'nxrs-recording-volume.service'
BACKUP_MOUNT = r'var-lib-nxrs\x2dbackup.mount'


def volume_id(value):
    if not isinstance(value, str):
        raise ValueError('explicit LUKS UUID required')
    parsed = uuid.UUID(value)
    if str(parsed) != value or parsed.int == 0:
        raise ValueError('canonical nonzero LUKS UUID required')
    return value


def files(value, media_service, media_json):
    value = volume_id(value)
    if media_service.count('DynamicUser=yes\n') != 1:
        raise ValueError('media service template changed')
    executable = '/usr/local/libexec/nxrs-mediamtx %d/config\n'
    if media_service.count(executable) != 1:
        raise ValueError('media executable template changed')
    media_service = media_service.replace(executable, '/usr/local/libexec/nxrs-mediamtx-recording %d/config\n', 1)
    media_service = media_service.replace('DynamicUser=yes\n', 'DynamicUser=no\n', 1)
    media_service += f'''
[Unit]
BindsTo={CHECK}
After={CHECK} nxrs-backup.service
Conflicts=nxrs-backup.service
[Service]
Group=nxrs-recordings
ReadWritePaths={ROOT}/media
Environment=TMPDIR={ROOT}/media/.tmp
UMask=0027
LimitCORE=0
'''
    media = json.loads(media_json)
    media['paths']['reference'].update(record=True, recordFormat='fmp4',
        recordPath=ROOT+'/media/%path/%Y-%m-%d_%H-%M-%S-%f', recordPartDuration='1s',
        recordMaxPartSize='8M', recordSegmentDuration='1m', recordDeleteAfter='24h')
    common = f'''[Unit]
BindsTo={CHECK} {BACKUP_MOUNT}
After={CHECK} {BACKUP_MOUNT} nxrs-media.service
Conflicts=nxrs-media.service
AssertPathIsMountPoint=/var/lib/nxrs-backup
[Service]
Type=oneshot
User=nxrs-backup
Group=nxrs-recordings
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateDevices=yes
PrivateTmp=yes
ReadOnlyPaths={ROOT}/media
ReadWritePaths={ROOT}/cache {ROOT}/restore /var/lib/nxrs-backup
LoadCredential=password:/run/nxrs-keys/backup.password
Environment=TMPDIR={ROOT}/cache/tmp
Environment=RESTIC_CACHE_DIR={ROOT}/cache/restic
Environment=GOMAXPROCS=2
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX
UMask=0077
LimitCORE=0
MemoryMax=256M
TasksMax=32
TimeoutStartSec=10min
StandardOutput=null
StandardError=null
ExecStartPre=/usr/bin/findmnt --nocanonicalize -rn -M /var/lib/nxrs-backup -t ext4 -O rw
ExecStartPre=/bin/sh -ec 'test "$$(systemctl show -p ActiveState --value nxrs-media.service)" = inactive; test "$$(systemctl show -p Result --value nxrs-media.service)" = success'
'''
    restic = '/usr/bin/restic --repo /var/lib/nxrs-backup/repository --password-file %d/password'
    return {
        'nxrs-media.service': media_service,
        'mediamtx.json': json.dumps(media, indent=2)+'\n',
        'nxrs-recordings.crypttab': f'nxrs-recordings UUID={value} /run/nxrs-keys/recordings.key luks,noauto,headless,tries=1,timeout=10s\n',
        MOUNT: f'''[Unit]
Requires={CRYPT}
After={CRYPT}
[Mount]
What=/dev/mapper/nxrs-recordings
Where={ROOT}
Type=ext4
Options=nodev,nosuid,noexec,noatime
TimeoutSec=15
''',
        CHECK: f'''[Unit]
BindsTo={MOUNT}
After={MOUNT}
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/libexec/nxrs-recording-volume {value}
TimeoutStartSec=10
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes
PrivateNetwork=yes
CapabilityBoundingSet=CAP_SYS_ADMIN
ProtectKernelTunables=yes
ProtectControlGroups=yes
LimitCORE=0
StandardOutput=null
StandardError=null
''',
        'nxrs-backup-init.service': common+f'ExecStart={restic} init\n',
        'nxrs-backup.service': common+f'ExecStart={restic} backup --one-file-system {ROOT}/media\n'
            +f'ExecStart={restic} forget --keep-daily 7 --keep-weekly 4 --prune\n',
        'nxrs-restore@.service': common+f"ExecStartPre=/bin/sh -ec 'test \"$${{#1}}\" = 64; case $$1 in *[!0-9a-f]*) exit 1;; esac' -- %i\n"
            +f'ExecStartPre=/usr/bin/mkdir -m 0700 {ROOT}/restore/%i\n'
            +f'ExecStart={restic} restore %i:{ROOT}/media --verify --target {ROOT}/restore/%i\n',
        'nxrs-recording.sysusers': 'g nxrs-recordings -\nu nxrs-media - "nxrs media" /nonexistent /usr/sbin/nologin\nu nxrs-backup - "nxrs backup" /nonexistent /usr/sbin/nologin\n',
        'nxrs-recording.sysctl.conf': 'kernel.core_pattern=|/bin/false\n',
        'nxrs-recording-volume': Path(__file__).with_name('recording-volume.sh').read_text(),
    }
