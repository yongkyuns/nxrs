# nxrs camera MJPEG ingest

This separate Rust process consumes the **existing** `service/stream` one-viewer
`GET /stream` feed, validates its bounded frame envelope, and sends JPEGs to a
supervised FFmpeg H.264 encoder publishing to local MediaMTX. The control API
never carries pixels. No camera HAL, acquisition owner or frame ABI is changed.

The device contract is HTTP/1.1 multipart boundary `nxrs-frame`, image/jpeg,
Content-Length, X-Sequence and X-Capture-Ms, with at most 153,600 bytes per frame.
Timestamps are device-owner observations, **not UTC or sensor exposure times**.
The parser intentionally targets this contract, not arbitrary Internet cameras.

## Run explicitly or use a supervised service

Build in the independent `gateway/` workspace:

```sh
cargo build --locked -p nxrs-camera-ingest
cargo test --locked -p nxrs-camera-ingest
```

Create an absolute-path private regular 0600 config in a trusted parent directory.
Replace these example addresses/formats with the already-running camera and
matching gateway publisher enrollment; do not probe/flash a device to guess them.

```json
{
  "camera": "192.168.1.50:8080",
  "allow_insecure_camera_http": true,
  "rtsp": "127.0.0.1:8554",
  "stream_path": "reference",
  "publisher_user": "publisher_reference",
  "publisher_token_file": "/etc/nxrs-monitor/publisher.token",
  "ffmpeg": "/usr/bin/ffmpeg",
  "width": 640,
  "height": 480,
  "frame_timeout_ms": 3000
}
```

```sh
./target/debug/nxrs-camera-ingest /absolute/private/ingest.json
```

The optional [strict ingest service](../deploy/ingest-services.md) renders the
unit and config alongside HTTPS/WireGuard. It stages private runtime credentials
and places ingest plus FFmpeg in one bounded unprivileged cgroup. The generator
never installs/starts services or initializes existing enrollment state. Opt-in
requires an explicit camera endpoint and insecure-HTTP acknowledgement.

Source addresses are literal RFC1918/loopback IPv4 with fixed `/stream`. There is
no discovery, DNS, redirect or proxy fallback. RTSP publication is loopback-only.
Use a **publisher** token, never a viewer or management token. Stop the synthetic
publisher before using its path: overridePublisher remains disabled. Do not run
a browser against the single-reader camera while the gateway owns it. Multiple
phone viewers subscribe to MediaMTX, not independent camera connections.

Keep source classification honest: synthetic fixtures stay synthetic; physical
is an operator configuration, not proof of freshness. Availability remains unknown
until measured source health is integrated with the API.

## Authenticated camera transport

For a compatible camera TLS server, set `allow_insecure_camera_http: false` and
add `camera_tls` to the existing config:

```json
{
  "site_id": "site",
  "camera_id": "cam",
  "gateway_id": "gateway",
  "issuer_sha256": "REPLACE_WITH_APPROVED_CA_DER_SHA256",
  "enrollment_generation": 1,
  "ca_certificate_file": "/etc/nxrs-monitor/camera-ca.pem",
  "camera_certificate_file": "/etc/nxrs-monitor/camera-peer.pem",
  "client_certificate_file": "/etc/nxrs-monitor/camera-client.pem",
  "client_key_file": "/etc/nxrs-monitor/camera-client.key",
  "crl_file": "/etc/nxrs-monitor/camera-crl.pem"
}
```

Use approved P-256/SHA-256 credentials from a CA with critical CA/pathlen=0 and
certificate/CRL-signing usages. Leaves require critical CA=false and only digital
signature usage. The camera has serverAuth, its IPv4 SAN and exactly
`urn:nxrs:camera:SITE:CAMERA`; the client has clientAuth and exactly
`urn:nxrs:ingest:SITE:GATEWAY:CAMERA`. The client key is unencrypted PKCS#8 under
the private-file boundary. Provision a current signed CRL with the approved
generation, at most 512 revoked entries and at most a one-day validity window.
Keep every input owner-private in trusted directories; never install the CA key
on the gateway. The gateway requires trustworthy UTC before credential loading.

```sh
nxrs-camera-ingest /absolute/private/ingest.json --require-camera-tls --check-config
nxrs-camera-ingest /absolute/private/ingest.json --require-camera-tls
```

The first command checks configuration and credentials without connecting or
starting an encoder. Rustls verifies chains, IP, validity, revocation and handshake
signatures; the gateway also requires the exact approved camera leaf and scoped
identities. TLS 1.2/1.3 have no HTTP, system-trust or anonymous-client fallback.
Connect/handshake/initial HTTP share one five-second deadline. Credential/CRL
expiry and observed clock rollback stop the source and reap its encoder, including
during reconnect backoff. Rotation requires a controlled restart with the new
approved files and generation; externally protected generation state is still
required to resist configuration rollback.

The [service generator](../deploy/ingest-services.md) can stage these gateway
credentials and enforce TLS at startup. Device TLS, provisioning/custody and
physical resource qualification remain in #33/#35. Publisher-secret isolation
remains in #37. This does not make the full system production-qualified.

## Bounds, freshness and lifecycle

One source socket and one encoder exist per attempt. The adapter holds a fixed
153,600-byte payload, 8 KiB input buffer, bounded HTTP/part headers (4/1 KiB), and
bounded progress lines. A deadline covers the entire next frame and pipe write;
partial progress cannot reset it. Relative arrival backlog over one second,
repeated/decreasing sequence numbers or backwards timestamps retire the attempt.
A new connection can establish a new sequence/clock epoch. Relative lag is not
initial frame age, clock synchronization or glass-to-glass latency.

JPEG size/markers, baseline SOF and configured even dimensions are checked.
These envelope checks are not full entropy decoding or a decoder security audit;
FFmpeg decodes images, and integration receivers separately verify actual output.

Encoder progress has a ten-second startup limit and a bounded later no-progress
limit. Stalls, malformed frames, blocked pipes, encoder failure or publication
failure close the source and kill/reap the encoder before reconnect. There is no
image looping/constant-rate duplication. Retry backoff is one to eight seconds,
reset after 30 healthy seconds. Reaping failure stops ingest rather than creating
more children. SIGINT/SIGTERM close/reap cleanly; standalone SIGKILL cannot promise
child cleanup. Use the opt-in service's whole-cgroup policy for that case.

The actual device server deliberately permits TCP half-close: a viewer may close
its request direction yet continue receiving. During a source stall it can retain
that response until its write/idle deadline. A sequential early ingest reconnect
can therefore be rejected busy. This is not a second admitted viewer. Recovery
checks must preserve admitted/disconnected accounting, finite deadlines and one
encoder rather than assuming all reconnect attempts are immediately admitted.

## Actual-source interoperability test

`tests/build_camera_source.py` compiles the actual camera contract and
`service/stream` sources into a native fixture supplying synthetic JPEGs. There
is no duplicate Python HTTP writer and no fallback when actual sources fail to
compile. Use a PR merge/current-main tree, or `--source-root` for a separate main
worktree when running an older feature-only checkout.

From the repository root:

```sh
python3 gateway/tests/build_camera_source.py --output gateway/target/camera-reference
python3 -m unittest discover -s gateway/tests -p 'test_ingest*.py' -v
python3 -m unittest discover -s gateway/tests -p test_rust_camera.py -v
# In a venv containing gateway/tests/media-requirements.txt:
python3 gateway/tests/ingest_reference.py \
  --gateway gateway/target/debug/nxrs-gateway-control \
  --ingest gateway/target/debug/nxrs-camera-ingest \
  --mediamtx /path/to/verified/mediamtx \
  --camera-source gateway/target/camera-reference/nxrs_camera_source
```

The test retains two advancing H.264 viewers sharing one admitted source, stall
teardown, new-epoch recovery, and child cleanup. The fixture checks
accepted-minus-disconnected equals active (at most one) on every server poll.
The hosted-only `--systemd` variant tests generated-unit privileges/limits,
SIGKILL cgroup cleanup, video cessation, explicit recovery and dependency loss.
Its controller/network dependency units are stubs; actual strict controller
failure is independently tested elsewhere. This is not the combined production
service group over WireGuard, intended-host cold boot, physical sensor/NuttX app,
iPhone/cellular behavior, indefinite soak or latency qualification. Refer to exact
commit/run evidence in PR #21, not the existence of test assertions.

## Trust and remaining work

The optional camera HTTP profile is unauthenticated/unencrypted and allowed only on an explicitly
trusted isolated LAN. The phone VPN does not protect this distinct link. Secure
device ingestion requires qualified device-side TLS. Config/token files reject
public modes, final symlinks/hardlinks and oversized input; parent directories
must remain trusted. No shell is invoked and child logs never expose secrets.
FFmpeg still has the loopback publisher credential in argv, visible to trusted
host process observers. A credential-safe encoder interface is not delivered.

The runner's FFmpeg version is recorded, not a pinned commercial encoder image.
Keep codec/license/security review, source-health observations, long-duration
backpressure/recovery, physical-device handoff and the combined strict/VPN/phone
qualification separate. Preserve the camera effort's acquisition ownership and
private footage boundaries.
