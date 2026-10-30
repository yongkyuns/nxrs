# Local camera streaming qualification

Portable camera capture and one-viewer HTTP MJPEG, tested on the Freenove
ESP32-S3/OV3660 with NuttX 13.1.0, single-core at 240 MHz. This is a local-network
prototype, not an authenticated home-monitoring product or a production-readiness
claim.

For physical TLS qualification, `build.sh --tls` selects the existing shared
`LiveCamera::new_tls` output and the `esp32s3-camera-tls` profile. Run
`tls_camera SECONDS` (10–600) after bringing up the station interface. The
existing no-echo Wi-Fi input establishes the RF entropy source first.
The serial owner sends the existing two length bytes followed by SSID/password
at `NXRS_WIFI_CREDENTIALS_READY`, before handing the port to the TLS supervisor.
`tls_provision.Supervisor(serial_port, private_empty_directory, camera_ip)`
then enrolls the device-generated CSR and supplies fresh UTC over trusted USB.
It exposes the approved gateway credentials and a `ready` event for the ingest
fixture; use `--require-camera-tls`. The device listens on 8443 with no plaintext
fallback. Enrollment is bounded and public; the volatile private key stays on
the TLS worker. This controlled development path requires a trusted USB host
for every boot and for time refresh. It does not implement persistent enrollment
or an autonomous trusted clock. Preserve and verify the original firmware before
temporary flashing, and restore it after qualification.

For continuous TLS **with the existing detector**, build with
`build.sh --platform esp32s3-camera-stream-tls`, then run
`stream_test tls continuous 0 320 240 81`. This uses the same capture,
detector, reconnect and shutdown loop as HTTP. TLS owns the sole UART reader;
a sequence-bound USB stop response requests ordinary joined shutdown.

`serve.py` accepts `--tls-config FILE` alongside its existing port, Wi-Fi and
state-file arguments. The mode-0600 JSON contains `enrollment_directory`
(private directory), `ingest_program` (absolute Rust ingest executable), and
`ingest` (its configuration). Set `camera` to the camera's private IPv4:8443,
`allow_insecure_camera_http` to false, and `camera_tls` to the independently
assigned `site_id`, `camera_id` and `gateway_id`. Keep the ordinary publisher,
detector and media settings. The helper adds the issued certificate paths/pin,
writes the complete `ingest.json`, and starts Rust with `--require-camera-tls`.
Stop an existing ingest owner before starting this supervisor.

The USB development supervisor renews through joined native/ingest sessions
every 45 minutes, before the one-hour CRL expires, without closing the serial
connection. Private enrollment files are removed after native and ingest retirement.
USB/firmware/ingest faults stop supervision; they do not trigger blind resets or
plaintext fallback. Trusted USB time and per-boot enrollment remain required.

[`app/camera-stream`](../../../app/camera-stream) composes the camera service
and caller-polled [`service/stream`](../../../service/stream) server.
[`service/connectivity`](../../../service/connectivity) owns reconnect policy.
These components contain no ESP pins, NuttX headers or platform feature flags.
The fixture supplies HALs, runtime credentials and measurements. Camera and
streaming services create no threads; Wi-Fi retains its provider worker.

## Performance and qualification

The current image passed a 30-minute VGA/quality-90 run with 30,966 fully decoded
images, 61 client reconnects and one Wi-Fi disconnect/rejoin. It delivered 97.43%
of 31,784 valid captures, including startup and injected outages. Steady viewing
approaches the selected sensor configuration's roughly 18 captures/s; Wi-Fi is
no longer the large delivery bottleneck. Every counted image below was received
and fully decoded on the Mac, not merely accepted by a socket:

| Mode | Run | Fully decoded | Received frames/s | Mean JPEG size |
| --- | ---: | ---: | ---: | ---: |
| 320×240, quality 81 | 30 s | 468 | 17.83 | 3.34 kB |
| 640×480, quality 81 | 60 s | 1,008 | 17.79 | 9.53 kB |
| 640×480, quality 90 | 1,800 s, reconnect stress | 30,966 | 17.07 | 12.64 kB |

Command durations are device-timed and include Wi-Fi startup. FPS uses the
host's first-to-last decoded image interval, so it need not equal frame count
divided by command duration. Initial connection time is excluded; intervening
recovery gaps are included. Three separate quality-90 cold-start checks delivered
16.77–17.60 FPS in 20/60-second commands. Larger or more detailed scenes require
more bandwidth; the stationary ceiling scene is not a
worst-case JPEG workload. These are observations, not guaranteed rates or the
sensor's maximum in every possible configuration. Capture cadence also varies;
not every dip is a network-delivery problem. These tests do not qualify other
sensor clock presets, worst-case scene detail or every network environment.

A separate 90-second VGA test passed a ten-second stalled reader, three client
reconnects, Wi-Fi disconnect/rejoin and verified shutdown: 1,192 decoded images,
16.02 frames/s, with a largest normal-viewing frame gap of 6.25 s. The 30-minute
run's largest gap was 5.23 s during Wi-Fi recovery, with no driver-flagged
discarded frames. This qualifies that run, not indefinite reliability.

Measurements use the 881,984-byte image with SHA-256
`30f69ef93a87a1b9ba34698f90123e204b728215320562075d414c9d2e4e2ed6`, built locally
with esp-rs Rust 1.90.0.0 and GCC 14.2.0_20251107. Preserve its source, ABI,
configuration and dependency-patch ledgers alongside the reports.

### Why Wi-Fi delivery was slow

The main reproducible bottleneck was the camera clock pin's electrical drive,
not an inherent Rust or Wi-Fi bandwidth limit. The generic GPIO default was
stronger than this Freenove connection needed. An independent SDK control kept
capture, firmware, Wi-Fi association and TCP connection running while changing
only the 16 MHz clock output's drive capability:

| Clock-pin drive | Receiver throughput, two repeated phases |
| --- | ---: |
| Default capability 2 | 0.125 / 0.173 Mbit/s |
| Weakest capability 0 | 7.44 / 8.14 Mbit/s |

These are receiver measurements over the middle approximately 20 seconds of
each 30-second phase, excluding transitions. The receiver verified 99.97% of
60.9 MB overall. Sensor output drive and pixel-clock gating stayed unchanged.
This isolates the clock-pad setting; distinguishing radiated interference from
supply/ground coupling would require electrical measurements.

Patch 0089 makes clock-pin drive board-configurable, preserving capability 2
for other boards. The Freenove streaming profile selects 0 at both sensor
probing and capture startup, without changing frequency or adding buffers,
threads or a portable HAL option. Other wiring must validate its own signal
integrity; do not blindly reduce every GPIO's drive strength.

The earlier result was 3.66 decoded versus 17.88 captured frames/s, but even
that old binary later reached 16.03 FPS. Separate-boot comparisons were noisy;
the within-connection electrical control above establishes the cause much more
directly than attributing the entire improvement to software changes.

There were also concrete software problems:

- **Instruction-cache pressure.** Changing only the instruction cache from
  16 to 32 KiB increased a short VGA check from 15.93 to 17.64 decoded frames/s.
  Steady idle-derived non-idle time fell from about 95% to 54%. The trade-off is
  16 KiB less internal RAM, not an additional thread or a larger image queue.
- **TCP recovery.** Patch 0086 accepts valid cumulative ACK progress after a
  partial retransmission. Patch 0087 removes NewReno's permanently shrinking
  congestion-window ceiling. Actual-source tests reproduce both defects and
  fail when their fixes are removed. NewReno remains disabled in this profile;
  its fix is not an explanation for this fixture's current FPS.
- **Recoverable camera errors.** The adapter previously treated a driver-flagged
  bad frame as a permanent fault. It now validates ownership, discards and
  requeues that frame without publishing it. `camera_discarded` counts these
  events separately from network errors; malformed ownership metadata and
  queue failures remain fatal. The change is tested, but the exact trigger of
  the earlier intermittent startup rejection was not captured.
- **Camera clock selection.** Patch 0088 prefers a fixed 160 MHz PLL when an
  integer divider produces the requested clock, retaining the 240 MHz fallback
  for other supported frequencies. The sensor clock here remains 16 MHz.

The independent reference used installed ESP-IDF 5.5.3 and camera driver 2.1.8,
single-core 240 MHz, 32 KiB instruction cache, 40 MHz flash/PSRAM and a 32 KiB
TCP send buffer. It establishes network headroom, not a PHY ceiling or a fair
RTOS benchmark. At 17.8 VGA frames/s these scenes need roughly 1.4 Mbit/s at
quality 81 and 1.8–2.0 Mbit/s at quality 90, before protocol overhead. The NuttX
profile keeps its existing, smaller network pools. Neither PHY rates nor
socket acceptance establishes delivered video performance; use decoded images.

Two FIFO DMA buffers, fresh-VSYNC synchronization, cache maintenance restricted
to received data, and bounded 2 KiB coalesced HTTP writes remain in place.
The optional [vendor camera reference](reference) is not a build dependency.
No debugger was attached during performance tests. In the endurance run,
calibrated owner-observation-to-host-decode latency averaged 231.5 ms over 30,546
measured frames; 420 were unmeasured and maximum clock uncertainty was 62.7 ms.
This is not sensor-exposure-to-display latency or a minimum-latency benchmark.

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

In this image's 180 endurance samples, user heap stayed at 1,743,496 B;
1,689,600 B of that is the selected image capacities above. Kernel heap ranged
from 58,032 to 79,392 B, and mean idle-derived CPU non-idle time was 62.7%.
Interrupt work can be charged to the interrupted task, including idle, so this
counter is not a complete measurement of CPU utilization. QVGA used 821,896 B
of user heap in its short check. The 921,600 B user-heap difference exactly
matches the additional DMA capacity. These are sampled heaps/CPU, not total RAM, worst-case bounds or
per-component attribution, and the durations differ.

After the endurance command exited, two readings on the original serial connection showed
22,328 B user heap and 53,224 B kernel heap; uptime was 1,800.83 s. The image
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
Use `--client-reconnect-every 0 --reconnect-at 0` for continuous viewing without
injected client or Wi-Fi reconnections; the default client stress interval is 30 s.
Start from an idle NSH prompt. This Mac's USB-UART open/close can reset the ESP;
post-stop checks therefore use the original connection before closing it.
Instant whole-app port rebinding is not qualified; client/Wi-Fi recovery keeps
the existing listener.

### Continuous inspection

The bounded qualification command deliberately stops after `SECONDS` (at most
one hour). It should not be used as an unattended camera feed. In particular,
blindly rerunning it can fail to bind port 8080 while the previous TCP connection
is still retained by NuttX. This is distinct from a camera capture failure.

For host-assisted inspection, rebuild the fixture with continuous-mode support
and run:

```sh
python tests/nuttx-std/stream/serve.py --port /dev/cu.YOUR_ESP32 \
  --wifi-file /private/tmp/wifi \
  --state-file /private/tmp/camera-session.json
```

The state path must be new and outside the checkout. The Wi-Fi input has the
same private-file/no-echo requirements as the qualification runner. This command
starts `stream_test continuous 0 320 240 81`, keeps one USB console open, and
holds a temporary macOS idle-sleep assertion for the lifetime of the session.
There is no hourly expiry, forced Wi-Fi reconnect, or routine listener rebind.
It records numeric telemetry and distinguishes firmware failure, boot, USB loss
and clean shutdown. After USB loss it checks the original USB device identity
before reconnecting; it adopts a running command or starts one after an NSH
prompt. It does not reset or flash hardware, conceal panics, or send credentials
to a replacement board. Repeated USB faults or a missing console stop supervision
with a failure record; this is not an autonomous firmware boot service.

Press Ctrl-C or send SIGTERM to the supervisor for a clean stop. In continuous
mode it sends the exact `stop` line and waits for the existing camera stop,
Wi-Fi disconnect and provider join before closing the console. An absent USB
control channel alone does not stop video. Resetting or removing power from the
board still loses its in-memory credentials; recovery requires the Mac supervisor.

The original `run.py` remains a bounded qualification tool. Continuous inspection
is not itself a soak result, a public/secure camera endpoint, gateway deployment,
or proof that an electrical reset trigger has been fixed.

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


## Detector HAL qualification

The default `esp32s3-camera-stream` product profile supplies PICO behind the
common `hal/detector` API. Build with
`--platform esp32s3-camera-stream-vww` for the original pure Rust VWW model, or
`--platform esp32s3-camera-stream-video` for the video-only baseline. The profile
owns the model, stack and sampling budget; model dependencies are included
automatically. Add `--probe` to enable stage timings and the serial-only
`detect-batch` command with PICO. That command starts no camera or Wi-Fi.
Probes observe the actual
HAL pipeline. See [current PICO performance](../../../docs/camera-pico-performance.md),
[original measurements](../../../docs/camera-pico-qualification.md)
and the [HAL ownership/API guide](../../../hal/detector/README.md).

The product factory defaults to a 750 ms PICO analysis budget and retains the
separate two-second VWW budget. The
stream runner's optional `--sample-ms 250..3000` supplies an explicit product
budget; it becomes a trailing `sample-ms=N` console argument. The argument comes
after `no-detection` when both are supplied. Ordinary builds without a detector
keep their existing command. The common worker never inspects model or platform
identity. The report distinguishes the requested override from the interval
reported by the running firmware, including when the factory default is used.
A lower analysis duty cycle protects camera throughput; video FPS and inference
cadence must be reported separately.
