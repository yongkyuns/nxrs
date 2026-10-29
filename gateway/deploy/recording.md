# Encrypted recording and maintenance backups

This opt-in addition to [strict services](strict-services.md) uses the pinned
MediaMTX recorder, LUKS2/ext4 and restic. The gateway can read footage while the
volume is unlocked. LUKS2 AES-XTS provides confidentiality, not authenticated
sector integrity; restic authenticates its encrypted backups. A compromised root
or running media/backup process remains a plaintext/key custodian.

Render a **new** strict bundle with the existing arguments and
`--recording-volume YOUR_LUKS_UUID`. It changes no machine configuration. The UUID
must identify an operator-provisioned LUKS2 volume using `aes-xts-plain64`, a
512-bit volume key and ext4. This tool never formats, repairs or erases a disk.
The ordinary profile retains recording disabled. No recording/playback HTTP
listener or phone library API is added.

## Operator installation and unlock

Use the dedicated Ubuntu 24.04/systemd host from the strict-service guide, with
`cryptsetup-bin`, ext4 tools, util-linux and restic installed. Review the generated
units and paths before installation. Stop the old monitoring group first.

Build the recording-only MediaMTX variant with Go 1.21+ available:

```sh
python3 tools/build-recording-media.py /tmp/nxrs-recording-build
```

The builder selects Go 1.27.3, verifies pinned upstream module sums, and applies
the small [recorder queue patch](../../upstream/mediamtx/recorder-drain.patch).
Install its `nxrs-mediamtx-recording` output root-owned and executable at
`/usr/local/libexec/nxrs-mediamtx-recording`. Recording units require this separate
binary: upstream 1.21.2 discards queued samples during recorder retirement.
The patch drains only recorder queues before native fMP4 finalization; live
viewer cancellation and finite service stop/resource limits still apply.

* Install the generated strict units and `var-lib-nxrs\x2drecordings.mount` under
  `/etc/systemd/system/`. Include `nxrs-recording-volume.service`,
  `nxrs-backup-init.service`, `nxrs-backup.service` and `nxrs-restore@.service`.
* Install `nxrs-recording-volume` root-owned mode0755 at
  `/usr/local/libexec/nxrs-recording-volume`. It only checks admission.
* Add the reviewed `nxrs-recordings.crypttab` entry to `/etc/crypttab`, preserving
  other entries. Install the generated sysusers file and run `systemd-sysusers`.
  Media and backup have separate stable accounts and the `nxrs-recordings` group.
* Disable swap in the reviewed host configuration. Review and install
  `nxrs-recording.sysctl.conf`: it discards **all host process core dumps**. Admission
  refuses active swap or another core-dump handler; `LimitCORE=0` alone is not the
  privacy boundary for a piped crash handler.
* Create `/run/nxrs-keys` root-owned mode0700 on tmpfs. Load the independently
  generated volume key into `recordings.key` and the separate restic password
  into `backup.password`, root-owned mode0600. Keep recovery copies off this host
  and away from the encrypted data. Do not persist these files on an unencrypted
  root filesystem, put their contents in argv/environment, or share fleet keys.

After `systemctl daemon-reload`, start the quoted mount unit
`'var-lib-nxrs\x2drecordings.mount'`. The crypttab entry uses an explicit volatile
key path and `headless`: a missing/wrong key refuses unlock without an interactive
or plaintext fallback. Verify the mount before preparing **new** directories:

```sh
sudo /usr/local/libexec/nxrs-recording-volume YOUR_LUKS_UUID
sudo chown root:nxrs-recordings /var/lib/nxrs-recordings
sudo chmod 0750 /var/lib/nxrs-recordings
sudo install -d -m 0750 -o nxrs-media -g nxrs-recordings \
  /var/lib/nxrs-recordings/media /var/lib/nxrs-recordings/media/.tmp
sudo install -d -m 0700 -o nxrs-backup -g nxrs-recordings \
  /var/lib/nxrs-recordings/cache /var/lib/nxrs-recordings/cache/tmp \
  /var/lib/nxrs-recordings/cache/restic /var/lib/nxrs-recordings/restore
```

Preserve existing directories/UIDs on upgrades; do not recursively repair an
unreviewed store. Mount an independently provisioned, finite ext4 backup
filesystem at `/var/lib/nxrs-backup` with `nodev,nosuid,noexec,noatime`, configured
through the host's reviewed fstab/mount unit. Create its **new empty** `repository`
directory owned by `nxrs-backup`, mode0700. It stores restic ciphertext; it must
not be the recording disk or an unmounted directory on the root filesystem.

Only for first backup enrollment, run
`sudo systemctl start nxrs-backup-init.service`. Native restic refuses an existing
repository. No boot unit initializes a repository, denial snapshot or key.
Start `nxrs-monitor.target` after reviewing the existing TLS/VPN prerequisites.

## Record, back up and restore

MediaMTX writes one-minute fMP4 segments with one-second parts, an 8 MiB part
bound and 24-hour native retention. Its [native fMP4 writer](https://github.com/bluenviron/mediamtx/blob/v1.21.2/internal/recorder/format_fmp4_track.go)
commits a sample when the next timestamp supplies its duration; the final pending
sample can be omitted on source closure. This is not a lossless archive of every
captured frame. The fixture verifies the exact decoded 120-frame prefix of 121
published frames, then verifies its backup and restoration. The allocated recording filesystem supplies
the hard capacity bound; exhaustion is an error, never an alternate path. Media
temporary files stay there. Backups cover the complete media tree; restic cache,
temporary files and restore staging also stay on the encrypted volume.

Backup/restore is an explicit **maintenance window**. Starting either operation
stops media and, through the strict group's existing dependencies, playback and
ingest. Operations require a clean inactive media service before reading. There
is no timer or automatic restart. This avoids backing up an actively written
segment; it does not offer uninterrupted online backup.

```sh
sudo systemctl start nxrs-backup.service
# On success, explicitly restore live service:
sudo systemctl start nxrs-monitor.target
```

Successful backup precedes restic's seven-daily/four-weekly snapshot retention
and prune. A missing mount/password, wrong key or full destination fails instead
of initializing another repository. Keep the independent recovery password.
Select a full 64-hex snapshot ID using restic on a private operator console, with
`TMPDIR=/var/lib/nxrs-recordings/cache/tmp` and
`--cache-dir /var/lib/nxrs-recordings/cache/restic`; supply the password by file.
Run `sudo systemctl start nxrs-restore@SNAPSHOT_ID.service` to restore only the
media subtree into a new private `/var/lib/nxrs-recordings/restore/SNAPSHOT_ID`.
The unit verifies restored content. It refuses an existing target, including a
partial interrupted restore. Preserve/rename failed staging explicitly before
retrying. Independently decode clips before operator promotion; nothing copies
staging into the live media tree automatically.

Rotate LUKS slots and restic keys using their maintained tools and separately
verify recovery before removing old access. Old headers, repo keys, snapshots
and copies can retain access: logical deletion is not secure flash erasure.
Compromised volume/repository master keys require a separately planned migration,
not just a password change. Operator custody/rotation and physical disk failure
qualification remain release gates.

Cold boot requires reloading volatile keys from the operator's separately held
material. Unattended TPM/KMS unlock needs its own qualified boot/key policy; no
unencrypted local-key fallback is provided. Phone captures and generic device
recordings retain their separate protection/physical qualification gates in #36.
Gateway configuration/denial state and other host files need their separate
host-protection policy; this profile does not encrypt the complete operating system.

The hosted fixture uses owned loop devices, synthetic media, the real generated
units/vendor cryptsetup generator and native recording/restic operations. It is
not physical power-loss, long-term capacity/performance or production deployment
evidence. See [#36](https://github.com/yongkyuns/nxrs/issues/36) for executed status.
