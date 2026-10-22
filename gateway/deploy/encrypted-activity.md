# Encrypted gateway recording volume

Status: review implementation for #36. This protects the activity tree against
powered-off storage theft within the trusted-gateway-operator threat model.
It does not qualify a production deployment, encrypt backups, add firmware
storage encryption or prove iPhone hardware data protection.

The Linux reference uses a directly mounted LUKS2 mapping with ext4 at
`/srv/nxrs`, and stores **all** activity state at `/srv/nxrs/activity`: source
segments, encoder inputs/outputs, thumbnails, staging, metadata, health/index
and retained clips. No plaintext recording directory is a fallback. Do not put
swap, crash dumps, exports or backup staging on an unprotected filesystem.
A running privileged host or the authorized service account can read footage.
LUKS/XTS provides offline confidentiality, not authenticated sector integrity;
malicious disk modification and filesystem integrity are separate limits.

## Runtime enforcement

Both worker/controller configurations declare:

```json
"protection": {
  "layout_version": 2,
  "mount": "/srv/nxrs",
  "luks_uuid": "<the existing LUKS2 volume UUID>"
}
```

This object is inside the activity storage settings, alongside its `directory`.
The volume must be an actual root mount of ext4, with `rw,nosuid,nodev,noexec`,
root-owned and not group/world writable. Rust reads kernel mountinfo and the
matching sysfs dm UUID, verifies the exact LUKS2 identity/device, rejects
symlinks/other nested filesystems, and permits only bind mounts of the same
protected filesystem. LVM, network filesystems, LUKS1 and layered block layouts
are intentionally unsupported by this profile. A trusted kernel/administrator
and local filesystem are assumptions; the dm UUID is kernel configuration
identity, not remote attestation or a claim that a cipher/KDF was independently
validated. Operator provisioning must establish those parameters.

The store verifies protection before directory creation and before each locked
operation. The worker checks before a new source/finalizer. Missing protection
in a strict launch, mount loss or a wrong UUID fails closed. Linux is the only
implemented provider; other hosts reject a protected configuration. Existing
unprotected configurations remain development-only. Services set
`--require-encrypted-storage` so dropping the object cannot silently select a
plaintext configuration. The worker's `--check-storage` mode runs real store
startup/inventory validation without starting a decoder.

Use an **empty, root-owned mode-000 mountpoint underneath the mount**. This
prevents an unprivileged service from writing a fallback directory after normal
unmount. Never pre-create an activity directory in that underlying filesystem.
The mounted filesystem root is root-owned mode 0711; its `activity` and
`maintenance` directories are owned by `nxrs-control` mode 0700. Layout version 2
is required by the maintenance-aware binaries; old binaries reject this field,
and new binaries reject older/missing versions. Upgrade the binaries/configs
together with the whole group stopped. Mount/device bindings stop the strict
service group on removal; the worker still reaps its source/finalizer children.
The entire `/srv/nxrs` mount is the service's writable exception so systemd's
read-only root hardening does not remount that root read-only in its namespace.
Ordinary filesystem reads/writes have no interruptible I/O deadline.

## Prepare a reviewable overlay

First prepare the existing activity bundle against the intended enrollment.
Then run (on configuration files only):

```sh
python3 gateway/deploy/encrypted_activity.py /private/existing-activity-bundle \
  /private/new-encrypted-overlay --luks-uuid ACTUAL-LOWERCASE-UUID
```

The new overlay preserves viewer/publisher/recorder identities and scopes,
updates both storage configurations, and renders minimal unit drop-ins for the existing strict service
profile plus the activity/mount units, with a volume dependency, private credential staging and
strict storage flags. It copies no private key/token, generates no replacement
identity, resets no denial state, formats/mounts no volume and starts no service.
Existing base units/target, secure-ingest unit, enrollment, TLS/VPN/management credentials and persistent denial state
must be retained. Install the reviewed controller config as
`/etc/nxrs-monitor/config.json`, worker config as `activity.json`, and the
**existing** recorder token as `recorder.token`. Strict `security.json`, its
protected runtime staging and `/var/lib/nxrs-control` remain required.
Install the `.service.d`/`.target.d` fragments as drop-ins; preserve the original
base units and existing ingest target membership. The controller command override
retains the reviewed strict security argument; compare it with the installed
profile before installation so later profile-specific options are not lost.
The overlay is parallel to secure-ingest work; composing all service profiles
still needs integrated qualification before release.

Only the operator provisions a dedicated volume and installs units/crypttab.
Use a maintained distribution cryptsetup with LUKS2, AES-XTS with a 512-bit XTS
key (two AES-256 keys), and Argon2id with parameters calibrated for the intended
host (including adequate memory/time). Inspect the actual LUKS metadata and
record UUID/cipher/KDF/tool versions without key material. Do not reuse the
fixture's intentionally cheap KDF settings for production. No tool here accepts
an existing disk to format or silently enables storage encryption.

The rendered crypttab fragment specifies an explicit, normally absent volatile
`/run/nxrs-unlock/manual-only.key` path, a one-second key-file wait, one timed
manual passphrase attempt with echo disabled, and `noauto`. Starting/enabling
the monitoring target still pulls in the mount/unlock units; `noauto` alone
does not prevent that dependency. The manual reference requires operator input
and does not provide unattended startup.
The explicit path avoids systemd implicit persistent named-key search when
crypttab uses `none`. No nxrs tool creates that key file. Keep it absent for the
manual reference, ensure `/run` is volatile and swap is protected, and review
existing token/automatic-unlock enrollment and password caching. These custody
prerequisites are operator gates; a mount/UUID check cannot prove them. Keep a strong
independent unlock secret and recovery/header material in separately controlled
offline custody, outside the recording disk and backups. Losing them loses the
footage; do not respond by selecting a plain directory. A powered-off gateway
with a plaintext auto-unlock key beside its disk does not meet this boundary.
Unattended unlock/TPM/network escrow needs its own reviewed custody design.

Before starting the strict target, unlock/mount through the reviewed systemd
units and verify the actual device/UUID, mount flags, permissions and the strict
worker storage check. Mount/cryptsetup failure must leave the target stopped.
A service/device failure leaves the group stopped for controlled recovery;
there is no identity/denial reseeding. Stop the whole group and confirm children
are gone before volume maintenance, key-slot changes or a future backup/restore.
For key rotation, add/test a new LUKS key slot and escrow recovery first, then
remove the old slot through a controlled operator procedure. Changing a
passphrase rewraps the volume key; it does not erase old headers, disk snapshots
or backups. Destruction/deletion claims must include all copies and flash wear.

## Qualification and remaining work

`encrypted_activity_reference.py` refuses a personal host before kernel/disk
operations and accepts no device path. On a disposable GitHub-hosted Ubuntu
24.04 runner it creates one owned sparse loop image, formats LUKS2/ext4,
executes the real recording/auth/decoding/retention/restart/source-loss pipeline
as an unprivileged UID, rejects missing/wrong UUID/key/flags and nested plaintext,
checks no fallback after unmount/lock, rotates/tests a replacement LUKS slot
and rejects its removed old passphrase, then unlocks/reopens persistent state.
A unique test canary is absent from the raw owned ciphertext. Commands/logs,
credentials and footage are captured privately and removed, not uploaded.
Cleanup inspects only exact owned kernel resources, including allocation after
a command timeout, and never forcibly/lazily unmounts or deletes an image whose
cleanup is unproven. This is a host fixture, not physical disk/endurance or
manufacturing evidence. The fixture does not execute the production systemd
service group; its integrated mount-loss behavior remains a release gate.

The independently keyed authenticated [backup/restore increment](encrypted-backup.md)
uses the version-2 maintenance lease and staged recovery gate. Its actual CLI
qualification, custody and integrated deployment evidence must be tracked
separately; volume-only success does not prove backup recovery. Device-side
recordings, real iPhone lock/reboot/export behavior, physical unlock/power-loss
recovery, full strict-service composition and release review remain open.

References: [cryptsetup LUKS2](https://gitlab.com/cryptsetup/LUKS2-docs),
[crypttab key acquisition](https://www.freedesktop.org/software/systemd/man/latest/crypttab.html),
[systemd mount dependencies](https://www.freedesktop.org/software/systemd/man/latest/systemd.mount.html),
and [security tracker](https://github.com/yongkyuns/nxrs/issues/32).
