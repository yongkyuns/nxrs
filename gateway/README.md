# Monitoring gateway (development reference)

Tracks #20 / draft PR #21. This is **not yet a complete home-camera product**.
The independent host workspace has no MCU dependencies and does not alter camera
acquisition, HALs, drivers or the root Cargo workspace.

Optional [automatic activity recording](activity/README.md) adds a private motion
clip store, camera-scoped history/media endpoints and an iPhone Activity timeline.
It subscribes to the existing loopback media stream rather than acquiring the
physical camera. Existing deployments remain supported without enabling it.

For an integrated opt-in Linux deployment with durable denial and fail-stop
supervision, use [strict HTTPS/WireGuard services](deploy/strict-services.md).
It reuses the [base network/proxy profile](deploy/README.md) and explicitly enables
[revocation mode](deploy/revocation.md). The original `vpn.py` generator remains
the legacy development profile; the new `strict_vpn.py` renders strict units and
provides a separate first-enrollment-only state initializer. Neither generator
installs services or changes a personal machine/router automatically.

## Build and initialize

```sh
cd gateway
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked
python3 tools/init_reference.py /tmp/nxrs-monitor-reference --media-origin https://localhost:8889
./target/debug/nxrs-gateway-control /tmp/nxrs-monitor-reference/config.json
```

Rust 1.90.0 and the separate Cargo.lock pin the host build. Initialization refuses
an existing destination, creates a 0700 directory and 0600 files, and generates
separate 256-bit viewer/publisher tokens. Configuration stores their SHA-256
hashes, not plaintext. Tokens must be generated, not human passwords. Never
print, commit or reuse test credentials for production.

The control listener defaults to `127.0.0.1:18080`; private MediaMTX authorization
and administrative revocation use `127.0.0.1:18081`. Startup refuses non-loopback
listeners, ambiguous identities/paths/credentials, cross-site scope, insecure or
untrusted media endpoints and invalid bounds. Proxy only public control routes
through trusted HTTPS; never proxy the internal listener. Local host processes
are trusted in this profile, which is not intended for untrusted shared hosts.

## Application and media authorization

The [OpenAPI contract](../docs/monitoring-api.json) is unchanged: authenticated
camera list and per-camera playback grants, plus minimal process `/healthz`.
Catalogs are viewer/site/camera scoped; hidden cameras return 404. Availability
remains `unknown` until real source observations exist. Synthetic sources stay
explicitly labeled, regardless of whether the decoder is receiving frames.

Grants carry random opaque media tokens; only token hashes and bounded metadata
are retained. A grant permits WebRTC read for exactly one camera path, with a
maximum 300-second admission lifetime checked against wall and monotonic time.
The store has a configured total cap and 16 outstanding grants per viewer. Full
stores return 429 and expired entries are reclaimed. A control restart invalidates
all grants. Grant expiry is not a maximum established viewing-session duration.

Publisher credentials authorize only RTSP publication to their exact path.
Account, publisher and playback credentials are not interchangeable. MediaMTX
must use HTTP authorization with `authHTTPExclude: []`, not anonymous default
permissions. Administrative actions remain denied in the legacy profile; strict
revocation mode adds a **separate privileged management token** for a private
loopback API. That token must never appear in viewer enrollment or media grants.

Responses use `no-store`; bodies are limited to 16 KiB, handlers to 64 per
listener, request/body time to five seconds, configuration to 64 KiB and catalogs
to 64 cameras. Proxy/socket/header limits remain a deployment responsibility.
Errors do not echo credentials or request bodies.

## Two explicit revocation modes

Without `--security`, the original private revoke operation returns 204 and
invalidates admission/grants **in memory only**. It does not stop existing video.
Remove the credential from static enrollment and restart both control and
MediaMTX for a manual hard revocation in that legacy profile.

With `--security SECURITY.json`, denials are saved using a private, exclusively
owned snapshot with synchronized atomic replacement. Missing/corrupt state fails
startup rather than becoming an empty deny list. The gateway tracks authenticated
WebRTC session ownership and uses the private MediaMTX API to terminate a revoked
viewer's streams without stopping unrelated viewers or the RTSP publisher.
The private revoke returns **202 after persistence**, while a separate private
status endpoint reports pending sessions and management readiness. A storage
failure is 503, not successful durable revocation. Persistence-worker admission
remains bounded even after HTTP cancellation. See [exact semantics](deploy/revocation.md).

Strict mode requires a healthy management path for new viewing, reserves at most
four sessions per viewer and 256 overall, and retires orphan viewing on controller
restart. A management outage leaves termination pending until recovery; it cannot
kill media through an unreachable API. The strict service group additionally
stops media/proxy when the controller process dies and requires explicit recovery.
It does not detect a live-but-hung controller. Static account enrollment, state
backup/rollback protection and end-user account administration remain work.
VPN peer removal is a separate network operation, not application identity.

## Reference and regression tests

The dedicated workflows separate different kinds of evidence:

| Workflow | What it exercises |
| --- | --- |
| `monitoring-gateway.yml` | Locked Rust build/lint, role-separated authorization, HTTPS/WHEP and synthetic H.264 decoding with real MediaMTX/FFmpeg processes |
| `monitoring-revocation.yml` | Strict persistence/ownership/cancellation regressions; two live viewers, selective termination, outage/recovery, restart denial, orphan cleanup and corrupt-state refusal |
| `monitoring-vpn.yml` | Generated nginx/firewall policy, kernel WireGuard in isolated Linux namespaces, actual decoded video and access isolation |
| `monitoring-systemd.yml` | Strict deployment generation and one-time state; real service-manager permissions, active video, selective revocation, controller-crash fail-stop and persistent recovery |
| `monitoring-ios.yml` | Native Swift/WebRTC application playback/build qualification, with its separately scoped test transport |

MediaMTX 1.21.2-nxrs.1 is reproducibly built with patched Go dependencies and
fresh source/binary advisory gates. Follow [the media build procedure](deploy/media-engine.md);
one recipe supplies the Linux and macOS reference runtimes.
Run the non-privileged Linux references with an already verified binary:

```sh
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt -r tests/media-requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/reference_integration.py \
  --gateway target/debug/nxrs-gateway-control --mediamtx /path/to/verified/mediamtx
.venv/bin/python tests/revocation_reference.py \
  --gateway target/debug/nxrs-gateway-control --mediamtx /path/to/verified/mediamtx
```

The original TLS proxy and the revocation outage proxy are fixtures, not deployed
proxy software. Control/admin revocation-test traffic is loopback HTTP; media
negotiation verifies an ephemeral trusted TLS certificate without changing global
trust. The VPN and service-manager harnesses use generated real nginx policy.
The service-manager test substitutes loopback/network dependencies, so it is
not an integrated strict-mode-over-VPN or whole-machine boot test. Its privileged
fixture refuses non-hosted machines. Check #21 for actual commit-specific results.

RTSP ingest is plaintext on loopback only. FFmpeg's ephemeral publisher token
appears in its arguments; run these harnesses on trusted test hosts. Raw logs,
keys and credentials are private and removed, not uploaded as artifacts. Runner
FFmpeg versions are recorded, not represented as pinned distributable encoder
images. Python transitive test packages are not yet a fully hash-locked deployment.

## Camera coordination and remaining qualification

The parallel camera work landed on main at `c81f017` and was followed by HAL error
isolation in #23 (`e5cdb1d`). Its current `app/camera-stream` / `service/stream`
implementation and `tests/nuttx-std/stream/README.md` provide the device-side MJPEG
handoff. The gateway should be its single device-side viewer and fan out after
encoding/MediaMTX, rather than introduce a second capture owner or firmware
streaming API. Those physical results belong to the camera qualification effort;
no hardware was used by this gateway slice. Plain-HTTP ingest remains trusted-LAN
only, and owner-observation timestamps are not sensor exposure timestamps.

Still required: actual iPhone UI/Keychain/local-network/background behavior and
cellular transitions; coordinated MJPEG ingest and capture freshness; intended-host
installation/cold boot and watchdog policy; long-running resource/recovery checks;
full account provisioning; dependency/encoder/SDK licensing and security review;
secure physical ingest and future hosted/CGNAT deployment. See the
[architecture and handoff](../docs/remote-monitoring.md). Stable identities and
client contracts remain independent of WireGuard.
