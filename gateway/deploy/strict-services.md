# Strict HTTPS/WireGuard service deployment

This opt-in profile combines the [VPN network/proxy policy](README.md) with
[durable viewer revocation](revocation.md), explicit state provisioning and a
fail-stop service group. It does not configure a machine automatically and is
not a completed camera product. See #21 for commit-specific executed evidence;
implemented tests are not themselves a claim that runtime qualification passed.

## Render a new private bundle

From the repository root, with `wg` installed:

```sh
python3 gateway/deploy/strict_vpn.py render /private/path/NEW-bundle \
  --host camera.example.test --gateway 10.77.0.1 --peer 10.77.0.2 \
  --endpoint YOUR_REACHABLE_HOME_ENDPOINT --peer-public-key YOUR_PHONE_PUBLIC_KEY
```

Replace the illustrative hostname with a name covered by your certificate and
resolving to the gateway tunnel address. Keep the phone private key on the phone.
Follow the base guide's endpoint, route-collision, certificate/DNS, firewall and
trusted-host preflight. This remains a single IPv4 home endpoint, one enrolled
phone and a labeled synthetic source. It does not provision CGNAT hubs, a hosted
backend or physical camera ingestion.

Rendering reuses `vpn.py`. A complete bundle has `deployment.json` with
`mode: strict`, written last. Do not install partial output. Existing destinations
are refused. Rendering changes no services, routes, firewalls, trust or live
identity state.

The bundle adds a separate private management credential, loopback MediaMTX API,
strict units, `nxrs-monitor.target` and `nxrs-control.sysusers`. Management is not
added to nginx routes or VPN port allowances. The original `vpn.py` remains a
legacy development profile and does not silently gain durable revocation.

## Install only after reviewing the bundle

Use a dedicated trusted Linux/systemd host. The reference targets Ubuntu 24.04;
other service managers/filesystems require separate qualification. Follow the
base guide's verified binary, certificate, WireGuard and firewall installation,
but use this section instead of independently enabling the three legacy services.
Stop the old group before replacing its configuration. `BUNDLE` below is the
absolute new directory you rendered.

Create the stable unprivileged control account explicitly:

```sh
sudo install -D -m 0644 "$BUNDLE/nxrs-control.sysusers" /etc/sysusers.d/nxrs-control.conf
sudo systemd-sysusers /etc/sysusers.d/nxrs-control.conf
```

Do not reuse an existing account with unrelated privileges or repair ownership
recursively without inspection. Media and proxy retain distinct dynamic users.
The controller uses a stable account for its mutable state, avoiding DynamicUser
state symlinks, which the snapshot store rejects.

Install the initializer and its reused modules as root-owned readable tools:

```sh
sudo install -D -m 0644 gateway/deploy/strict_vpn.py /usr/local/lib/nxrs-monitor/deploy/strict_vpn.py
sudo install -D -m 0644 gateway/deploy/vpn.py /usr/local/lib/nxrs-monitor/deploy/vpn.py
sudo install -D -m 0644 gateway/tools/init_reference.py /usr/local/lib/nxrs-monitor/tools/init_reference.py
```

### First enrollment only

The following is **not an upgrade, boot or recovery step**. Use it only for a
new deployment with no previous deny list. The chained command must refuse an
existing directory, including a dangling symlink; stop on any error:

```sh
sudo test ! -e /var/lib/nxrs-control && \
  sudo test ! -L /var/lib/nxrs-control && \
  sudo install -d -m 0700 -o nxrs-control -g nxrs-control /var/lib/nxrs-control && \
  sudo -u nxrs-control /usr/bin/python3 /usr/local/lib/nxrs-monitor/deploy/strict_vpn.py \
    init-state /var/lib/nxrs-control
```

Initialization requires an empty private non-symlink directory owned by the
executing account. It exclusively creates and synchronizes the snapshot. Failed
initialization retains a marker; existing snapshots, lock files, backups and
partial state are neither erased nor repaired. A trusted administrator could
still deliberately delete files: this is not root-tampering or rollback protection.

No service invokes this initializer. `StateDirectory=nxrs-control` creates the
directory and grants writable storage, but does not seed a snapshot. Missing or
corrupt state therefore refuses startup. Preserve this directory across upgrades,
configuration changes and restarts. Protect backups against reads and rollback;
never restore an older deny list as routine lost-phone recovery.

### Install credentials and start the group

Install reviewed `config.json`, `security.json`, `mediamtx.json`, `nginx.conf` and
`firewall.nft` under `/etc/nxrs-monitor/`, root-owned mode0600. Install TLS and
WireGuard files as in the base guide. Keep the mutable deny list outside the
immutable credential mounts; never overwrite it from a freshly rendered bundle.

`LoadCredential` passes immutable configuration to unprivileged services. systemd
can expose loaded credentials using a named-user ACL whose mode mask resembles
group-readable permissions. The gateway intentionally still rejects ordinary
non-private security files. A control `ExecStartPre` copies **only** its loaded
management credential to `/run/nxrs-control/security.json` mode0600 inside a0700
`RuntimeDirectory`. The copy is made as the service user, with no privilege or
permission-check bypass, and disappears with the service. It does not touch the
persistent deny list. Credential rotation requires controlled restart.

All nginx module temporary directories are explicitly under its writable runtime
directory. Compiled FastCGI/uWSGI/SCGI modules can initialize directories even
without corresponding routes; no such routes are exposed here. Filesystem
protection is not relaxed to accommodate distribution-specific default paths.

Install units and activate only the group:

```sh
for unit in nxrs-control.service nxrs-media.service nxrs-proxy.service nxrs-firewall.service nxrs-monitor-stop.service nxrs-monitor.target; do
  sudo install -m 0644 "$BUNDLE/$unit" "/etc/systemd/system/$unit"
done
sudo systemctl daemon-reload
sudo systemctl enable --now nxrs-monitor.target
```

When upgrading the legacy profile, first disable its independent control/media/
proxy boot links. Only the target should own their activation. It pulls in the
existing WireGuard/firewall dependencies. Verify snapshot, certificates and
credentials before enabling startup. Process health is not media readiness,
source freshness or phone reachability; inspect private revocation status too.

## Fail-stop and recovery

Media/proxy bind to control using `BindsTo` plus `After`. All three are `PartOf`
the target, which binds to all three. An application process failure stops the
group; `Restart=no` intentionally requires explicit recovery rather than starting
unmanaged media or repeatedly retrying invalid identity state. This first profile
trades availability for a clear security boundary, not a hard-real-time promise.

Normal viewer revocation remains selective: other viewers and the publisher can
continue. A whole controller/group failure is different: all viewing ends and
publishers must reconnect after recovery. After investigating the cause:

```sh
sudo systemctl stop nxrs-control.service nxrs-media.service nxrs-proxy.service nxrs-monitor.target
sudo systemctl stop nxrs-monitor-stop.service
sudo systemctl reset-failed nxrs-control.service nxrs-media.service nxrs-proxy.service nxrs-monitor-stop.service nxrs-monitor.target
sudo systemctl start nxrs-monitor.target
```

Never reinitialize state during recovery. Revoked identities must stay denied;
healthy viewers use fresh grants. This dependency policy handles process death,
including SIGKILL, but not a live-but-hung controller or uninterruptible disk I/O.
A management outage still leaves selective termination pending until recovery;
new viewing fails closed. A watchdog is separately scoped work.

## Qualification boundaries

The non-privileged tests cover strict rendering, explicit state initialization,
credential staging and runtime-path policy. `monitoring-systemd.yml` adds a real
service-manager fixture that refuses non-GitHub-hosted machines. It preserves the
generated hardening/credential units but substitutes unique names, paths and
loopback ports. A no-op dependency replaces WireGuard/firewall units; the separate
VPN workflow covers the actual kernel tunnel, not this combined strict setup.

The fixture checks real process UIDs, zero effective capabilities, no-new-privileges,
private credentials and read-only filesystem boundaries. It then attempts real
synthetic H.264 playback through nginx, selective revocation, abrupt controller
failure, group shutdown, explicit recovery and missing/corrupt-state rejection.
Every owned unit, account, child and private fixture is cleaned up. Diagnostics
are allow-listed statuses/modes/booleans, never raw logs, keys or video.

Consult #21 for passed versus pending/failed gates at the specific commit. Even
a successful service start/restart is not whole-machine cold boot, power-cut
certification, intended-host installation, a sustained soak, or iPhone/cellular/
physical-camera evidence. Existing encoder and dependency qualification limits
still apply.

## Primary references

- [systemd unit dependencies](https://www.freedesktop.org/software/systemd/man/251/systemd.unit.html)
- [systemd state and credential directories](https://www.freedesktop.org/software/systemd/man/251/systemd.exec.html)

Configuration and documentation do not replace runtime testing on the intended
host. The profile uses established directives, not newly added systemd features.

### Private-file boundary

Control security settings, denial snapshots and their lock use the same
`nxrs-gateway-private-file` policy as ingest configuration and publisher tokens.
The service opens without following a final symlink, then checks the opened
descriptor for a regular file, no group/other permission bits, one hard link and
its caller's size limit. FIFO opens are nonblocking so a missing peer cannot
hold startup before the type check. Locks use the same descriptor checks and
are created as mode 0600 without truncating an existing file. Missing, corrupt,
public or special-file state is an error; it never resets denial history.

All parent directories and the local filesystem remain trusted. Operators must
stop the owning service group before changing the state path or lock, and must
never unlink an active lock. Descriptor checks do not provide a filesystem I/O
deadline, inspect ACLs, stop a writer with the service UID, or prevent restoration
of an older valid snapshot. Preserve the deployment's dedicated UIDs, directory
ownership, credential isolation and backup/rollback procedures.

The strict HTTPS service uses [proxy artifact admission](../proxy/README.md). Missing, changed or expired approved artifacts refuse the strict group; recovery requires an explicit start after the reviewed bundle is restored.

An actual failure of any strict application service triggers `nxrs-monitor-stop.service`.
This bounded root oneshot has no capabilities and uses only the local manager's
Unix socket to queue a stop of the three named application units and their target.
It uses `--no-block` so it does not wait inside its own manager transaction. The
handler is outside the target's `PartOf` graph and has no install/enable link.
The hosted refusal cases require a fresh successful handler invocation and all
main processes exited before explicit fixture cleanup or artifact restoration.
A failed target activation alone does not establish sibling cleanup; target
state transitions and `StopWhenUnneeded` alone were insufficient in qualification.

The failure handler remains active/exited after queuing cleanup. This latches
repeated failure notifications until explicit recovery; it runs once per failure
episode. Join the application units (including pre-start/control processes,
queued jobs and cgroups) before clearing the latch. A zero MainPID alone is not
a lifetime barrier. The hosted fixtures exercise that same ordered recovery
protocol and require a fresh completed latch invocation for every injected failure.

Management health has a five-second monotonic budget from controller startup and
from each completed reconciliation. HTTP errors, malformed/incomplete session
lists, failed kicks and stalled responses do not renew it. During a transient
failure, new playback remains denied; if management cannot recover within the
budget, controller failure stops the strict media/proxy group and existing
viewing. Investigate and use the explicit recovery sequence above. A recovered
API permits the same controller to continue only before its deadline. This
requires strict group supervision; a separately launched legacy media server
is not stopped by controller exit. The loopback media API is an independent
health boundary from active video, not evidence that video has already ceased.

Application credentials have a single finite activation window in `config.json`:
`credential_window.not_before` and `credential_window.expires_at` are positive
UTC Unix seconds, at most 90 days apart. Generated reference cohorts last 30 days.
Strict startup requires a current window. Viewer/history/grant, publisher,
recorder and management admissions expire together; existing strict media is
closed by controller supervision. A monotonic deadline prevents elapsed time
from being recovered by moving wall time backward. Observed wall rollback,
clock failure or expiry poisons that activation until controlled recovery.
A transient management recovery cannot renew credential validity.

Plan rotation before the deadline. Stop and join the complete group, retain
the existing denial state, create fresh independent tokens for every application
role, replace their configured hashes/private files and the management token,
set a new bounded window, then use explicit recovery. Do not rerun initial-state
provisioning or reuse denied viewer IDs to restore access. Re-enrollment of a
denied identity remains denied after token rotation; ownership transfer needs
an explicitly new authorized identity and its own operational procedure. The
window is a cohort activation policy, not an online issuer or automatic renewal.

For a recorder key replacement inside an existing activation window, use the
same complete stop/join barrier before changing either the recorder's configured
hash or private token file. Preserve its recorder username, camera scope,
viewer/publisher/management credentials, denial snapshot and activation window.
Install the matching hash/file pair while the group is stopped, then explicitly
restart the group. Verify the old key is rejected, the replacement worker decodes
fresh frames, denied viewers remain denied and kept footage remains accessible.
This does not renew the activation window. If expiry requires a new window,
follow the complete cohort rotation procedure above. Failed replacement stays
stopped; do not recover by restoring an older denial snapshot or obsolete key.

Backend publisher and recorder denial uses configuration's globally unique
enrollment IDs. From the authorized local administration boundary, POST an empty
JSON object to `/internal/publishers/{username}/revoke` or
`/internal/recorders/{username}/revoke`. These routes remain private. HTTP 202
means the denial was durably written; the matching `/revocation` route reports
persistence and management health. New RTSP admission is denied immediately.
Complete reconciliation kicks the matching active sessions while unrelated
identities continue. Both RTSP and WebRTC reconciliation must succeed within the
existing five-second health budget. HTTP 202 alone does not prove media stopped:
verify native progress cessation and session retirement, or stop/join the group
if management cannot establish them. After a controller restart, publishers
must reauthenticate. Wait for orphan sessions to retire and new publication to
become ready before creating fresh playback sessions.

For a lost camera or compromised recorder, deny its enrollment ID before
recovering service. A new token, camera assignment or role cannot revive a
denied ID. Join the complete group before changing enrollment, retain the denial
snapshot, and enroll an explicitly new authorized ID with a fresh independent key
and the intended camera scope. Use controlled recovery and verify old admission
refuses and new native decoding progresses. Retire device TLS identities and
applicable WireGuard peers separately; RTSP denial does not revoke those other
authorities.

The controller reads version1 viewer snapshots and version2 enrollment-identity
snapshots. A successful denial writes version2 and preserves every prior ID.
Older controllers refuse version2. Rollback requires compatible software, never
an older denial snapshot or an empty seed. Missing/corrupt state refuses startup;
failed persistence retains in-memory denial and blocks new media. If history
capacity prevents recording a new denial, pending identities remain bounded by
the configured cohort. Retrying an older denial cannot restore healthy admission
while any requested denial remains unsaved. Use controlled maintenance; never
empty the snapshot to recover capacity.
Protected anti-rollback storage, complete ownership transfer/factory reset, VPN lifecycle,
device certificate retirement and physical key erasure remain separate gates.

Gateway UTC, root-owned configuration and the local OS remain trusted. This
does not establish protected time/generation across restart, key erasure,
individual per-role schedules, WireGuard peer expiry, or hardware qualification.
Legacy mode without a window remains an explicit development profile.
