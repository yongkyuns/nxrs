# Durable viewer revocation and active-session enforcement

This is an **opt-in single-host gateway mode**, separate from the default reference
profile. It adds durable viewer denials and selective MediaMTX WebRTC termination.
It does not implement account creation, pairing, ownership transfer, a cloud
identity provider or a commercial release. The client API and camera firmware do
not change, and no WireGuard keys are used as application identities.

## Enable explicitly

The default `nxrs-gateway-control CONFIG.json` remains compatible with the earlier
reference tests: revocation is in-memory and admission-only. To enable enforcement:

```sh
./target/debug/nxrs-gateway-control CONFIG.json --security /absolute/path/security.json
```

The security file must be a private regular file, not a symlink or hardlink. Its
contents are:

```json
{
  "revocations_file": "/absolute/private/state/revocations.json",
  "media_api": "127.0.0.1:9997",
  "media_api_token": "REPLACE_WITH_A_NEW_RANDOM_64_CHARACTER_LOWERCASE_HEX_TOKEN"
}
```

The placeholder is intentionally invalid. Generate a separate token with a
cryptographically secure generator; never reuse a viewer or publisher credential.
The address is a literal loopback socket, not a URL, hostname or Internet address.
The HTTP client cannot follow redirects or use proxy environment settings.

For a disposable development enrollment, the following creates a **new** private
directory and refuses to overwrite an existing one. It prints no credentials and
does not start services or modify networking:

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

**Do not use `/tmp` for deployed persistent identity.** Provision a private,
service-owned persistent directory on a local filesystem that supports advisory
locking and file/directory synchronization. Seed `revocations.json` exactly once
with `{"version":1,"revoked":[]}`. Missing or corrupt state refuses startup; it
must never be replaced with an empty list as routine recovery. Preserve the state
across upgrades, account-config edits and restarts. Retain backups protected with
the same permissions and do not restore an older snapshot that re-enables a lost
phone. IDs in the deny list remain revoked even after removal/re-addition to
static enrollment. No automatic un-revoke endpoint is provided.

The matching **dedicated** MediaMTX instance must enable its loopback control API:

```yaml
api: true
apiAddress: 127.0.0.1:9997
authMethod: http
authHTTPAddress: http://127.0.0.1:18081/internal/mediamtx/auth
authHTTPExclude: []
```

Retain the profile's other restrictions. Do not expose port 9997 through nginx,
WireGuard forwarding or a public firewall rule. The management token is authorized
by the gateway callback; do not put it into camera/viewer grants. MediaMTX's API
permission is coarse: possession of this token is privileged, not limited by the
upstream API to session listing/kicking. The nxrs client code itself sends only
bounded list/kick requests. Local processes and administrators remain trusted.
Use a dedicated MediaMTX instance: after controller restart, unowned existing
WebRTC readers are terminated before new viewing is accepted.

The existing `vpn.py` generator and its systemd units **do not enable this mode**.
A deployment must explicitly add the security credential and persistent writable
state directory to the control service. The state is mutable and must not live in
an immutable `LoadCredential` mount. Provision appropriate service ownership,
`StateDirectory`/filesystem access and supervision, then qualify boot/restart and
sandbox enforcement. No runtime systemd integration is claimed by this slice.

## Revocation semantics

Only the private loopback listener exposes these administrator operations:

```sh
curl --fail-with-body -X POST -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:18081/internal/viewers/VIEWER_ID/revoke
curl --fail-with-body \
  http://127.0.0.1:18081/internal/viewers/VIEWER_ID/revocation
```

In strict mode, **202 means the denial was durably saved and termination is
accepted**, not that every remote stream is already gone. New grants and admission
for that viewer fail, including grants issued before revocation. The reconciler
uses the authenticated callback's session ID to disconnect that viewer while
preserving other viewers and RTSP publishers. Callers must also inspect status:

```json
{
  "revoked": true,
  "persistence_healthy": true,
  "media_control_ready": true,
  "tracked_sessions": 0
}
```

These are observations, not an atomic global snapshot or an unconditional proof
of remote absence. In particular, do not report success from zero tracked sessions
when `media_control_ready` is false. Negotiating sessions may need another pass.
The real media tests independently inspect MediaMTX and continuously decoded
frames to verify that viewing stops. Repeating revocation is safe; it rewrites
and synchronizes the current denial set rather than assuming an earlier failed
write succeeded.

A storage failure returns 503, keeps the in-process denial, and blocks further
viewer admission until a successful persistence retry. It is not a successful
durable revoke. Resolve the storage failure without resetting the deny list and
retry. One persistence worker is admitted at a time; a second concurrent request
gets 429 even if the first HTTP caller has already timed out or canceled. A hung
filesystem can still delay policy progress and shutdown; application HTTP
timeouts do not make synchronous filesystem I/O interruptible.

## Supervision, limits and failure behavior

The private API is polled approximately once per second, with a two-second
exchange limit and four-second pass budget. Fresh management observation is
required for new viewer admission. Session/response bounds, invalid JSON,
unexpected pagination, redirects, or management errors mark the controller
unavailable. Health `/healthz` remains process health, not media readiness.

If the management API becomes unreachable, new viewing fails closed and saved
revocations remain pending. **Existing media cannot be forcibly stopped through
an unreachable API.** Termination retries after recovery. Likewise, killing the
controller is not itself a guaranteed media kill switch. A production service
manager must provide and test the desired fail-stop behavior; this code does not
promise a hard revocation-time bound through host/network failures.

A controller restart intentionally terminates pre-existing unowned WebRTC readers
and requires non-revoked viewers to reconnect with fresh grants. RTSP publishing
is not stopped. In normal operation unknown established readers are removed;
new, not-yet-established negotiations get time to complete their authentication.
A snapshot taken before admission cannot erase newly registered ownership or
cause a newly authorized reader to be kicked at the final ownership recheck.

Strict mode reserves at most four sessions per viewer and 256 overall. This
includes negotiations, not just displayed video. Once observed, a closed session
releases its slot on the next absent snapshot; never-observed admissions have a
30-second bounded reservation. Keep MediaMTX's path reader cap as an additional
limit. This is one-process admission accounting, not a distributed fleet quota.

The denial snapshot is capped at 4,096 IDs and one MiB. Files use private modes,
exclusive writer locking, temporary-file synchronization, atomic replacement and
directory synchronization. The tests verify reopen/error behavior, not actual
power-cut hardware durability or protection from root/same-user tampering.
Backups, storage monitoring and an authenticated multi-user administration
surface are still productization work.

Grant expiry remains an admission deadline, **not** a forced viewing duration.
Active ownership survives the original grant's expiry and remains revocable.
The static camera/source catalog still reports availability as unknown until
real capture/media observations are implemented.

## Executed-test categories

`monitoring-revocation.yml` builds the locked Rust workspace, runs persistence,
ownership, race/cleanup and legacy-compatibility regressions, and executes
`gateway/tests/revocation_reference.py` against the pinned MediaMTX binary.
The synthetic Linux test covers two active viewers, selective termination,
management outage/recovery, continued publishing, durable denial on restart,
orphan cleanup, healthy reconnection, corrupt-state startup rejection and cleanup.
The management outage proxy is only a fixture. Control/admin test traffic is
loopback HTTP; WHEP/video uses scoped TLS/WebRTC encryption.

This does not replace physical iPhone, WireGuard/cellular, home-router, sustained
load, filesystem power-cut, systemd or actual-camera qualification. See #21 for
commit-specific CI evidence, not merely the existence of this test code.
