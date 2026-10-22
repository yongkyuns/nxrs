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

## Native Nxrs integration gate

Once the separate-board experiment establishes useful results:

1. Qualify whether the exact pinned NuttX Espressif Wi-Fi blob/config supports
   CSI and callback registration. If unavailable, add a traceable upstream patch
   or keep CSI on an external ESP-IDF sensor. Do not replace the camera firmware
   as part of an unattended monitoring qualification.
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

The hardware experiment and native CSI provider are pending. Current host tests
qualify policy, protocol isolation, bounds and report failure behavior only.

References: [ESP-CSI overview](https://github.com/espressif/esp-csi),
[Espressif sensing component](https://components.espressif.com/components/espressif/esp_wifi_sensing),
[HAL architecture](hal-platform-architecture.md),
[NuttX device access](nuttx-device-access.md).
