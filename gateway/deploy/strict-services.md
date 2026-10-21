# Strict HTTPS/WireGuard service deployment

This opt-in profile combines the existing [VPN network/proxy policy](README.md)
with [durable viewer revocation](revocation.md). It adds explicit persistent-state
provisioning and a fail-stop service group. It does not configure the administrator's
machine automatically and is not a completed camera product.

## Render a new private bundle

From the repository root, with `wg` installed:

```sh
python3 gateway/deploy/strict_vpn.py render /private/path/NEW-bundle \
  --host camera.example.test --gateway 10.77.0.1 --peer 10.77.0.2 \
  --endpoint YOUR_REACHABLE_HOME_ENDPOINT --peer-public-key YOUR_PHONE_PUBLIC_KEY
```

The example hostname must be replaced by a name covered by your actual certificate
and resolving to the gateway tunnel address. Keep the phone's private key on the
phone. Follow the original profile's endpoint, route-collision, certificate/DNS,
firewall and trusted-host preflight. This remains IPv4, a single home endpoint,
one enrolled phone and a labeled synthetic source; CGNAT hub/hosted topology and
real camera ingestion are not added here.

Rendering reuses `vpn.py` rather than forking its nginx/firewall policy. A successful
bundle has `deployment.json` with `mode: strict`, written last. A partial bundle
must not be installed. Existing destinations are refused. No services, routes,
firewall rules, certificates or live identity state are changed by rendering.

The additions are a private `security.json` with an independent random management
token, a loopback-only MediaMTX control API, strict service definitions, the
`nxrs-monitor.target` group and a `nxrs-control.sysusers` account definition.
The security credential goes only to the control service via `LoadCredential`.
The management API is never added to nginx or VPN port allowances. The original
non-strict generator remains available for the earlier development fixtures;
using it does not silently enable these stronger semantics.

## Install only after reviewing the bundle

Use a dedicated, trusted Linux host with systemd. The sample reference runs on
Ubuntu 24.04; do not assume another service manager or filesystem has been tested.
Complete the original guide's binary/checksum, certificate, WireGuard and firewall
installation, but use this section instead of independently enabling the three
legacy application services. Stop any old monitoring group before replacing its
configuration. No phone, gateway or home router is modified by CI.

Install the static service account explicitly:

```sh
sudo install -D -m 0644 "$BUNDLE/nxrs-control.sysusers" /etc/sysusers.d/nxrs-control.conf
sudo systemd-sysusers /etc/sysusers.d/nxrs-control.conf
```

`BUNDLE` is the absolute new directory you rendered. The controller uses a stable
unprivileged `nxrs-control` account, not root. Media/proxy retain separate dynamic
users. A static controller account is intentional: its private mutable state is
not a recycled DynamicUser directory/symlink or a read-only credential mount.
This store rejects symlink state directories. Do not repurpose an existing user
with unrelated privileges or blindly repair ownership recursively.

Install the initializer and its reused modules as root-owned, readable tools:

```sh
sudo install -D -m 0644 gateway/deploy/strict_vpn.py /usr/local/lib/nxrs-monitor/deploy/strict_vpn.py
sudo install -D -m 0644 gateway/deploy/vpn.py /usr/local/lib/nxrs-monitor/deploy/vpn.py
sudo install -D -m 0644 gateway/tools/init_reference.py /usr/local/lib/nxrs-monitor/tools/init_reference.py
```

### First enrollment only: seed persistent state

The following is **not an upgrade, boot or recovery step**. It is permitted only
for a genuinely new deployment with no previous revocations. Verify the directory
does not already exist, including as a dangling symlink, before creating it:

```sh
sudo test ! -e /var/lib/nxrs-control && sudo test ! -L /var/lib/nxrs-control && \
  sudo install -d -m 0700 -o nxrs-control -g nxrs-control /var/lib/nxrs-control
sudo -u nxrs-control /usr/bin/python3 /usr/local/lib/nxrs-monitor/deploy/strict_vpn.py \
  init-state /var/lib/nxrs-control
```

Stop on any error; do not execute the second line after a failed new-directory
check. Initialization requires an empty, non-symlink, private directory owned by
the executing account. It exclusively creates the snapshot, synchronizes it and
the directory, and refuses repeat invocation. A failure marker is deliberately
retained after interrupted initialization. Existing snapshots, lock files, backups,
corruption or partial state are not erased or repaired. Local trusted
administrators can still deliberately delete files; this is not rollback or
root-tampering protection.

No service calls this initializer. `StateDirectory=nxrs-control` supplies writable
storage and private permissions at startup, but does not create a fresh snapshot.
A missing or corrupt snapshot therefore fails closed. Preserve this directory
across configuration edits, upgrades and service restarts. Protect backups against
unauthorized reads and rollback; never restore an older deny list as routine
recovery for a lost phone.

### Install configuration and start the group

Install the reviewed `config.json`, `security.json`, `mediamtx.json`, `nginx.conf`
and `firewall.nft` into `/etc/nxrs-monitor/` with mode 0600 and root ownership, as
in the original guide. Install the certificate/key and WireGuard configuration
there as directed by that guide. The security file references
`/var/lib/nxrs-control/revocations.json`; the mutable snapshot must not be placed
inside `LoadCredential` or overwritten by copying a newly rendered bundle.

Install the unit files:

```sh
for unit in nxrs-control.service nxrs-media.service nxrs-proxy.service nxrs-firewall.service nxrs-monitor.target; do
  sudo install -m 0644 "$BUNDLE/$unit" "/etc/systemd/system/$unit"
done
sudo systemctl daemon-reload
sudo systemctl enable --now nxrs-monitor.target
```

On an upgrade from the legacy profile, first disable its independent
`nxrs-control.service`, `nxrs-media.service` and `nxrs-proxy.service` boot links;
only the strict target should own their activation. The target pulls in the
existing WireGuard/firewall dependencies. Confirm a valid persisted snapshot and
credential/certificate installation before enabling it.

Use `systemctl status nxrs-monitor.target nxrs-control.service nxrs-media.service
nxrs-proxy.service` and the private revocation status endpoint to distinguish
process activation from management readiness. Health alone does not prove a
camera feed, fresh frames or remote iPhone reachability.

## Failure and recovery policy

Media and proxy bind to the controller with `BindsTo` plus `After`; all three are
part of the strict target. The target binds to all three. A process failure stops
the group rather than allowing unmanaged media to continue or repeatedly starting
through invalid identity state. **Automatic restarts are intentionally disabled**
for this first strict profile. This trades availability for an explicit, auditable
recovery boundary. It is not a hard-real-time shutdown guarantee.

Normal viewer revocation remains selective: it does not stop other viewers or
the RTSP publisher. A **whole controller/group failure** is different: media and
proxy are stopped, all viewing ends, and publishers must reconnect when the group
is restored. An administrator recovers only after inspecting the failure:

```sh
sudo systemctl reset-failed nxrs-control.service nxrs-media.service nxrs-proxy.service nxrs-monitor.target
sudo systemctl restart nxrs-monitor.target
```

Never reinitialize identity state during this operation. The stored deny list
must still reject revoked viewers; healthy viewers use fresh grants. Certificate
or credential replacement likewise requires a controlled group restart, not an
assumption that immutable loaded credentials update themselves.

This service dependency handles process termination, including abrupt controller
death. It does **not** detect a live-but-hung process, make a blocked filesystem
interruptible, or force selective termination through an unreachable MediaMTX
management API. Management-outage policy remains as documented in revocation.md:
new viewing fails closed, durable revocation stays pending, and reconciliation
retries after recovery. A watchdog/liveness policy is separate work.

## Qualification and boundaries

`monitoring-systemd.yml` runs eight generator/state regressions and
`gateway/tests/systemd_reference.py` on a disposable GitHub-hosted Linux runner.
The latter refuses non-hosted machines. It starts the generated service units
with their real hardening and `LoadCredential` settings, using unique names,
temporary credential/binary paths, a private persistent-state directory and
random loopback ports. A no-op network fixture substitutes for the WireGuard and
firewall service dependencies; it does not pretend to be a real tunnel. The
separate VPN workflow covers the real kernel tunnel and network policy.

The service test probes actual non-root UIDs, zero effective capabilities,
no-new-privileges, read-only filesystem boundaries and private credential access.
It receives synthetic H.264 via the actual nginx/gateway/MediaMTX processes,
revokes one of two viewers, kills the controller while another is playing,
checks group shutdown/video cessation, explicitly restarts, verifies persisted
denial and healthy playback, and rejects missing/corrupt state without reseeding.
It attempts cleanup of every owned unit, account, process and private fixture even
on failure. Private logs, secrets, session URLs and video are not uploaded.

These are implemented checks; #21 records executed results per commit. A hosted
service start/restart is not a whole-machine cold boot, power-cut filesystem
certification, home gateway installation, iPhone/cellular test, physical-camera
run, soak or release certification. The reference encoder and Python transitive
dependencies retain their previously documented qualification limits.

## Primary service-manager references

- [systemd unit dependencies](https://www.freedesktop.org/software/systemd/man/251/systemd.unit.html): ordering is separate from lifecycle binding; `BindsTo` plus `After` is used here.
- [systemd execution and state/credential directories](https://www.freedesktop.org/software/systemd/man/251/systemd.exec.html): service-owned persistent storage and credential mounts have distinct purposes.

The reference uses long-established directives rather than requiring newly added
service-manager features. Neither documentation nor config text replaces runtime
qualification on the intended deployment host.
