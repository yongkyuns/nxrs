# Monitoring gateway (development reference)

Tracks #20 / draft PR #21. The independent Rust control service is now implemented;
this is **not yet a deployable home-camera product**. It changes no firmware/HAL
code, touches no device, and depends on no MCU crate or root Cargo workspace.

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
hashes, not plaintext. These tokens are not human passwords. Do not print, commit,
copy into issue comments, or reuse the test credentials for production.

The control listener is `127.0.0.1:18080`; private MediaMTX authorization and local
administrative revocation use `127.0.0.1:18081`. Startup refuses non-loopback
listeners, duplicate identities/paths/credentials, cross-site scope, insecure or
untrusted media endpoints, and invalid resource bounds. Both listener addresses
are configurable but must remain loopback in this profile.

Only the public control routes should be reachable through a trusted HTTPS
reverse proxy. **Do not proxy the internal listener or trust forwarding headers
as authentication.** This single-host profile trusts processes on the same host;
it is not suitable for shared/untrusted hosts. Production proxy, socket/header
limits, persistent identity and deployment packaging are still required.

## Implemented API and authorization

The [OpenAPI contract](../docs/monitoring-api.json) is unchanged for the iPhone
client: `GET /v1/cameras`, `POST /v1/cameras/{id}/playback-grants` with `{}`, and
minimal `/healthz`. Catalogs are viewer/site/camera scoped, and hidden cameras
return 404. `availability` remains `unknown`: configuration is not evidence that
a camera or publisher is online. Synthetic source labeling is explicit.

Grants contain random opaque media credentials; only hashes and bounded metadata
are retained. A grant permits WebRTC read for exactly one camera's configured
stream path, for at most 300 seconds. Expiration is bounded by both wall time and
monotonic elapsed time. The store has a configured total cap and a 16-outstanding-
grant per-viewer cap; full stores reject with 429 and reclaim expired entries.
A service restart invalidates all outstanding grants.

Publisher credentials permit only RTSP publication to their own stream path.
Viewer/account tokens cannot publish, and publishers cannot list cameras or read.
MediaMTX must use `authMethod: http`, the internal `/internal/mediamtx/auth`
callback, and **`authHTTPExclude: []`**. API, metrics, pprof and other actions are
denied. Disable unused MediaMTX protocols and administrative endpoints as in the
reference test; do not retain anonymous default permissions.

Control responses use `no-store`. Request bodies are limited to 16 KiB, admitted
handlers to 64 per listener, handler/body time to five seconds, configuration to
64 KiB, and catalogs to 64 cameras. Errors do not echo credentials or request
bodies. These are application bounds, not a claim that open/slow TCP connections
are bounded: the deployment proxy must enforce that separately.

The private `POST /internal/viewers/{viewer_id}/revoke` accepts `{}` and blocks
future API/media admission, including existing unexpired grants. **It does not
terminate an already-established WebRTC session.** Revocations are in-memory:
remove the credential from configuration and restart both control and MediaMTX
for the present manual hard-revocation procedure. Durable revocation and prompt
server-enforced active-session termination remain production blockers. Do not
mistake five-minute token expiry for five-minute maximum viewing time.

## Real synthetic-media integration

The dedicated `monitoring-gateway.yml` workflow builds/tests/lints the Rust
service, downloads MediaMTX **1.21.2** with the committed release-asset SHA-256,
and runs this reference test:

```sh
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt -r tests/media-requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/reference_integration.py \
  --gateway target/debug/nxrs-gateway-control --mediamtx /path/to/verified/mediamtx
```

The test launches real gateway/MediaMTX/FFmpeg processes, generates one-use TLS
certificates and separate random credentials, verifies HTTPS with a dedicated
trust root, publishes a synthetic H.264 source, negotiates WHEP and decodes five
advancing 320x240 frames with aiortc. Negative cases cover missing/wrong-role/
wrong-camera/expired credentials, untrusted TLS, capacity bounds, revocation of
new sessions, and authorization-service failure. WHEP DELETE and process cleanup
are exercised. No camera hardware or user footage is read.

The TLS front proxy is an in-test aiohttp fixture, **not** production gateway
software. RTSP ingest is plaintext on loopback only. FFmpeg's ephemeral publisher
credential appears in its process arguments; run this harness only on a trusted
test host. Raw process logs/secrets remain in a private temporary directory and
are removed on completion/failure, not uploaded as CI artifacts. The runner's
FFmpeg build is reported rather than represented as a pinned distributable
encoder image. Python top-level test versions are pinned; their transitive
packages are not yet a fully hashed deployment lock.

## Remaining work

Native SwiftUI/libWebRTC app; deployable verified HTTPS and WireGuard topology;
MediaMTX live availability/freshness observations; persistent enrollment and
active-session revocation; network/soak/admission tests; production encoder build
and license review; and integration with the parallel camera effort after its
format/buffer/timestamp handoff. The reference test proves only the synthetic
Linux media path, not the iPhone or a cellular/VPN path.

See [coordination and architecture](../docs/remote-monitoring.md). The gateway
contract is independent of WireGuard, preserving the future hosted deployment
boundary without adding any VPN code or cloud account logic to camera firmware.
