# CSI radio compatibility qualification

This branch prepares **tests**, not a production presence feature. The gateway,
iPhone, certificates and recording policy are outside this fixture. A camera
maintenance trial requires the explicit authorization and backup procedure below.
Neither build command opens a serial port, discovers devices, flashes
firmware, changes an AP or starts a hardware test.

Two separate images distinguish numerical runtime evidence from physical RF:

| Image | Purpose | Native PHY gain calls | Radio evidence |
| --- | --- | --- | --- |
| `kernel` / `csikernel` | NuttX ESP32-S3 QEMU numerical/ABI witness | Recording stubs | None; synthetic inputs |
| `radio` / `csi_radio` | Explicit CSI capture and IP traffic | Real, only with `--force-baseline` on a spare | Device trial failed; unqualified |

`pins.json` selects NuttX 13.1.0, esp-hal-3rdparty `b90b1837…` (IDF 6.1 headers),
and the **6.0 ESP32-S3** variants of `esp_csi_gain_ctrl` 0.1.5 and
`esp_radar_motion_dec` 0.1.1. Complete package and archive SHA256s are mandatory.
The packages' automatic CMake selector has no 6.1 directory; this witness
explicitly selects 6.0 for compatibility assessment. No dependency pin changes.
Vendor binaries are not checked in. Keep each staged Apache-2.0 LICENSE and
build/patch receipts with any distributed test image.

## Prepare tools and packages

Initialize `external/nuttx` and `external/nuttx-apps`. Use the existing
[NuttX std build environment](../../docs/nuttx-std.md), including GCC, Python
3.12, Kconfig tools, esptool and genromfs. On Linux x86-64,
`bash tools/install-qemu-tools.sh` provides the pinned Espressif tools; source
`target/qemu-tools/environment.sh`. macOS uses the existing explicit native ESP
setup (`NXRS_ESP32S3_NATIVE_TOOLS=1`, `NXRS_QEMU_TOOLS_DIR=...`). The host's
ordinary upstream QEMU does not supply the ESP32-S3 machine.

Download the two exact registry packages listed in `pins.json` into a private
scratch directory. Pass their ZIP paths explicitly; the builder verifies them
before extracting only the selected archives, public headers and licenses.
`--sdk-cache` optionally supplies a clean independent checkout at the exact
SDK pin. Its contents are copied and build objects cleaned in the build copy.
A cache with source changes is rejected; do not clean/reset the camera cache.
The existing tracked upstream patches apply only to isolated build trees.

## Reproduce kernel runtime checks without devices

```sh
python3 tests/csi-radio/build.py kernel \
  --out target/csi-radio-kernel \
  --gain-package /private/scratch/esp_csi_gain_ctrl-0.1.5.zip \
  --motion-package /private/scratch/esp_radar_motion_dec-0.1.1.zip
python3 tests/csi-radio/qemu.py --build target/csi-radio-kernel \
  --qemu /path/to/pinned-espressif-qemu/bin/qemu-system-xtensa
```

Use a fresh `target/csi-radio-NAME` for each kernel build. The runner requires a
matching source/image receipt and the pinned Espressif QEMU 9.0.0_20240606. It
instantiates two hardware cores for ROM loading, with NuttX SMP disabled, no
network and snapshot flash. Only its own emulator processes are terminated.
It runs three fresh positive boots (818 assertions each) and one deliberate
failure. Failure, missing/duplicate results, wrong NSH status, incomplete
allocation cleanup and incorrect host numerical comparisons reject the run.
The output JSON/logs stay under the ignored build directory.

The C numerical witness is SDK test code under `tests/`; it is not a C HAL or a
replacement for Rust resource management. Allocation hooks retain real NuttX
malloc/free and add guards/failure injection. PHY functions are recording stubs,
while logging, libc/libm and numerical algorithm machine code are real target
code. This does not establish real RF metadata, physical timing or presence
accuracy. It also does not run the vendor's FreeRTOS sensing FSM.

## Build the spare-board image now

```sh
python3 tests/csi-radio/build.py radio \
  --out target/csi-radio-device \
  --gain-package /private/scratch/esp_csi_gain_ctrl-0.1.5.zip \
  --motion-package /private/scratch/esp_radar_motion_dec-0.1.1.zip
```

This uses the existing `esp32s3-wifi` board profile, including its USB-serial
console and octal PSRAM assumptions. Check these against the **spare board**
before flashing; it is not an interchangeable camera deployment image. The
ordinary Rust std builder and its exact compiled-libc ABI gate are required.
An older cached SDK missing the NuttX filesystem fixes must not bypass that gate.

Only this test image links `--wrap=esp_wifi_init` to set `csi_enable=1` while
retaining the real NuttX adapter and OS callbacks. It does not reinitialize Wi-Fi
from the application. No production profile or HAL is modified. The radio image
uses the real gain kernel; the motion kernel has separate QEMU qualification,
and the radio fixture does not claim to implement the full presence pipeline.

Radio test images explicitly request `LINE_MAX=256` and
`NSH_MAXARGUMENTS=12` through the ordinary builder's checked integer overrides.
The base profile's 64-byte shell line cannot carry the maintenance command:
truncation can change the destination port and discard optional flags. The pinned
USB-serial driver also has a fixed 64-byte receive ring; the host sends command
and credential frames in paced 16-byte chunks. Before credentials are sent,
`CSI_TEST_OPTIONS` must confirm the exact requested mode, raw/force flags,
bound port and TCP selection. A missing, duplicated or altered acknowledgment rejects the run.
These are test-image/runner changes; production profiles are unchanged.

## Transport controls

Use a separate output to build a normal-initialization control:

```sh
python3 tests/csi-radio/build.py radio --no-csi-init \
  --out target/csi-radio-transport-control \
  --gain-package /private/scratch/esp_csi_gain_ctrl-0.1.5.zip \
  --motion-package /private/scratch/esp_radar_motion_dec-0.1.1.zip
```

With an already-flashed, explicitly selected device, add `--transport-only` to
the host run command and omit `--capture` and `--force-baseline`. This uses the
same bounded UDP packet/echo path, but does not configure CSI, register a CSI
callback, enable capture, or execute gain/motion kernels. It requires exact
echoes, successful NSH status, explicit disconnect and worker join. Its separate
`CSI_TRANSPORT_RESULT`/`CSI_TRANSPORT_PASS` cannot qualify CSI capture.

Repeat transport-only on the ordinary CSI-initialization-enabled radio image
before testing capture. Receipts record whether initialization was enabled;
the target acknowledgment confirms whether capture APIs are requested. A
first-send duration and bounded UDP-timeout counters distinguish transport
startup from RF freshness errors. Full CSI acceptance criteria remain unchanged.

`--tcp` selects a separate reliable-traffic control. It uses the same 16-byte
records, 100 ms scheduling, bounded outstanding window and two-second echo/RF
deadlines. Partial stream reads/writes retain at most one record per direction;
each poll does bounded I/O. Premature EOF, malformed replies and missing echoes
still fail. Host and target must agree on the protocol before credentials, and
result validation requires exactly one matching protocol marker. TCP success
cannot qualify the UDP baseline or erase its retained failures.

The test builder explicitly enables `NET_TCP`, `NET_TCPPROTO_OPTIONS` and
`NET_TCP_WRITE_BUFFERS`. The pinned unbuffered sender can wait for acknowledgments
despite nonblocking mode, so buffered writes are required for this control.
Resources are bounded to a 4096-byte send budget, four static write-buffer chains
and no dynamic chains. Checked configuration/source hashes are retained in the
receipt. The base Wi-Fi profile and production profiles are unchanged. After
CSI retirement, TCP half-closes and waits at most two seconds for peer EOF while
polling station health; only then may station teardown complete. Missing peer
closure evidence rejects a pass. Successful capture also requires consistent
callback/progress counters and the full requested duration.

## Physical test when a spare is available

Use a spare by default. Verify spare-board identity,
console, chip revision, flash/PSRAM and image hash, and back up its original
firmware before an explicitly authorized manual flash. Use a separate test AP
on a non-overlapping channel where possible; do not reconfigure the camera AP.
A matching camera-equipped spare is needed for later camera coexistence tests.

An explicitly authorized maintenance interruption of the existing camera is a
separate option: first save its complete flash and exact supervisor restart
command privately, verify the board and memory configuration, and stop the camera
owner cleanly. Use `--ack-camera-maintenance` instead of `--ack-spare-board` for
capture only. This mode rejects gain forcing both on the host and in firmware.
Restore and verify the original firmware and real stream immediately afterward,
including after a failed test. It does not qualify simultaneous camera/CSI use.

With the test image already flashed, an idle NSH and one exclusive serial owner,
put the WPA2 passphrase in an owned mode-600 file outside the repository. Install
pyserial in the existing test venv. The following is a **real hardware command**;
it is never invoked by build, host tests or CI:

```sh
python3 tests/csi-radio/run.py --ack-spare-board \
  --port /dev/cu.EXPLICIT_SPARE_PORT --host-ip TEST_HOST_IPV4 \
  --bssid TEST_AP_BSSID --ssid TEST_SSID \
  --password-file /private/scratch/test-wifi-password --seconds 30 \
  --capture /private/scratch/new-csi-capture.log
```

Use `--open-network` only for an intentionally open test AP. Credentials use the
existing raw/no-echo, length-delimited reader; no password appears in commands,
firmware, logs or build settings. The runner does no device discovery, reset or
flash, refuses an occupied exclusive port, and starts only from an idle NSH.
Opening a serial adapter is hardware access; verify the selected device first.
Capture files are new, mode 600 and outside this checkout. Only strict sampled
CSI records are saved, never arbitrary console output. Protect AP identifiers
and RF captures as local runtime data; do not commit them.

`--bssid associated` reads the connected AP from the native SDK after the normal
station owner has associated with the supplied SSID. Rust validates its unicast
identity and fixes that one peer before CSI admission. It does not enable
promiscuous reception or follow arbitrary peers. An explicit BSSID still selects
only that peer, as above.

Main and the existing Wi-Fi worker own the test. The callback performs fixed
metadata translation and bounded copy, with no allocation, logging, gain
forcing or waiting. Rust uses an eight-frame `heapless` 0.8.0 SPSC ring;
the SDK producer and main consumer have separate ownership locks. The producer
never takes the consumer's lock, and its `try_lock` rejects unexpected concurrent
SDK callbacks. The pinned ring's enqueue/dequeue each use bounded atomic loads,
one item transfer and a release store, with no CAS or retry loop. A consumer
preempted while copying cannot hold a lock needed by the producer. Ring storage
is allocated once before SDK admission and retained for process lifetime, even
after failed teardown. Main reads producer statistics only after SDK retirement
and the in-flight barrier. Regression tests pause each side while driving the
other, as well as checking overflow, stale epochs and sticky failures.

Contention, malformed packets, overflow, stale/reversed RF time, channel changes
and two seconds without accepted RF progress fail the trial. RF microsecond
wrap is handled explicitly. Each queued frame also carries callback-arrival
monotonic time; draining an old queue cannot make stale input fresh. One AP BSSID is selected before admission.

The owner requests a small exact echo every 100 ms (UDP baseline or explicit TCP control), continuously drains the
existing station owner and computes gain compensation outside the Wi-Fi task.
Raw export is intentionally sampled at at most 10 Hz, separately counted from
queue loss. It retains the original control bytes and CSI, including the
`first_word_invalid` marker; consumers must discard the invalid first four bytes
when that marker is set. No classifier consumes raw samples here.

Stop closes admission, restores automatic gain **if forcing was attempted**,
disables CSI, unregisters its callback and waits boundedly for admitted calls.
Callback storage has static lifetime, so late SDK callbacks never dereference
freed context; epochs isolate later sessions. This is not a claim that the SDK
provides a documented callback-quiescence barrier. Any uncertain cleanup is a
sticky failure; the same image must not silently reopen capture. Station cleanup
then explicitly disconnects and joins the existing Wi-Fi worker. Abort/reset
cannot execute Rust cleanup; gain forcing is confined to an explicit spare-board
trial, and recovery must verify RF state rather than assuming Drop ran.

A capture pass requires at least 50 fresh frames, valid compensation, at least
ten exact echoes for the selected protocol verified by both host and target, no queue loss, confirmed
cleanup and zero NSH status. It proves that **these capture/transport APIs ran**.
It does not prove the meaning of private gain bytes or detection accuracy.
A failing trial remains failed even if cleanup or a later trial succeeds.

## Pending acceptance gates — keep the PR draft

### Observed device blocker, 2026-10-22

Initial capture attempts were unqualified for their requested configuration:
the shell line limit and USB receive ring could truncate the destination port
and optional flags. Their [original evidence](results/esp32s3-radio-2026-10-22.json)
is retained; it does not establish a CSI binary incompatibility.

After the console fixes and target acknowledgment, both 30-second UDP controls
passed: 294 exact echoes with normal initialization and 283 with CSI support
initialized. Both confirmed disconnect, worker join and zero NSH status without
configuring capture. A subsequent capture received 98 real exported CSI records
(128-byte CSI, 48-byte native RX control, channel 6), spanning 15.043 seconds of
source timestamps. It then failed the two-second accepted-RF-progress deadline;
UDP remained active, with 127 echoed requests before failure. There was no
successful complete capture result. Callback totals and compensation counts are
not reported by that failed trial. Continuous capture had not yet been qualified.

Follow-up [callback diagnostics](results/esp32s3-callback-diagnostics-2026-10-22.json)
observed 299 matching 802.11n callbacks, zero callback drops, an unchanged AP and
250 compensation calls before another RF deadline failure. A UDP-only control
also lost a request after 262 echoes. Power saving was already disabled in the
pinned configuration; no CSI filters or radio power settings were changed.

The old single-outstanding-request fixture can suppress new traffic after one
lost echo, then report an RF timeout because its own stimulation stopped. The
fixture now uses a fixed 32-entry echo window, sending at most once per 100 ms
independently of pending replies. Every echo still has a two-second deadline;
missing, duplicate or malformed replies still fail, and a PASS still requires
every request to be echoed. RF freshness, zero queue/callback loss and cleanup
requirements are unchanged. This separates stimulus starvation from a CSI
delivery stall without promoting failed trials.

The first bounded-flight run exposed one callback drop while the consumer held
the old queue mutex and correctly failed its zero-loss gate. The SPSC handoff
removes that shared producer/consumer critical section; unexpected producer
contention and full queues still fail rather than silently dropping evidence.

With both corrections, a requested 60-second run reached 208 frames and 159
finite positive gain-compensation calls in 22.070 seconds, with zero callback
drops. It failed on an overdue UDP echo (sequence 185); the most recent CSI
callback was only 50 ms old, with the same selected AP. The old stimulus coupling
can therefore explain an apparent RF stall after a missing echo. This is not a
60-second PASS: 204 requests had been sent and only 202 echoed at rejection,
including one documented expired request and another outstanding request.

### Buffered TCP continuity, 2026-10-22

The separate [TCP control evidence](results/esp32s3-tcp-controls-2026-10-22.json)
retains an initial build with TCP disabled and a deliberately cancelled
unbuffered attempt. The latter completed 276 transport echoes, but its capture
was cancelled after source inspection showed that the unbuffered sender can
wait for ACKs despite nonblocking mode. Neither attempt qualifies CSI capture.

After enabling and checking bounded native write buffers, the 30-second
transport-only control passed all 300 echoes and confirmed peer EOF, disconnect,
worker join and zero NSH status. Two subsequent captures passed in the same boot:

| Requested duration | Accepted CSI frames | Valid gain-compensation calls | Exact echoes | Callback drops |
| --- | ---: | ---: | ---: | ---: |
| 60 seconds | 1084 | 1035 | 561 / 561 | 0 |
| 120 seconds | 2118 | 2069 | 1109 / 1109 | 0 |

The native observation windows were 60.000 and 120.040 seconds. Both confirmed
peer EOF, CSI retirement, station disconnect, worker join and zero NSH status.
All observed callbacks matched the selected AP and reported HT. The final RX
metadata reported 20 MHz, channel 6 and 128-byte CSI; sampled raw exports also
retained 128-byte CSI on channel 6. These are bounded TCP-stimulated capture and same-boot
reacquisition passes, not UDP, adverse-network, independent-boot, coexistence or
presence-accuracy qualification. The earlier UDP failures remain failed.

All three TCP maintenance windows restored the complete original firmware with
hash verification and decoded three fresh real frames through the existing
WebRTC gateway. Gain forcing and CSI/radio settings were unchanged. The latest
receipt hashes the 14 fixture/build/runner/profile inputs and resolved config;
its native image passed all 117 libc ABI checks. Raw samples and backups remain
private, and no presence decisions were produced.

Private gain byte semantics, real gain forcing, reference-stack comparison, coexistence and presence
accuracy remain unqualified. Do not relax freshness/overload/echo criteria to
obtain a pass.

The complete original flash was restored with hash verification, and three
fresh 320×240 frames were decoded through the existing WebRTC gateway after both
successful control windows (including the failed capture). Gain forcing was never
requested. The live camera is back on its original firmware. Sanitized receipts
and outcomes are in [the control evidence](results/esp32s3-transport-controls-2026-10-22.json).
Credentials, full flash backups and raw RF records remain private and excluded
from Git. Every follow-up maintenance window likewise restored the original
firmware and decoded three real frames. Long-duration/restart and adverse-network
CSI qualification, UDP reliability and full presence qualification remain pending.

- [ ] Capture-only trial above, repeated from fresh boots; record exact image,
  chip, AP/channel, SDK/Wi-Fi/PHY revisions and private capture hashes.
- [ ] Compare the pinned stack with a supported IDF 6.0 reference on the spare
  under controlled signal levels. Compare distributions/behavior, not bytewise
  equality between independent over-the-air receptions.
- [ ] Resolve private RX metadata bytes 22/23 (FFT/AGC gain). Seek Espressif
  confirmation for the exact chip and radio revision. Plausible ranges or
  compensation alone do not prove this undocumented ABI.
- [ ] Only after that gate, repeat the hardware command with
  `--force-baseline`. This deliberately changes RF gain and may lose packets.
  Confirm the actual gain response and return to automatic gain in captured
  metadata; an SDK success return alone is insufficient. Restrict the trial to
  the spare and do not treat it as the capture-only path.
- [ ] Test AP/traffic loss and recovery on the dedicated test network, invalid
  calibration and failed stop. Never induce loss by disabling the camera AP.
- [ ] Run camera-only / CSI capture / CSI plus processing on a matching spare
  camera, using an independent test gateway. Record decoded frame gaps,
  reconnects, throughput, CPU/heap/stack high-water, RF freshness and queue loss.
  This capture image does not implement that combined camera workload yet.
- [ ] Separately measure empty-room, moving/stationary-person and nuisance
  cases with ground-truth labels. Full preprocessing, calibration and presence
  accuracy are not supplied by this capture witness or the HMS diagnostic bridge.

Bounded TCP capture has passed; broader hardware acceptance remains pending. No unattended deployment, production CSI binding,
notification/recording behavior change or merge is implied by host/QEMU success.

## Host regressions

```sh
cargo test --locked -p nxrs-csi-radio-test --lib
cargo clippy --locked -p nxrs-csi-radio-test --all-targets -- -D warnings
python3 -m unittest discover -s tests/host -p test_csi_radio.py -v
```

References: [Espressif CSI configuration](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/wifi-driver/wifi-vendor-features.html#wi-fi-channel-state-information-configure),
[sniffer throughput effects](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/wifi-driver/wifi-modes.html#wi-fi-sniffer-mode),
[architecture requirements](../../docs/hal-platform-architecture.md).
