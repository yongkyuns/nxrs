# Local camera streaming qualification

Portable camera capture and one-viewer HTTP MJPEG, tested on the Freenove
ESP32-S3/OV3660 with NuttX 13.1.0, single-core at 240 MHz. This is a local-network
prototype, not an authenticated home-monitoring product or a production-readiness
claim.

[`app/camera-stream`](../../../app/camera-stream) composes the camera service
and caller-polled [`service/stream`](../../../service/stream) server.
[`service/connectivity`](../../../service/connectivity) owns reconnect policy.
These components contain no ESP pins, NuttX headers or platform feature flags.
The fixture supplies HALs, runtime credentials and measurements. Camera and
streaming services create no threads; Wi-Fi retains its provider worker.

## Performance and qualification

The earlier QVGA image passed a 30-minute run at 3.54 received / 4.44 captured
frames/s. The optimized image passed 30 minutes at **640×480, quality 81**:
6,593 fully decoded images, **3.66 received / 17.88 captured frames/s**,
including a forced Wi-Fi disconnect/rejoin and 68 client reconnects. VGA carries
four times as many pixels, at roughly the earlier QVGA received rate. The short
QVGA check below received 10.09 frames/s. These are sequential observations,
not a controlled benchmark or guaranteed rates; LAN delivery remains variable.

The largest inter-frame gap was 4.45 s, at Wi-Fi recovery. The run passed
decoding, continuity, camera stop, worker join and shell-status checks.
The final no-frame-to-completion interval was
0.39 s, measured after joining the decoder but before the serial resource audit.
This does not guarantee smooth, low-latency video.

Measurements were taken on 2026-10-20 with an 881,960-byte firmware binary,
SHA-256 `8f7a29e659132d218c84493b439f58ac18ce79dabbe0f77f7ce786781aa89c75`.
Tools were esp-rs Rust 1.90.0.0 and GCC 14.2.0_20251107. Preserve the build
ledgers and qualification reports with that binary.

These short checks use the same final image and a stationary low-detail scene.
The viewer intentionally reconnects every 30 seconds, in these checks and the
endurance run. The 180-second VGA check also forces a Wi-Fi rejoin at 60 seconds:

| Mode | Run | Fully decoded | Received frames/s | Mean JPEG size |
| --- | ---: | ---: | ---: | ---: |
| 320×240, quality 81 | 30 s | 239 | 10.09 | 3.55 kB |
| 640×480, quality 81 | 180 s | 896 | 5.07 | 9.93 kB |
| 640×480, quality 90 | 45 s | 52 | 1.40 | 12.66 kB |

Both VGA qualities captured about 17.8 frames/s. Quality 90 produced smaller
JPEG quantization coefficients and larger files, not just a different requested
setting. These sequential rates do not isolate its effect on network throughput.
A separate 120-second VGA test passed a ten-second stalled reader, client
reconnects, Wi-Fi disconnect/rejoin and explicit shutdown: 348 decoded images,
3.57 received frames/s, with a largest frame gap of 7.54 s.

Throughput depends on scene complexity, Wi-Fi conditions and reader behavior.
The test scene is a low-detail ceiling, not a worst-case JPEG-size workload.
Capture FPS is not received or displayed FPS. The host fully decodes every
counted image; the firmware's `delivered` counter only means complete parts
accepted by the socket. Software capture timestamps describe owner observation,
not sensor exposure. Calibrated latency ends at host decode, not physical display;
reports retain clock uncertainty and explicitly unmeasured frames.
Received FPS is measured between the first and last decoded image, includes
intervening reconnect gaps, and excludes initial startup and the separate
stalled-reader probe. These are observations, not guaranteed rates or a board
maximum. The endurance run's mean observation-to-decode latency was 771 ms
over 6,185 calibrated frames; 408 frames were unmeasured. Calibration uncertainty
reached ±364 ms. This does not establish a precise
latency improvement; hardware exposure/display timing was not measured.

### What changed

- Two retained FIFO DMA buffers let capture continue while the owner consumes a
  frame. Buffer identity and ownership are checked before copying/requeuing.
- The camera driver no longer clears the entire raw capacity in its completion
  interrupt. It invalidates only the received, cache-line-rounded extent;
  required pre-DMA cache flushing and short-frame rejection remain.
- Receiver reset needs a fresh VSYNC transition. A hardware pulse counter saw
  548 sensor edges but only 274 receiver interrupts. Explicit synchronization
  recovered the missing frames. The fix gates image data throughout
  synchronization; a lost blanking window retries at a fresh physical boundary
  without publishing a partial frame. Native tests vary HREF/VSYNC mid-pulse.
- HTTP writes combine adjacent multipart headers and payload in a bounded
  2 KiB stack batch. Partial writes commit only accepted bytes; no growing
  queue or extra full-frame allocation is introduced.
- The bounded TCP send threshold is 16 KiB rather than 8 KiB, so this scene's
  roughly 10 KiB VGA image need not cross the threshold mid-frame. This is a
  platform resource choice, not an application workaround or a universal
  optimum; it does not remove other TCP/Wi-Fi delivery delays.

An optional [reference fixture](reference) uses the already-installed Espressif
Arduino camera/Wi-Fi drivers without modifying them. QVGA capture-only measured
17.69 frames/s with matching sensor PLL/timing readbacks. Its small HTTP wrapper
is not the vendor web-server application, and variable LAN delivery results
are not a hardware ceiling or an RTOS fairness benchmark. It is not a build
dependency of nxrs.

## Memory and bounds

| Selected mode | Two HAL DMA capacities | Staging + one history slot + outgoing frame | Total payload capacities |
| --- | ---: | ---: | ---: |
| 320×240 JPEG | 307,200 B | 460,800 B | 768,000 B |
| 640×480 JPEG | 1,228,800 B | 460,800 B | 1,689,600 B |

These are application-selected capacities, not fixed Rust/framework overhead.
NuttX currently requires raw-size USERPTR capacity even for JPEG; published
frames contain only actual compressed bytes. Each service buffer holds at most
153,600 B; a larger JPEG returns `Full`, rather than truncating or growing storage.
Static RTOS/Wi-Fi storage and other user/kernel allocations are additional.
This profile uses PSRAM and does not qualify a 250 KiB-RAM device.
Quality is set before capture through the portable
[`Camera` API](../../../hal/camera/README.md); 81 preserves the OV3660's original
scale-12 default. Higher quality generally needs more network bandwidth.

Across all 180 endurance samples, user heap stayed at 1,743,496 B;
1,689,600 B of that is the selected image capacities above. Kernel heap ranged
from 58,272 to 92,024 B, and mean idle-derived CPU non-idle time was 65.9%,
including interrupts. QVGA used 821,896 B of user heap and 53.5% CPU in its
short check. The 921,600 B user-heap difference exactly matches the additional
DMA capacity. These are sampled heaps/CPU, not total RAM, worst-case bounds or
per-component attribution, and the durations differ.

After command exit, two readings on the original serial connection both showed
22,328 B user heap and 54,904 B kernel heap; uptime was 1,800.84 s. The image
storage was released without a reset. This is not proof of indefinite leak
freedom or zero retained native allocations. Separate-port readings were
invalidated because opening/closing this Mac's USB-UART connection reset the
board. No debugger was attached during qualification.

One active viewer, a 1 KiB request limit and one reusable outgoing frame buffer
bound server storage. Each poll has fixed I/O-call and byte budgets. A client
is retired if a complete header/frame cannot be accepted by the socket within
three seconds. This is not an on-wire/display deadline. On reconnect it receives
the latest frame, not accumulated history. Capture continues during Wi-Fi outage.

The profile uses nonblocking TCP write buffering: a 16 KiB send threshold
(one bounded write can cross it), 100×400-byte shared packet buffers, 16 fixed
connection/write-chain slots and 64 network callback slots, without dynamic pool
growth. Read-ahead allocation is throttled with eight packet slots remaining,
leaving buffers available for TCP writes. Payload space and callback availability
are separate resources. The threshold is not an extra permanent 16 KiB allocation
per viewer. The owner polls at up to 200 Hz. Power use is not qualified.

## Reproduce locally

Use the [ESP Rust/NuttX tool environment](../../../docs/nuttx-std.md). On macOS,
set `NXRS_ESP32S3_NATIVE_TOOLS=1` and `NXRS_QEMU_TOOLS_DIR` to a Mac-hosted
bundle containing `environment.sh`; do not reuse a Linux tool installation.
Builds use at most two workers and patch isolated dependency copies, not the
installed SDK or tracked checkouts.

```sh
cargo test --locked -p nxrs-camera -p nxrs-camera-service \
  -p nxrs-camera-stream -p nxrs-stream-service -p nxrs-connectivity-service
bash tests/nuttx-std/stream/build.sh
```

Back up the verified ESP before flashing
`target/camera-stream-device/nuttx/nuttx.bin` at offset zero with esptool. Do not
flash the Pico. Install host packages from `requirements.txt` into a local venv:

```sh
python tests/nuttx-std/stream/run.py --port /dev/cu.usbmodem14201 \
  --host-ip YOUR_MAC_LAN_IPV4 --wifi-file /tmp/wifi --seconds 1800 \
  --width 640 --height 480 --quality 81 --reconnect-at 600 \
  --output /private/tmp/stream-frame.jpg --report /private/tmp/stream-report.json
```

Use fresh private output paths. For a separate 90-second backpressure test, add
`--slow-reader --reconnect-at 30`. It stalls a small-window reader for ten
seconds, requires a bounded peer close/reset, then verifies normal viewing.
Start from an idle NSH prompt. This Mac's USB-UART open/close can reset the ESP;
post-stop checks therefore use the original connection before closing it.
Instant whole-app port rebinding is not qualified; client/Wi-Fi recovery keeps
the existing listener.

The credential file contains `ssid=...` and `password=...`, is owned by the
current user, mode 600 or stricter, and outside the repo. `SSID: value` and
`password: value` also work; use `=` for literal leading spaces. Credentials
travel over no-echo serial input, never build configuration or shell arguments.
Images/raw evidence remain private. Plain HTTP is for a trusted LAN only.

## Acceptance and limits

Require decoder-valid images at the requested resolution, continuing capture,
client reconnects, device Wi-Fi disconnect/rejoin, explicit camera stop, joined
Wi-Fi/host workers and successful NSH status. The host requires a first decoded
frame within 30 seconds and rejects inter-frame or final no-frame gaps over
10 seconds. Earlier valid frames cannot qualify a later stalled stream. Clock
probes pause during transport recovery until a real frame decodes; unavailable
calibration never invents latency. The five-second per-I/O timeout accommodates
the native SYN/ACK retry schedule without relaxing the ten-second continuity
limit. Malformed clock data and worker shutdown failures remain errors.

Portable tests cover partial writes/deadlines and two-buffer ownership/error
paths. Native sanitizer harnesses execute applied driver functions for JPEG
bounds, cache work, synchronization, queue rollback and scalar controls. These
do not replace hardware qualification or establish indefinite leak freedom.
Dependency fixes remain traceable [upstream patchsets](../../../docs/upstream-patchsets.md),
including camera/control patches 0073–0075 and 0079–0085, performance accounting
0076, and packet-consumption/TCP ACK handling 0077–0078.
