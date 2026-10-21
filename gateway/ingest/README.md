# nxrs camera MJPEG ingest (trusted-LAN development profile)

This separate Rust process consumes the **existing** `service/stream` one-viewer
`GET /stream` feed, validates its bounded frame envelope, and sends JPEGs to a
supervised FFmpeg H.264 encoder publishing to the local MediaMTX instance. The
control API never carries image bytes. No firmware, camera HAL, acquisition owner
or device frame ABI is introduced or modified.

The device contract inspected on main is HTTP/1.1 multipart with boundary
`nxrs-frame`, `Content-Type: image/jpeg`, `Content-Length`, `X-Sequence`, and
`X-Capture-Ms`; frame capacity is 153,600 bytes. The timestamp is the device
owner's observation, **not UTC or sensor exposure time**. This initial parser is
intentionally specific to that contract, not a generic Internet camera client.

## Run explicitly

Build in the independent gateway workspace:

```sh
cd gateway
cargo build --locked -p nxrs-camera-ingest
cargo test --locked -p nxrs-camera-ingest
```

Create an absolute-path, private regular `0600` configuration file in a trusted
parent directory. Replace the sample device address/port and paths with your
actual deployment; do not probe or change the device merely to guess these.
Use the **publisher** token already provisioned by the gateway generator, not the
viewer or management token. The device camera stream must already be running.

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

The source address is a literal RFC1918/loopback IPv4 socket; the path is fixed to
`/stream`. There is no discovery, DNS, redirect, proxy or URL-credential handling
on device ingest. RTSP publication is restricted to loopback. Do not run another
browser/reader against the single-client camera simultaneously. Multiple phone
viewers subscribe to MediaMTX, never independently to the camera.

Stop the synthetic publisher before publishing to its path; `overridePublisher`
remains disabled. Update the gateway catalog's name and `source` deliberately:
synthetic fixtures remain `synthetic`; mark an actual installed device `physical`
only when that really is the connected source. Catalog availability remains
`unknown`; this adapter does not add an unimplemented health API.

## Bounded behavior and freshness

There is one camera connection and one encoder per attempt. The adapter retains
one fixed 153,600-byte payload buffer, an 8 KiB input buffer, bounded headers
(4 KiB HTTP / 1 KiB multipart) and bounded progress lines. A single deadline
covers each entire frame header, JPEG and encoder-pipe write. Slow drip traffic
and partial writes do not reset it. This bounds the adapter, not total FFmpeg or
kernel socket memory; intended-host resource/soak tests remain necessary.

Repeated/decreasing sequence numbers or decreasing device timestamps retire the
attempt. A new connection may begin a new sequence/clock epoch. Relative arrival
lag growing by more than one second also retires the source rather than draining
a growing queue. This detects added backlog relative to the best offset in that
connection, not initial frame age, clock accuracy or glass-to-glass delay.

JPEG checks enforce size, start/end markers, baseline SOF and configured even
resolution. Embedded/concatenated JPEGs and other JPEG coding modes are rejected
in this initial sensor profile. FFmpeg still performs actual image decoding;
header acceptance is not a decoded-image proof or a full decoder security audit.

Encoder output progress has a ten-second startup deadline and then a bounded
no-progress deadline. A stalled source, malformed frame, blocked pipe, dead
encoder or failed publication causes source/pipe closure and encoder kill/reap
before any reconnect. No image looping or constant-rate duplicate-frame filter
is used. Retry delay increases from one to at most eight seconds; healthy
30-second attempts reset it. Failure to reap a child stops the adapter rather
than creating another process. SIGINT/SIGTERM also close the source and reap the
encoder. A hard SIGKILL requires a service-manager cgroup to kill children; this
slice does **not** integrate an ingest unit into the strict deployment generator.

## Trust and remaining qualification

`allow_insecure_camera_http` is an explicit acknowledgement, not encryption.
The current camera feed is unauthenticated plain HTTP on a trusted isolated LAN.
The phone's WireGuard tunnel does not secure that different link. Authenticated,
encrypted device ingestion is a separate production requirement. Use the existing
trusted single-host gateway model; no untrusted co-tenants.

Configuration/token files reject public modes, symlinks and hardlinks and are
size-bounded. Parent directories must be trusted. No shell is invoked. Encoder
stderr is suppressed and diagnostics expose fixed categories only, never camera
images, source response bodies or credentials. The loopback RTSP publisher token
is necessarily present in FFmpeg's argv in this version; administrators and
process observers on the host are trusted. Do not claim process-list secret
isolation. A credential-safe encoder integration remains production work.

The dedicated `monitoring-ingest.yml` runs Rust protocol/policy/deadline tests and
`tests/ingest_reference.py`. The reference uses a **synthetic Python device-wire
fixture**, actual FFmpeg, the Rust control service, pinned MediaMTX, scoped TLS,
and two continuously drained H.264 receivers. It tests one admitted device
connection, source-stall publication teardown, reconnect/new epoch and graceful
child cleanup. It does not execute the firmware server or qualify a real camera,
iPhone UI, cellular/WireGuard, systemd ingest service, indefinite soak or latency.
The runner encoder version is recorded, not claimed as a pinned production image.
See PR #21 for commit-specific results, not just the existence of these tests.

Next qualification: actual service/stream interoperability and physical-device
handoff, ingest cgroup/sandbox deployment, source-health observations, prolonged
backpressure/recovery and a combined strict WireGuard/iPhone run. Retain the
camera effort's existing capture ownership and private footage boundaries.
