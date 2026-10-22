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
  --source-kind physical --allow-insecure-camera-http
```

Replace the example source with the address/port and format of the already
running, qualified camera. The example is not discovery or a claim that this
hardware address exists. The source supports one reader: close direct browser
viewing before starting ingest. MediaMTX, not the device, fans out to viewers.

HTTP acknowledgement is mandatory. This link is unauthenticated and unencrypted;
use only an isolated trusted LAN. Phone-to-gateway WireGuard does not protect it.
`physical` is the operator's source classification, not proof of fresh video or
physical-camera qualification. The default classification remains `synthetic`.

The new private files are `ingest.json` and `nxrs-ingest.service`; the generated
monitoring target adds a Wants dependency. No service is installed or started by
the generator. Existing destinations are refused, and no persistent denials are
initialized/reset by this optional feature.

## Install with the strict profile

Build the separate host workspace from `gateway/`: `cargo build --release --locked
-p nxrs-camera-ingest --features publisher-sdk --bins`. The publisher build requires
the installed FFmpeg SDK development headers/libraries and libclang. Install
`nxrs-camera-ingest` and `nxrs-rtsp-publisher` under `/usr/local/libexec/`, owned by
root and not writable by service users. Install a reviewed FFmpeg build with its `libx264` encoder at
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

Ingest, encoder and publisher share one unprivileged systemd cgroup, including the inherited
256 MiB memory limit and 128-task ceiling. No capabilities, Netlink, persistent
state access or management credential are added. Immutable loaded credentials
are staged into 0600 files in its private runtime directory; this preserves the
Rust ordinary-file permission checks. A private pipe supplies the publisher token;
it is absent from all three process argument lists and environments. The image
encoder receives no credential through its process interface. All three still
share a UID and runtime directory: same-UID compromise/root access remain outside
this isolation boundary. Review [publisher qualification](../ingest/publisher.md).

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
rendered ingest sandbox, checks all three process privileges/cgroup limits, kills only
the ingest main process, and checks encoder disappearance, source disconnection,
video cessation and explicit recovery. Its control/network dependency units are
disposable stubs; actual controller lifetime/revocation is tested separately by
`systemd_reference.py`. Neither component test is the combined full deployment
over WireGuard, physical camera-to-phone, intended-host cold boot or a soak test.

Record exact CI runs with their commit before treating these gates as passed.

Ingest configuration and publisher-token reads use the descriptor-validated
private-file policy described in [strict services](strict-services.md#private-file-boundary).
Final symlinks, multiple hard links, public permissions and non-regular files are
rejected. Opening a FIFO does not wait for a writer. The deployment still needs
trusted parent directories and a local filesystem; this is not an I/O deadline
or stronger isolation from a compromised same-UID encoder.

## Authenticated candidate

Use the [TLS staging procedure](../ingest/tls.md) to stage an approved bundle and
gateway-local client key while preserving existing publisher and denial state.
Its generated unit requires `--require-camera-tls` and stages the five private
TLS files through the same credential/runtime boundary. It does not enable or
qualify TLS on the device, alter live services, or provide separate-UID key
isolation. Review target and deployment gates before installation.
