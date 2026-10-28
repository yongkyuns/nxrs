# Optional supervised camera ingest

This extends the [strict VPN profile](strict-services.md), not the development
profile. It consumes the existing device `GET /stream`; it does not start, flash
or reconfigure the camera. The camera effort still owns capture and its driver.

## Generate a NEW deployment bundle

Follow the strict profile's TLS, WireGuard, firewall, credential and first-state
provisioning instructions. To include ingest when rendering a new bundle:

```sh
python3 gateway/deploy/strict_vpn.py render /private/new-nxrs-bundle \
  --host YOUR_GATEWAY_TLS_NAME --endpoint YOUR_PUBLIC_WIREGUARD_HOST \
  --peer-public-key YOUR_PHONE_PUBLIC_KEY \
  --camera 192.168.1.50:8080 --width 640 --height 480 \
  --source-kind physical --camera-tls-enrollment /private/camera-enrollment.json
```

Replace the example source with the address/port and format of the already
running, qualified camera. The example is not discovery or a claim that this
hardware address exists. The source supports one reader: close direct browser
viewing before starting ingest. MediaMTX, not the device, fans out to viewers.

For a camera with compatible mutual TLS, the enrollment JSON contains only the
approved identity and trust metadata:

```json
{
  "site_id": "site",
  "camera_id": "cam_reference",
  "gateway_id": "gateway",
  "issuer_sha256": "REPLACE_WITH_APPROVED_CA_DER_SHA256",
  "enrollment_generation": 1
}
```

`cam_reference` is the current generator's single catalog camera. The renderer
does not issue credentials or contact the camera. Follow the
[ingest credential profile](../ingest/README.md#authenticated-camera-transport)
for certificate roles, CRL, trusted UTC and rotation requirements. Install the
approved `camera-ca.pem`, `camera-peer.pem`, `camera-client.pem`,
`camera-client.key` and `camera-crl.pem` under `/etc/nxrs-monitor/`, root-owned
with mode 0600. Never install the CA signing key. The unit stages these loaded
credentials into its private runtime directory and validates the complete Rust
configuration before connecting or starting FFmpeg. Missing, expired, mismatched
or revoked credentials refuse startup. Its `--require-camera-tls` option also
rejects an HTTP replacement configuration. Refresh credentials with an operator
controlled service restart; automatic enrollment/rotation is not implemented.

For development against an HTTP-only camera, replace `--camera-tls-enrollment`
and its argument with `--allow-insecure-camera-http`. These choices are mutually
exclusive and a camera requires one. HTTP is unauthenticated and unencrypted;
use only an isolated trusted LAN. Phone-to-gateway WireGuard does not protect it.
`physical` is the operator's source classification, not proof of fresh video or
physical-camera qualification. The default classification remains `synthetic`.

The new private files are `ingest.json` and `nxrs-ingest.service`; the generated
monitoring target adds a Wants dependency. No service is installed or started by
the generator. Existing destinations are refused, and no persistent denials are
initialized/reset by this optional feature.

Device-side TLS, qualified entropy/time, credential custody and physical resource
measurements remain open in #33/#35; a rendered service is not system qualification.

## Install with the strict profile

Build the separate host workspace with `cargo build --release --locked -p
nxrs-camera-ingest` from `gateway/`. Install that binary as
`/usr/local/libexec/nxrs-camera-ingest`, owned by root and not writable by service
users. Install a reviewed FFmpeg build with its `libx264` encoder at
`/usr/bin/ffmpeg`; the hosted test's Ubuntu package is not a pinned commercial
redistribution. Keep dependency/license review in the release checklist.

In addition to the strict-profile files, install `ingest.json` as
`/etc/nxrs-monitor/ingest.json` with mode 0600/root ownership, and the unit as
`/etc/systemd/system/nxrs-ingest.service`. Use the generated target that includes
`Wants=nxrs-ingest.service`. Its existing `publisher.token` must correspond to the
same control catalog's publisher digest. Never use a viewer or management token.

On an existing installation, review the generated unit/config as a migration.
Do NOT replace all existing credentials or regenerate/reset revocations merely
to add ingest. Back up the current private configuration first. Preserve stable
camera/viewer IDs and enrollment state. Full pairing/upgrade automation is not
implemented by this file renderer.

## Failure behavior

Ingest and FFmpeg share one unprivileged systemd cgroup, including the inherited
256 MiB memory limit and 128-task ceiling. No capabilities, Netlink, persistent
state access or management credential are added. Immutable loaded credentials
are staged into 0600 files in its private runtime directory; this preserves the
Rust ordinary-file permission checks. The publisher password still appears in
FFmpeg's loopback RTSP argv; the trusted local administrator/process-observer
boundary remains a production limitation, not a solved credential-isolation claim.

Source/network/encoder faults normally use the adapter's bounded reconnect loop.
If the adapter itself dies, `KillMode=control-group` and `SendSIGKILL=yes` retire
remaining children. `Restart=no` requires explicit recovery; it is not a promise
of unattended availability. With healthy control/media dependencies, recover via
`systemctl start nxrs-ingest.service`. A control/media/network service loss stops
ingest too; recover the strict group using its documented procedure.

An ingest failure does not deliberately shut down the control API: its camera
can remain selectable while playback fails/stalls. The target being active is
not evidence of a healthy ingest stream; check the ingest unit and actual decoded
frames. The catalog still does not report measured source freshness. No change
to revocation persistence or camera acquisition is made here.

## Interoperability and lifecycle test

`gateway/tests/build_camera_source.py` compiles the actual repository
`hal/camera/src/api.rs` and `service/stream/src/{lib,server}.rs` into a native test
owner. The owner supplies generated JPEGs and controls pause/resume through stdin;
it does not reimplement HTTP or multipart framing. Source SHA-256 values are
printed in CI. Provider selection, sensor drivers and the NuttX application are
not compiled by this isolated server test.

```sh
python3 gateway/tests/build_camera_source.py --output gateway/target/camera-reference
python3 -m unittest discover -s gateway/tests -p test_ingest_service.py -v
```

Use a PR merge/current-main tree containing the camera work. On an old feature
checkout, `--source-root /path/to/current-main-worktree` selects those actual
sources without changing either worktree. The integration script now requires
`--camera-source gateway/target/camera-reference/nxrs_camera_source` alongside
its existing gateway/ingest/MediaMTX arguments. There is no fallback to the old
Python imitation when real source compilation fails.

The normal test verifies real server framing, one admitted source connection for
two decoded viewers, stall teardown, new sequence epoch and graceful cleanup.
The optional `--systemd` test refuses non-GitHub-hosted machines, starts the
rendered ingest sandbox, checks both process privileges/cgroup limits, kills only
the ingest main process, and checks encoder disappearance, source disconnection,
video cessation and explicit recovery. Its control/network dependency units are
disposable stubs; actual controller lifetime/revocation is tested separately by
`systemd_reference.py`. Neither component test is the combined full deployment
over WireGuard, physical camera-to-phone, intended-host cold boot or a soak test.

Record exact CI runs with their commit before treating these gates as passed.
