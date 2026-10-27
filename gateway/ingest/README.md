# nxrs camera MJPEG ingest (trusted-LAN development profile)

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
cargo build --locked -p nxrs-camera-ingest --bin nxrs-camera-ingest
# Requires the host FFmpeg SDK headers/libraries and libclang:
cargo build --locked -p nxrs-camera-ingest --features publisher-sdk --bin nxrs-rtsp-publisher
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
  "publisher": "/usr/local/libexec/nxrs-rtsp-publisher",
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

## Bounds, freshness and lifecycle

One source socket, one encoder and one separate publisher exist per attempt. The adapter holds a fixed
153,600-byte payload, 8 KiB input buffer, bounded HTTP/part headers (4/1 KiB), and
bounded progress lines. Encoder and publisher progress each require their first output within ten
seconds. The encoded-byte relay uses that same bounded window for its first
transfer, allowing the encoder to probe initial input. After the first transfer,
`frame_timeout_ms` applies to every complete encoded transfer as well as each
incoming frame; first-output grace cannot extend a later stall. Authentication,
source reads, credential delivery and process cleanup retain their own bounds.
A deadline covers the entire next frame and pipe write;
partial progress cannot reset it. Relative arrival backlog over one second,
repeated/decreasing sequence numbers or backwards timestamps retire the attempt.
A new connection can establish a new sequence/clock epoch. Relative lag is not
initial frame age, clock synchronization or glass-to-glass latency.

JPEG size/markers, baseline SOF and configured even dimensions are checked.
These envelope checks are not full entropy decoding or a decoder security audit;
FFmpeg decodes images, and integration receivers separately verify actual output.

Encoder progress has a ten-second startup limit and a bounded later no-progress
limit. Stalls, malformed frames, blocked pipes, encoder failure or publication
failure close the source and kill/reap both children before reconnect. There is no
image looping/constant-rate duplication. Retry backoff is one to eight seconds,
reset after 30 healthy seconds. Reaping failure stops ingest rather than creating
more children. SIGINT/SIGTERM close/reap cleanly; standalone SIGKILL cannot promise
child cleanup. Use the opt-in service's whole-cgroup policy for that case.

The enrolled TLS certificate/CRL lease remains active during reconnect backoff.
Expiry or a failed clock check terminates ingest with `Authentication` even while
the camera is offline; the retry timer cannot postpone policy failure. Applying
new enrollment still requires a supervised restart after prior resources retire.
The host regression `gateway/tests/ingest_retry_lease.py` runs the actual Rust
executable with a valid initial policy and a short signed CRL, reaches the maximum
backoff against an offline loopback endpoint, and requires expiry without starting
an encoder or publisher. It establishes host lifecycle behavior, not a TLS
handshake or physical clock/entropy qualification.

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

Camera HTTP is unauthenticated/unencrypted and is allowed only on an explicitly
trusted isolated LAN. The phone VPN does not protect this distinct link. Secure
device ingestion remains a production requirement. Config/token files reject
public modes, final symlinks/hardlinks and oversized input; parent directories
must remain trusted. No shell is invoked and child logs never expose secrets.
The encoder receives JPEGs and emits H264 in MPEG-TS; a separate single-use Rust
publisher remuxes that stream through the FFmpeg SDK to the same authenticated
loopback RTSP path. Only the publisher's stdin receives the 64-byte token
preamble. Neither child's argv/environment contains it. See [publisher boundary](publisher.md).
The service still shares a UID: an encoder compromise can read same-UID runtime
credentials or inspect processes. Separate identities/key storage remain #37 gates.

The runner's FFmpeg version is recorded, not a pinned commercial encoder image.
Keep codec/license/security review, source-health observations, long-duration
backpressure/recovery, physical-device handoff and the combined strict/VPN/phone
qualification separate. Preserve the camera effort's acquisition ownership and
private footage boundaries.

## Authenticated ingest candidate

[Mutual TLS ingest](tls.md) adds an explicit required-TLS host path using approved
per-camera enrollment, pinned trust, CRLs and finite credential leases. The
existing HTTP example above is still a development configuration. Board-side
TLS/entropy/key storage and physical qualification remain open; the test TLS
front end is not a production camera provider.
