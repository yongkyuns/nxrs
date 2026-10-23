# Post-platform-refactor physical camera qualification

These measurements use clean commit `83db7b80d482961d0f583dbc8cb7c6beeea24094`,
which contains merged PR #54 (`9567577`) plus the macOS Bash 3.2 frontend fix.
The default build previously expanded an empty optional-argument array under
`set -u`. The fix changes argument forwarding, not camera or detector behavior.
The new regression executes the complete frontend with a stub native backend;
CI now also runs that check on macOS.

The flashed image is 1,831,456 bytes, SHA-256
`a03006d1e47fc8fe6a8208de631648c0b45d303c86a271ef7524454bb3aaa0cf`.
`firmware.json` records the source/configuration hashes, pinned numerical SDK
revision and target ABI witness status. The source build was clean. Only the
ESP32-S3 was flashed, after preserving and verifying its full 16 MiB flash.
The target gate checked 117 witnesses: 116 compatible and the existing known
unsupported raw `SIG_IGN` value; it does not qualify general signal APIs.
Credentials, trust files, firmware binaries, camera images and raw device logs
remain private.

## Camera and inference

One physical Freenove ESP32-S3/OV3660, single core at 240 MHz, QVGA quality 81,
default PICO profile and no probes or sampling override:

| Run | Duration | Decoded MJPEG FPS | Completed observations/s | Failures | Camera discards |
| --- | ---: | ---: | ---: | ---: | ---: |
| Same image, detection disabled | 60 s | 17.78 | — | — | 0 |
| Detection enabled | 600 s | 17.77 | 1.32833 | 0 | 0 |
| Stalled reader, client reconnects and forced Wi-Fi reconnect | 120 s | 16.52 | 1.325 | 0 | 0 |

The steady run completed 797 observations. Its 750 ms profile budget is distinct
from achieved cadence and video FPS. Mean inference call wall time was 606.36 ms,
maximum 726 ms; these include preemption and are not numerical-kernel CPU time.
Idle-derived non-idle samples averaged 893.1/1000 in this scene; this is not a
complete CPU accounting measurement or a claim of improved compute efficiency.
The earlier pinned-model vendor/stage comparison remains in
[PICO performance](../../../../docs/camera-pico-performance.md); no new engine
or frozen-dataset accuracy result is claimed here.

Sampled steady user heap ranged from 2,518,640 to 2,555,112 bytes and kernel heap
from 58,544 to 60,200 bytes. The detector stack used 53,040 of 130,848 observed
usable bytes. Both detection runs returned to 22,368 user and 53,208 kernel heap
bytes after explicit shutdown, before closing the original serial connection.
These bounded samples do not establish indefinite leak freedom. Recovery had
one successful Wi-Fi rejoin and a largest normal-reader frame gap of 6.203 s.
Its aggregate FPS includes injected interruptions.

Video FPS uses the first-to-last decoded-frame interval. Startup and any initial
slow-reader phase before normal decoding are excluded; later client/Wi-Fi gaps
are included. Requested seconds describe the device command duration, not that
receiver interval. Observation cadence uses the last device detection report's
uptime instead.

The individual reports preserve numeric telemetry, continuity checks and
post-stop samples. Device addresses and image paths are removed. `summary.json`
distinguishes receiver timing, observation uptime and requested duration.

## Delivery and phone

The production camera feed and authenticated ingest were restored. Existing
protected gateway enrollment/trust files still match their recorded hashes.
The signed app was installed over its existing bundle identity and enrollment.

An independent authenticated WHEP receiver decoded **1,065 frames in 60.002 s**
(**17.749 FPS**) while receiving 80 distinct observations (1.3333/s). It used the
existing explicit CA and validated the versioned SSE schema. There was one
source session, zero inference failures, zero camera discards and at least
17,520 bytes of owner-stack margin. `gateway-whep.json` retains these numeric
records. This localizes the lower hosted-phone result downstream of the camera
and gateway, rather than demonstrating an edge capture/inference throughput loss.

Two 60-second hosted tests on the physical iPhone 14 Pro decoded **14.65 and
15.14 FPS**, separately from the camera's 17.77 FPS. All **651/651 and 671/671**
sampled decoded buffers had matching metadata, representing 81 and 80 distinct
observations. The production model clears detection to unavailable on Pause,
closes its peer and resumes with a new peer; foreground policy callbacks also
close and recover playback. The second sample asks the host's initial root to
use its existing onboarding preview while the measured model uses real saved
enrollment. These are instrumented decoder samples, not an uninstrumented
phone display-throughput claim; the remaining FPS gap needs attribution.

The XCTest witness is retained as `PhoneHostTests.swift`; it uses saved enrollment
and contains no credentials. It is hosted in a disposable copy of the app's
Xcode project with the existing local signing identity. No production test
switch or extra installed app is required. A separate UI runner could not be
installed because Apple's free-profile app limit was reached. Thus actual
Home-button backgrounding and full-screen UI acceptance are not refreshed.
The Mac system-trust overlay probe also rejected the local certificate
(`URL error -1202`); system trust was left unchanged.

The isolated synthetic reference uses current Rust camera/stream sources,
encoder, SEI, gateway and native SDK. Both runs passed before/after a source
stall/restart, with zero red/person versus blue/clear pixel-metadata mismatches.
Its FPS is not a physical camera benchmark. Rust policy tests also reject stale
results and clear localized presence when accepted boxes disappear despite raw
scores remaining in the hysteresis band. A person physically entering/leaving
this room was not exercised; no new accuracy claim is inferred from a clear
scene.

## Reproduce

Prepare the pinned SDK and native tool environment as documented in the
[stream fixture](../../../nuttx-std/stream/README.md), then run the default
`bash tests/nuttx-std/stream/build.sh`. Preserve the correct board's flash and
verify its stable USB identity before flashing `nuttx.bin` at offset zero.

Use `tests/nuttx-std/stream/run.py` with fresh private output/report paths,
`--seconds 60 --no-detection --client-reconnect-every 0 --reconnect-at 0` for the
control, `--seconds 600 --require-detection --client-reconnect-every 0
--reconnect-at 0` for steady inference, and `--seconds 120 --require-detection
--slow-reader --reconnect-at 40` for recovery. Supply the owned serial port,
host LAN address and private Wi-Fi file as described by that fixture. Do not
override the model or sampling budget. Restore the continuous supervisor and
ingest after the bounded commands.

The synthetic pixel reference command is documented in the preceding
[overlay qualification](../2026-10-22-overlay/README.md). `validation.json`
records the local checks and separates phone acceptance limits from host tests.
