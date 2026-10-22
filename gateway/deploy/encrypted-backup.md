# Authenticated encrypted recording backups

Review implementation for #36, based on the encrypted-volume profile. The
reference trusts the gateway administrator/key custodian and protects offline
footage/backup theft. It does not exclude a running compromised gateway/root,
qualify physical power loss or encrypt device-side/iPhone captures by itself.

## Format, keys and custody

Use the exact reviewed restic 0.19.1 executable identified in
[backup-dependencies.json](backup-dependencies.json). The native format provides
maintained authenticated encryption, key derivation/wrapping, chunk integrity
and repository locks; nxrs does not implement an encryption format. Both archive
and executable hashes are checked during qualification, and the CLI checks the
reviewed executable hash before handing it any secret. This identifies an
artifact, not a FIPS certificate, reproducible build or complete advisory review.
See [restic's format](https://restic.readthedocs.io/en/stable/100_references.html).

The repository master key is protected by an **independent 256-bit backup
recovery secret**, represented by 64 lowercase hex characters from a qualified
host CSPRNG. Provision and escrow it in a separately controlled encrypted vault
or offline custody. Keep it separate from the LUKS unlock secret, recording
volume and backup repository; it must remain recoverable after the gateway is
lost. The CLI accepts only a ready private stdin pipe and EOF, with a one-second
input deadline; keys are absent from argv/environment/configuration/metadata.
Rust-owned key buffers zeroize; Python input copies, native memory, swap and
privileged reads are not claimed erased. Protect swap/core dumps and custody.

Only a local private repository is supported by this first profile. It must be
outside the recording volume and not an ancestor of it. Its encrypted files can
be transported/copied to separately controlled backup storage using an approved
storage procedure; offsite transport/access credentials are separate. Repository
file sizes/counts and public key-header/KDF/host metadata can remain observable.
There is no plaintext cache; native temporary files are redirected to the
protected volume. Lost/wrong keys fail, never select an unencrypted fallback.

Rotation after compromise should create a **new repository/master key**, back up
with a new independent secret, verify restore, escrow it and retire old copies
through the reviewed custody policy. Rewrapping/removing a restic password slot
does not revoke old repository-key files/backups already copied by an attacker.
Keys and flash/snapshot/backup deletion do not promise erasure of every old copy.

## Layout and coherent ownership

Protected configuration requires `layout_version: 2`. Old binaries reject this
field; new binaries reject missing/older versions. Upgrade worker, controller,
backup helper and both configurations together while the whole group is stopped.
Ordinary development stores remain explicitly unprotected; backup commands refuse
them. Provision `/srv/nxrs/activity` **and** `/srv/nxrs/maintenance` as private
mode-0700 directories owned by the recording account, inside the mounted LUKS2
volume. The mounted root remains root-owned 0711; the empty underlying unmounted
mountpoint remains mode 000. The backup repository is an existing private 0700
directory owned by the same recording account, on independent storage.

Every protected Store owns a shared lifetime lease outside the replaceable data
tree. Maintenance takes its exclusive lease. Thus every command requires the
controller, worker and descendants stopped. Backup additionally holds actual
worker/store locks while validating/freezing the complete committed tree.
Unpublished spool/staging files cause refusal rather than an incoherent snapshot;
resolve normal recovery first. Source segments/unfinished files remain covered by
volume encryption. A backup contains media/event/health state and empty locks,
**never enrollment, viewer/publisher/management keys, trusted roots or denials**.
Restoring footage cannot reseed or roll back application authorization.

Run the helper as the dedicated, unprivileged recording account with no effective,
permitted or ambient capabilities. It rejects root/mixed UID/capability contexts
and mismatched private directory owners. Run it under the reviewed whole-process
supervisor (`KillMode=control-group`, no core dumps, bounded memory/tasks,
`NoNewPrivileges`, no capabilities/namespaces, narrow writable paths). A crash or
forced stop must retire the entire group before another command. Normal cancel,
operation deadline and output overflow kill/reap the native child; unproven
retirement retains ownership and stops admission pending operator group cleanup.
The custody launcher only transports a terminal-entered key; Rust owns policy.

## Configuration and invocation

Create an account-readable private configuration (0600) retaining the existing
storage settings and protection object. For example, with actual installed paths
and UUID/hash values substituted:

```json
{
  "storage": {
    "directory": "/srv/nxrs/activity",
    "camera_ids": ["cam_one"],
    "retention_seconds": 259200,
    "max_bytes": 1073741824,
    "max_events": 1000,
    "protection": {"layout_version": 2, "mount": "/srv/nxrs", "luks_uuid": "ACTUAL-UUID"}
  },
  "repository": "/var/lib/nxrs-backups/camera",
  "repository_id": null,
  "restic": "/usr/local/libexec/nxrs-restic",
  "restic_sha256": "EXACT-REVIEWED-EXECUTABLE-SHA256",
  "timeout_seconds": 600
}
```

`repository_id: null` is accepted only by `init`, which refuses an existing
nonempty repository and reports its public full repository ID. Record that ID
in the protected configuration before other operations. The CLI verifies it
through the native authenticated repository config on every operation; no
`latest`/short snapshot selector or remote-backend string is accepted. Configuration
preparation/copying must not reset enrollment, denial state or recording identity.

From an authorized recording-account terminal under its reviewed process group:

```sh
python3 gateway/tools/activity_backup.py /private/backup.json init
python3 gateway/tools/activity_backup.py /private/backup.json backup
python3 gateway/tools/activity_backup.py /private/backup.json check
python3 gateway/tools/activity_backup.py /private/backup.json restore FULL-SNAPSHOT-ID
```

The launcher obtains the whole secret with echo disabled **before** spawning the
CLI, then sends it through a private pipe with a sanitized environment. It writes
no key file. Automation must use the native interface
`nxrs-activity-backup CONFIG OPERATION [FULL-SNAPSHOT-ID]` with an already-ready
private pipe from approved custody; don't put secrets in shell literals/history,
argv or environment. `backup` reports the full snapshot ID only after successful
native completion; an incomplete/ambiguous attempt is not a successful receipt.
Preserve trusted snapshot IDs outside the repository; old copies cannot establish
freshness against a malicious custodian. No command auto-installs, starts services,
formats disks or removes repository snapshots.

## Verified restore and interruption recovery

Restore requires a preprovisioned **empty** activity directory on the declared
protected volume. It never overwrites a live/nonempty recording tree. Keep the
existing source/backup and current authority/denial state. Authenticate all backup
packs, constrain the actual native manifest (paths/types/privacy/count/size), then
extract/verify into a private encrypted maintenance stage. Root/settings/camera
scope, exact event manifests and quotas are checked before publication. Every
file/directory is synced. A durable operational intent blocks all matching Store
clients until every staged entry is published and synced; interrupted partial
content is never an admitted store. This journal is lifecycle metadata, not a
second cryptographic format.

An ordinary pre-publication failure retires the child and discards only its own
unpublished stage. A crash or publication/durability failure can leave an intent;
startup remains blocked during an incomplete publication. Stop/verify the whole
owned group, repair the declared mount/permissions/custody prerequisite, then use
`resume FULL-SNAPSHOT-ID` with the same repository, camera scope, volume UUID and
trusted snapshot ID. It reauthenticates the source before discarding only the
unserved candidate recorded by that intent, and starts a new verified stage.
Missing/corrupt/mismatched intents refuse recovery; never clear a marker or point
the services at a plain directory merely to get startup working. Use a fresh
protected target and reviewed offline recovery if the intent cannot be trusted.

A hard-killed native operation may leave an encrypted repository lock. The
explicit `unlock-stale` command calls only native safe `unlock` under the same
maintenance/identity checks; it never uses `--remove-all` or bypasses an active
lock. Reinspect groups/host identity rather than force-unlocking active writers.
After restore, verify current authorization, kept/history state, actual decoded
playback and the normal storage check before controlled service startup. Retention
still applies to old unkept footage; restoring a deliberately chosen old snapshot
can reintroduce recordings deleted after it, not any removed access privilege.

## Evidence boundary

Host native-backend tests execute the real pinned engine: coherent source lock,
encryption/key absence, exact repository identity, wrong-key rejection, actual
manifest mutations, verified restore, corrupt encrypted-pack rejection and child
cancel/deadline retirement. On macOS these do **not** execute the Linux volume
provider or establish supported protected restore there.

The hosted Linux fixture adds the actual CLI on an owned LUKS2 loop image with
unprivileged real Store publication, H264/thumbnail restore and authenticated API
playback; process argv/environment/key-output checks; active-controller refusal;
actual write-permission failure after durable ready intent; whole-group kill
during a preparing native restore; stale-lock/explicit resume; and ciphertext
corruption. Count these only after execution at the exact PR head. No fixture
keys, raw logs or footage are uploaded. It accepts no device to format and refuses
a personal host before kernel operations. Cheap fixture KDFs/synthetic secrets
must not become deployment policy.

Physical storage/power interruption/endurance, actual production service-group
composition/unlock, custody/escrow/restore on the intended host, native/OS advisory
closure and artifact-bound release evidence remain gates. Device-side recordings,
iPhone hardware lock/export behavior and complete identity lifecycle remain in
#32/#35/#36/#37. This increment does not close those issues on documentation or an
unmerged/partially qualified branch.
