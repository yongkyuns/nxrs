# Durable viewer revocation and active-session enforcement

This is an opt-in single-host gateway mode: durable viewer denials and selective
MediaMTX WebRTC termination, not account creation, pairing, ownership transfer,
a cloud identity provider or a commercial release. Client/camera contracts do not
change, and WireGuard keys are not application identities.

For generated services, credentials, persistent-state provisioning and fail-stop
supervision, use [strict deployment](strict-services.md). The base `vpn.py`
generator remains the legacy profile. The sections below describe the security
mode independently of any service manager.

## Enable explicitly

Without additional options, `nxrs-gateway-control CONFIG.json` keeps the earlier
in-memory, admission-only behavior. To enable enforcement:

```sh
./target/debug/nxrs-gateway-control CONFIG.json --security /absolute/path/security.json
```

The security file must be a private regular file, not a symlink or hardlink:

```json
{
  "revocations_file": "/absolute/private/state/revocations.json",
  "media_api": "127.0.0.1:9997",
  "media_api_token": "REPLACE_WITH_A_NEW_RANDOM_64_CHARACTER_LOWERCASE_HEX_TOKEN"
}
```

The placeholder is deliberately invalid. Generate an independent cryptographic
token, never a viewer/publisher credential. The API address is a literal loopback
socket, not a URL, hostname or Internet endpoint. The client cannot follow
redirects or inherit proxy environment settings.

For disposable development only, create a new private directory without printing
credentials or modifying services/networking:

```sh
python3 - <<'PY'
import json, os, secrets
from pathlib import Path
root = Path('/tmp/nxrs-revocation-development')
root.mkdir(mode=0o700, exist_ok=False)
def write(name, value):
    with open(root / name, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        json.dump(value, f)
        f.write('\n')
write('revocations.json', {'version': 1, 'revoked': []})
write('security.json', {
    'revocations_file': str(root / 'revocations.json'),
    'media_api': '127.0.0.1:9997',
    'media_api_token': secrets.token_hex(32),
})
PY
```

Do not use `/tmp` for deployed persistent identity. Provision a private,
service-owned local filesystem directory supporting advisory locking and
file/directory synchronization. Seed an empty snapshot exactly once; never repair
missing/corrupt state by silently resetting it. Preserve denials across upgrades,
config edits and restarts. Removed/re-added viewer IDs remain denied. There is no
automatic un-revoke operation. Protect backups from unauthorized reads and rollback.
The strict service guide supplies a separate first-enrollment initializer that
refuses nonempty, unsafe or partially initialized directories.

The matching **dedicated** MediaMTX instance enables authenticated loopback control:

```yaml
api: true
apiAddress: 127.0.0.1:9997
authMethod: http
authHTTPAddress: http://127.0.0.1:18081/internal/mediamtx/auth
authHTTPExclude: []
```

Retain other profile restrictions. Never expose9997 through nginx, VPN forwarding
or public firewall rules. The callback authorizes the separate management token;
never put it into viewer enrollment/grants. MediaMTX API permission is coarse:
this token is privileged, even though nxrs sends only bounded list/kick requests.
Local processes/admins remain trusted. Use a dedicated media instance because
controller restart removes unowned old WebRTC readers before admitting new viewing.
Mutable identity state cannot live in an immutable `LoadCredential` mount.

## Revocation semantics

Only the private loopback listener exposes these administrator operations:

```sh
curl --fail-with-body -X POST -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:18081/internal/viewers/VIEWER_ID/revoke
curl --fail-with-body \
  http://127.0.0.1:18081/internal/viewers/VIEWER_ID/revocation
```

In strict mode,202 means the denial was durably saved and termination accepted,
not that every remote stream has already ended. New grants/admission for that
viewer fail, including previously issued grants. Session IDs from authenticated
callbacks establish viewer ownership, allowing selective termination without
stopping other viewers or RTSP publishers. Callers must inspect status too:

```json
{
  "revoked": true,
  "persistence_healthy": true,
  "media_control_ready": true,
  "tracked_sessions": 0
}
```

This is not an atomic global snapshot or unconditional proof of remote absence.
Zero tracked sessions with `media_control_ready:false` is not success. Negotiation
races may need another pass. Real tests inspect MediaMTX and continuously decoded
frames independently. Repeated revocation synchronizes the current denial set
rather than assuming a previously failed write succeeded.

Storage failure returns503, retains the in-process denial and blocks further
viewing until successful persistence retry. Resolve the disk failure without
resetting state. Only one persistence worker is admitted; a concurrent request
gets429 even when the first HTTP caller has timed out/canceled. Synchronous disk
I/O can still delay progress/shutdown; an HTTP timeout does not interrupt a hung
filesystem operation.

## Management, supervision and bounds

The private API is polled approximately once per second, with a two-second
exchange limit and four-second pass budget. New viewing requires fresh control
observations. Invalid/oversized/paginated/unexpected management responses and
redirects mark control unavailable. `/healthz` is process health, not readiness.

With management unreachable, new viewing fails closed and saved revocations stay
pending. Existing media cannot be forcibly stopped through an unreachable API;
termination retries after recovery. A standalone controller process has no
inherent media kill switch. The optional strict service group binds media/proxy
to controller lifetime and requires explicit recovery after a process failure.
Its hosted runtime qualification is separately reported; neither dependency
binding nor this reconciler supplies hard-real-time or live-hang watchdog guarantees.

A controller-only restart retires old unowned WebRTC readers and requires fresh
grants, without stopping RTSP publication. Whole strict-group fail-stop also
stops MediaMTX, so publishers must reconnect. Unknown established readers are
removed; fresh negotiations get time to authenticate. Snapshot/admission races
cannot erase newly owned sessions or bypass the final ownership/path recheck.

Reservations are capped at four sessions per viewer and256 overall, including
negotiations. Observed closed sessions release slots on the next absent snapshot;
never-observed admissions have a30-second reservation bound. MediaMTX's path cap
is additional. These are one-controller limits, not distributed fleet quotas.

The deny snapshot is bounded to4096 IDs and1MiB, with private permissions,
exclusive writer locking, temporary-file synchronization, atomic replacement and
directory synchronization. Reopen/error tests are not power-cut hardware evidence
or protection against root/same-user tampering. Private settings/state/lock opens
refuse final symlinks and do not wait for FIFO peers. Checks use the opened
descriptor before/after reading or locking; ancestor directories remain trusted,
and ordinary filesystem I/O can still wait. Backup policy, storage monitoring
and authenticated multi-user administration remain productization work.

Grant expiry is an admission deadline, not a maximum viewing duration; established
session ownership remains revocable after the original grant expires. Source
availability remains unknown until capture/media observations are implemented.

## Test categories

`monitoring-revocation.yml` builds/lints/tests the locked Rust workspace and runs
the real MediaMTX/FFmpeg/aiortc reference: two active viewers, selective termination,
management outage/recovery, continued publishing, durable restart denial, orphan
cleanup, healthy reconnection and corrupt-state refusal. Control/admin test
traffic is loopback HTTP; WHEP uses verified ephemeral TLS. The outage relay is
only a fixture. No global trust, user footage or raw secrets are published.

`monitoring-systemd.yml` separately exercises generated strict units and their
permissions/failure behavior. The original VPN workflow tests actual kernel
WireGuard. These separate fixtures are not a combined strict-mode-over-VPN,
physical iPhone/cellular/home-router, machine-cold-boot, sustained-load,
filesystem power-cut or actual-camera qualification. See #21 for executed results
at each commit, not merely the existence of test code.
