# Experimental Wi-Fi CSI presence sensing

Espressif provides an official
[Wi-Fi sensing demo](https://github.com/espressif/esp-csi/tree/8633d67152db2808f141cc1595970aa9cf406045/examples/esp-radar/wifi_sensing_demo)
using `esp_wifi_sensing` and `esp-radar`. It supports ESP32-S3 and both motion
and presence hints. An ESP32 and a compatible normal Wi-Fi router can provide
the sensing link; a dedicated ESP transmitter is another arrangement. Detection
depends on placement, environment, traffic, and calibration. It does not identify
people, count occupants, or reliably establish that a home is empty.

The vendor example requires ESP-IDF >=5.4. It is **not a NuttX application**.
Our checked-in NuttX ESP32-S3 Wi-Fi adapter and Nxrs station HAL currently expose
no CSI sample API. Do not link the vendor FreeRTOS sensing FSM into that provider
or claim that a host test qualifies its native ABI.

## What is implemented

`service/presence` is a reusable, dependency-free, allocation-free Rust policy
component. A caller feeds typed classifier hints and polls its deadline from
the caller's existing event loop. It adds no worker. Its responsibilities are:

- Require calibration/readiness before accepting hints.
- Confirm both a minimum number of reports and a minimum elapsed interval.
- Hold a detected hint briefly to reduce flicker, while letting lost input
  invalidate it immediately.
- Reject duplicate/reversed source timestamps without refreshing the deadline.
- Invalidate old evidence on explicit source restart or re-calibration.
- Distinguish `Unknown`, `NotDetected`, motion evidence, and presence evidence.

This component consumes classifier output; it does not implement Espressif's RF
feature extraction or a new presence-detection algorithm.

`tests/csi-presence` connects the official demo's `HMS:` serial diagnostics to
this Rust component. It explicitly selects one peer MAC, bounds input to 2048
bytes per complete line, and emits experimental JSON transitions. Extra demo
channels cannot contaminate the selected channel. One bounded stdin reader
allows the owner to expire its deadline even if stdin remains open and silent.
It opens **no USB device or network listener**, sends no firmware commands,
and does not change the camera, gateway, enrollment, iPhone UI, or recording
policy. Vendor C is not copied into Nxrs. The exact inspected upstream revision
is in `tests/csi-presence/upstream.json`.

## Important diagnostic limitation

The demo's `ts_ms` is the time it **prints a diagnostic report**. Its public
serial message does not include the last CSI packet timestamp, an RF packet
counter, or a reconnect generation. Repeated cached classifier values can be
printed with new report timestamps. Consequently this bridge can detect loss
of the **report stream**, but cannot certify RF-sample freshness or a healthy
Wi-Fi sensing link. Every output declares `advisory_only: true` and
`rf_sample_freshness: "not_exported_by_demo"`.

`not_detected` means a calibrated classifier reported no detection; it must
never be translated into “room empty.” Neither replay nor fresh serial reports
are physical presence qualification. No automatic recording, absence decision,
notification or product “Live” status uses this experimental path.

## Run without hardware

```sh
cargo test --locked -p nxrs-presence-service
cargo test --locked --manifest-path tests/csi-presence/Cargo.toml
cargo run --locked --manifest-path tests/csi-presence/Cargo.toml -- \
  --peer 02:00:00:00:00:01 --mode replay \
  < tests/csi-presence/fixtures/lifecycle.log
```

The fixture is **hand-authored protocol test data**, not a recording from the
home or evidence of detection accuracy. Replay uses the source report clock
to exercise timing without sleeping. EOF always invalidates presence. Observer
mode instead uses the Mac's monotonic clock and expires after two seconds of
report silence; both modes are explicitly labeled in output.

## Physical trial on a spare board

1. Use a spare ESP32-S3 (or another supported Wi-Fi ESP32) and the pinned vendor
   example. Keep the working camera's firmware and USB supervisor unchanged.
   Provision Wi-Fi privately using the vendor tooling; do not commit credentials.
2. Build the example with web-serial diagnostics enabled. Start its stream and
   perform empty-room training with the vendor console. The relevant commands
   are `HMSCMD HELLO`, `HMSCMD START_STREAM`, `HMSCMD TRAIN_START AP`, and
   `HMSCMD TRAIN_STOP AP`. Follow the component's calibration guidance; do not
   treat a successful command acknowledgement as a completed calibration.
3. Have the board's **single serial owner** provide a capture/pipe to stdin.
   Do not open the camera's serial port or let two tools own the same board.
   Select the AP BSSID from that private capture rather than assuming the
   example's extra hard-coded peer addresses are real transmitters.
4. Feed that selected stream with `--peer <actual-peer-MAC> --mode observer`.
   This is read-only evaluation. Record ground truth separately: empty room,
   walking, seated still, small movement, fans/curtains/pets, room next door,
   router power loss and recovery, changing traffic and camera streaming load.
5. Measure false triggers, missed presence, report and RF cadence, detection
   latency, disconnect recovery, CPU/memory and any effect on video throughput.
   No accuracy claims are justified by the protocol regression tests.

## Pinned NuttX radio qualification branch

The separate [CSI radio fixture](../tests/csi-radio/README.md) builds a dedicated
qualification image for a spare or an explicitly authorized camera maintenance
window with complete firmware backup/restoration. Rust owns capture bounds, callback generations, RF timestamp
validation, gain baseline/compensation sequencing and cleanup. The test-local C
witness only translates the pinned SDK headers and calls SDK functions. The
production Wi-Fi HAL, camera profiles, gateway and iOS client remain unchanged.

The kernel-only QEMU witness executes the unchanged IDF 6.0 ESP32-S3 gain and
motion archives against the pinned IDF 6.1-derived SDK. Three fresh boots passed
818 checks each, including all four motion allocation-failure sites, actual SDK
logging, host numerical oracles and two concurrent numerical workers. A
separate deliberate-failure boot failed as required. PHY gain calls in that
witness are recording stubs; synthetic data proves neither RF metadata semantics
nor presence accuracy. Pins, reproduction commands and evidence are in the
fixture; no vendor archives, credentials or private RF captures are committed.

The physical fixture collects raw CSI from one explicit AP BSSID and exchanges
bounded UDP echoes or an explicitly selected TCP control while the existing NuttX station owner keeps checking link
health. Capture does not enable promiscuous mode or force receiver gain. A
separate explicit gain-forcing trial restores automatic gain before stopping
CSI. Real RF field interpretation, actual PHY effects, reference comparisons,
detection accuracy and camera coexistence remain pending. A capture PASS labels
those qualifications as unverified rather than promoting the experiment into a
product feature.

The 2026-10-22 controls isolated and fixed console-command truncation (a 64-byte
shell line and USB receive ring). With checked shell capacity, paced input and
target option/port acknowledgment, UDP transport passed both without CSI
initialization (294 echoes) and with CSI initialized but capture disabled
(283 echoes). Real capture then exported 98 CSI records before its two-second
RF freshness deadline failed while UDP continued. The original firmware and
WebRTC playback were restored and verified. This is partial radio evidence;
continuous CSI, private gain semantics and presence detection remain unqualified.
See [sanitized control evidence](../tests/csi-radio/results/esp32s3-transport-controls-2026-10-22.json).

Follow-up diagnostics recorded 299 matching HT callbacks, no callback drops and
an unchanged AP; a pending UDP request coincided with the RF expiry. A separate
UDP-only control also failed. The single-flight fixture could stop stimulating
CSI after a lost echo. A bounded 32-entry echo window now keeps sending at 100 ms
intervals while preserving both two-second deadlines and zero-loss acceptance.
This is a test-fixture correction, not a demonstrated radio or presence fix.
An SPSC handoff also removes a callback/consumer mutex collision seen in the
first bounded-flight trial. With both changes, 208 frames and 159 gain
compensations were accepted with no callback drops before an overdue UDP echo
failed the trial at 22.070 seconds. CSI was still fresh (50 ms) at that failure,
so loss of the echo did not imply loss of CSI delivery. The requested 60-second
qualification did not pass; all four follow-up windows restored original
firmware and decoded real video. No presence decisions were produced.
See [the retained diagnostic failures](../tests/csi-radio/results/esp32s3-callback-diagnostics-2026-10-22.json).

A separate buffered TCP control then passed 300 echoes in 30 seconds. Following
60- and 120-second CSI sessions passed with 1084 and 2118 accepted frames, 1035
and 2069 valid gain-compensation calls, every echo returned, and zero callback
drops. Both completed their requested native observation windows and confirmed
peer EOF, CSI retirement, station disconnect, worker join and zero NSH status.
The test image explicitly enables bounded TCP write buffers because the pinned
unbuffered sender can wait despite nonblocking mode; producer/consumer ownership
and both two-second deadlines are unchanged. All three TCP maintenance windows
restored original firmware and verified real WebRTC video.

This establishes bounded TCP-stimulated capture and same-boot re-acquisition on
the pinned stack. Earlier failed/cancelled attempts are retained, and TCP success
does not qualify UDP delivery. Independent boots, longer/adverse-network runs,
private gain semantics/reference comparison, coexistence and the complete
presence pipeline remain open. No presence or occupancy decisions were produced.
See [the TCP control evidence](../tests/csi-radio/results/esp32s3-tcp-controls-2026-10-22.json).

## Native Nxrs integration gate

Once the separate-board experiment establishes useful results:

1. Run the separate-board fixture against the exact pinned Wi-Fi/PHY blobs.
   Static/link/QEMU checks establish callable numerical kernels, while real CSI
   continuity, private gain metadata and PHY effects still need physical evidence.
   CSI is disabled at Wi-Fi initialization in the normal camera build. Do not
   replace the camera firmware as part of an unattended monitoring qualification.
2. Put acquisition in `hal/wifi/src/platform/nuttx/` with a typed capability in
   the owning Wi-Fi crate, explicit unsupported capability reporting and shared
   ownership of the radio. Keep target-header layout translation in that
   provider's `ffi/`; callback copying must be bounded and return promptly.
3. Keep resource lifetime, overflow counters, validation, source epochs,
   calibration policy, debounce, loss and recovery in Rust. Prove callback
   quiescence before releasing callback context or buffers. Do not import the
   example's C/FreeRTOS product state machine into a native bridge.
4. Carry actual RF frame progress and channel/configuration identity to the
   classifier. Missing/stalled samples, queue overflow, changed channel/BSSID
   and invalid calibration must produce `Unknown`, even if reports continue.
   CSI/RSSI measurements are not authenticated human-presence evidence.
5. Expose optional authenticated monitoring data to the gateway/iOS only after
   RF freshness and placement qualification. Keep CSI evidence separate from
   camera motion/person detection; do not silently suppress existing recordings.
   Physical camera coexistence and sustained resource tests must pass before
   enabling it in a home-monitoring profile.

Further hardware qualification and the production CSI provider are pending. Host tests
qualify policy, protocol isolation, bounds, rollback and failure behavior. The
separate kernel QEMU evidence qualifies only its explicitly exercised runtime
paths.

References: [ESP-CSI overview](https://github.com/espressif/esp-csi),
[Espressif sensing component](https://components.espressif.com/components/espressif/esp_wifi_sensing),
[HAL architecture](hal-platform-architecture.md),
[NuttX device access](nuttx-device-access.md).

## Complete IDF reference experiment, 2026-10-23

The [full reference fixture](../tests/csi-radio/presence/README.md) now runs
unchanged esp-radar 0.3.4 and esp_wifi_sensing 0.1.1~3 on the native IDF 6.0
ESP32-S3 reference, including preprocessing, native numerical kernels, empty-room
training APIs and presence/motion classification. This is separate from a future
Rust/NuttX implementation; the production HAL and client remain unchanged.

The first **unlabelled** logarithmic-amplitude run could not finish calibration:
one template, zero background points, `ESP_ERR_INVALID_STATE`. A subsequent
instrumented run did calibrate and emitted presence hints, but 642 of 2,685 native
numerical windows contained nonfinite inputs/outputs. The unchanged vendor
preprocessing calls `log10f(amplitude)` without guarding zero. This is a plausible
source-level explanation, not a measured attribution of each invalid sample.

Selecting the vendor's supported **linear-amplitude** option before admitting the
AP peer removed observed nonfinite inputs/outputs in a third run: 2,715 windows,
zero nonfinite inputs/outputs and no uninspected calls. It collected 45 templates
and 1,129 background points, completed calibration, and emitted 88 presence-hint
reports. **Two native numerical calls still returned errors**, first observed
around 86.5 and 110.1 seconds; their exact status codes were not retained by this
witness. They remain an execution/reliability gate. No gain forcing, classifier
threshold adjustment or substituted numerical output was used.

No person supplied occupancy labels, and no empty-room baseline was confirmed.
These positive reports are **not proof that someone was detected** or a count
of people. The reference snapshot also lacks RF freshness, and the upstream
consumer can skip failed numerical calls while retaining old classifier state.
Do not deliver this as a product presence/absence decision. The next test needs
a supervised empty-room baseline, entry/movement, seated stillness and exit,
with failure/staleness handled as unavailable and native errors diagnosed.

Every maintenance window restores the original full flash and verifies real
WebRTC frames. The two early windows initially received 404 while their publisher
reconnected; subsequent fresh real-frame verification succeeded. Those delays
and the unsuccessful calibration are retained, not promoted into clean results.
See the sanitized full-reference receipt under `tests/csi-radio/results/`.
