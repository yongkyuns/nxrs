# Tracker simulation on NuttX

The qualification image runs a single-core ESP32-S3 NuttX kernel in Espressif
QEMU. It uses the production Rust device composition, HALs and `std` runtime.
UART and IP traffic pass through NuttX’s drivers. The two UART peers run on the
Mac and the remaining test devices attach at NuttX’s driver lower halves.

| Model | Driver path exercised |
| --- | --- |
| SIM7080G | ESP32-S3 UART2, SIMCom AT commands, PPP, NuttX TUN, IPv4/UDP |
| NEO-M9N | ESP32-S3 UART1, UBX configuration, fix decoding and rate restoration |
| Two BMI270s and AXP2101 | NuttX I²C character driver and separate device addresses |
| ILI9342C | NuttX SPI character driver and RGB565 write |
| FT6336 | NuttX FT5x06 I²C polling and touch input drivers |
| Settings flash | NuttX RAM MTD with flash semantics and its NVS driver |

Run from the repository root with the normal Nxrs ESP32-S3 tool environment.
Build Espressif QEMU's `xtensa-softmmu` target at revision
`093b0c343e5457f16ead57d89cf2a75361cf3acf` for the UART2 character backend.
Apply the QEMU patches before building. They model core 1 RUNSTALL and
prevent UART receive timers from firing on an empty FIFO.
[Espressif QEMU setup](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-guides/tools/qemu.html)

```sh
git -C /path/to/espressif-qemu apply \
  "$PWD"/upstream/qemu/patches/*.patch
```

## Modem and device qualification

```sh
cargo nxrs build --manifest-path tests/nuttx-std/tracker-sim/Cargo.toml \
  --profile esp32s3-qemu-tracker \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --out target/tracker-sim
python3 tests/nuttx-std/tracker-sim/run.py target/tracker-sim/nuttx/nuttx.merged.bin
```

Put the built `qemu-system-xtensa` on `PATH`, or pass
`--qemu /path/to/build/qemu-system-xtensa` and
`--qemu-data /path/to/source/pc-bios` for an uninstalled source build.

The runner uses private local UART sockets and disables QEMU networking.
The modem refuses the first dial and drops one IPCP reply on the next attempt
to check retransmission. It returns a UDP payload, then terminates the
established PPP link. The runner verifies that joining the failed session
preserves the link-loss error and that a new session transfers UDP data and
stops cleanly in the same boot. The GNSS model verifies the requested configuration and its
restoration. It sends the test fix when the qualification program confirms
that the GNSS event stream is open. The device checks read both IMUs, verify
battery state, draw a pixel, receive a touch and store a record through NVS.

Single-core NuttX sets `RUNSTALL` in `SYSTEM_CORE_1_CONTROL_0` at boot.
The QEMU patch models that register bit and holds the second core out of
its boot ROM polling loop. With `-icount shift=7,sleep=off`, QEMU advances
straight to the next scheduled interrupt when both cores are idle. The
profile uses periodic NuttX ticks. [QEMU clock implementation](https://github.com/espressif/qemu/blob/093b0c343e5457f16ead57d89cf2a75361cf3acf/accel/tcg/icount-common.c#L291)

The runner times the guest's 10-second Rust sleep and the full qualification
against host monotonic time. The full timing starts at the NuttX shell command
and ends at its reported exit status. `--wall-clock` runs the same checks with
QEMU's wall clock. Physical scheduling latency requires a hardware measurement.

On Apple Silicon, the same firmware and `--shift 7` passed on three fresh
boots with the original QEMU build and 20 with the patched build.

| Host elapsed time, median | Original QEMU | Patched QEMU |
| --- | ---: | ---: |
| 10-second guest sleep | 2.731 s | 0.119 s |
| Full qualification | 5.131 s | 0.304 s |

The patched wall-clock control also passed. The timings above used the
RUNSTALL patch before the UART receive-timeout patch was added.

## Headless one-IMU tracker

The acquisition fixture registers one modeled BMI270 on NuttX's I²C driver
and exposes NSH over UART0. It exercises the same observation service used in
the tracker. The verifier checks live readings, a clean exit and reacquisition
in the same boot.

```sh
cargo nxrs build --manifest-path tests/nuttx-std/tracker-sim/logging/Cargo.toml \
  --profile esp32s3-qemu-tracker-headless \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --target-c-source tests/nuttx-std/tracker-sim/native_init.c \
  --out target/tracker-headless
node tests/nuttx-std/tracker-sim/headless.mjs \
  target/tracker-headless/nuttx/nuttx.merged.bin target/tracker-headless/report.json
```

### Route system logs to a rotating file

The logging qualification profile extends the one-IMU profile. Its boot
fixture mounts TMPFS and starts the Rust archive as a separate NuttX task
before starting NSH. The task installs the Rust `log` adapter for NuttX
`syslog` and drains RAMLOG at `/dev/kmsg` using a blocking file read. The same
headless tracker runs as a separate NSH invocation.

```sh
cargo nxrs build --manifest-path tests/nuttx-std/tracker-sim/logging/Cargo.toml \
  --profile esp32s3-qemu-tracker-logging \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --target-c-source tests/nuttx-std/tracker-sim/logging/init.c \
  --out target/tracker-logging
node tests/nuttx-std/tracker-sim/headless.mjs \
  target/tracker-logging/nuttx/nuttx.merged.bin target/tracker-logging/report.json
```

The archive keeps `/logs/system.log` and one rotated `system.log.1`, up to 512
bytes each. Rotation preserves byte order and can split a log line. TMPFS is
volatile, and the kernel RAMLOG can overwrite unread data during a burst.
The verifier checks the boot SYSLOG marker before the tracker runs, the
modeled driver’s SYSLOG markers, the observation service's Rust log records
during two tracker runs, and retention after repeated rotations. Tracker
measurements use the typed snapshot stream. [Desktop and browser qualification](../../logging/README.md)

## Full dashboard and BLE/TLS

Build the production `motion-tracker` app for the simulator. Python 3.13
or newer must expose OpenSSL's PSK client callback in `ssl`.

```sh
python3 -m venv target/tracker-peer
target/tracker-peer/bin/python -m pip install bumble==0.0.236 Pillow==13.0.0 zxing-cpp==3.0.0
cargo nxrs build --app motion-tracker --profile esp32s3-qemu-tracker-dashboard \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --target-c-source tests/nuttx-std/tracker-sim/dashboard_init.c \
  --out target/tracker-dashboard-sim
target/tracker-peer/bin/python tests/nuttx-std/tracker-sim/dashboard.py \
  target/tracker-dashboard-sim/nuttx/nuttx.merged.bin
```

The peer sends touch contacts through the emulated FT6336, reads the SPI
panel pixels and decodes the actual pairing QR code. Bumble supplies a
virtual controller on UART2/H4 and an independent central. NuttX owns the
guest Bluetooth host and GATT. OpenSSL acts as the TLS-PSK client and
verifies the decrypted tracker schema. Pairing keys stay in process memory.

The scenario checks tab selection, QR cancellation and rotation, rejection of
the old key, authenticated telemetry, a paused BLE reader, reconnect, rendering adjacent tiles sent over BLE across a simulated boundary, GNSS
freshness and recovery, battery state, and a clean app restart with preferences
retained. The dashboard uses `-icount shift=5,sleep=off`. Use
`--wall-clock --frames 42 --timeout 70` for a shorter real-time control.

The scenario passed on five fresh Apple Silicon QEMU boots. The 99-second guest
report window took a median of 5.22 seconds on the host, and the entire scenario
took a median of 5.66 seconds. The wall-clock control passed separately. The
`shift=7` stress run exceeded the application's IMU freshness budget during
panel rendering, so the full-dashboard scenario uses `shift=5`.

The standalone modem and dashboard/BLE scenarios each use UART2. The combined
scenario below uses a separate RAM UART for BLE. Physical RF, native phone
behavior and persistence across power cycles require hardware.

## Full tracker and TLS upload over PPP

Build the production app with the modem model on UART2. The collector uses
Python 3.10+ with TLS 1.3 and an ephemeral P-256 certificate generated by
`openssl`. A second QEMU process with user networking forwards the guest
collector port through `nc` to the loopback listener. It supplies peer TCP
without requiring a privileged host network interface.

```sh
cargo nxrs build --app motion-tracker --profile esp32s3-qemu-tracker-upload \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --target-c-source tests/nuttx-std/tracker-sim/dashboard_init.c \
  --out target/tracker-upload-sim
python3 tests/nuttx-std/tracker-sim/upload.py \
  target/tracker-upload-sim/nuttx/nuttx.merged.bin \
  --qemu /path/to/patched/qemu-system-xtensa --network-qemu qemu-system-xtensa --wall-clock
```

The runner rejects the first dial, closes the collector socket, terminates
PPP, then drops all PPP traffic on the open UART after recovery. The cellular
HAL sends an LCP Echo-Request every five seconds and reports link loss after
three unanswered probes. The runner measures detection in guest time and
recovery to the first fresh TLS upload in host time. USB and the collector
take independent snapshots, so the checks align them by acquisition time.
USB verifies advancing readings from both IMUs and GNSS through both outages.
The synthetic BMI270s fill their FIFOs at 100 Hz in guest monotonic time.
Repeated reads in one sample period leave the sample count unchanged.
The collector verifies fresh, bounded summaries at the one-second test cadence. A fresh boot
with a wrong pin must upload zero records. LCP echoes check the PPP peer.
The [physical bench](../tracker-hardware/README.md) measures carrier
connectivity beyond the modem.

The **ESP32-S3 QEMU** CI workflow builds the patched emulator with
`tools/build-tracker-qemu.sh` and runs the same command with `--wall-clock`.

## BLE telemetry during PPP loss and reconnect

This profile keeps the Stamp model on UART2 and connects the virtual BLE
controller through NuttX's RAM UART. The tracker runs both TLS connections
with its existing production code.

```sh
cargo nxrs build --app motion-tracker --profile esp32s3-qemu-tracker-coexist \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --target-c-source tests/nuttx-std/tracker-sim/dashboard_init.c \
  --out target/tracker-coexist-sim
target/tracker-peer/bin/python tests/nuttx-std/tracker-sim/coexist.py \
  target/tracker-coexist-sim/nuttx/nuttx.merged.bin \
  --qemu /path/to/patched/qemu-system-xtensa --network-qemu qemu-system-xtensa
```

The simulated phone scans the dashboard QR code, authenticates and receives
its first BLE record while a cellular TCP payload is held. It keeps that
authenticated BLE connection open through the rest of the test. After both links deliver live data, the Stamp terminates
PPP and refuses redial until the phone has received at least four fresh records
with both IMUs and GNSS live. The local collector must then receive fresh
records on a new TLS connection while the original BLE stream continues.

After TLS delivery resumes, the Stamp model withholds a data segment and its
retransmissions for at least 3.2 seconds measured by the independent USB
stream. The default wall-clock run requires BLE delivery during that wait,
rejects gaps above 2.5 seconds between BLE records, and checks that the
collector receives fresh data again. Use `--shift 3` for a faster continuity
check. Accelerated timings include the effect of QEMU's virtual clock on the
host-driven BLE controller.

The ESP32-S3 QEMU workflow runs `--export` against the notification service.
It adds CPU work to display SPI transfers while both IMUs, GNSS and cellular
TLS stay active. It paces the BLE ciphertext at 5.5 kB/s, with a 4.8 kB/s
interval matching the slower physical link trace. It withholds three bursts of
controller transmit credits and injects notification and acknowledgment loss.
During the 4.8 kB/s interval, it drops ten isolated notifications spaced by
5,500 new ciphertext bytes. Each drop consumes its scheduled link time and
controller completions still arrive. This covers a replay loop that can stall
export even with the link up. A receiver that discards data after a gap
also has to recover during the legacy export reconnect.
The test requires contiguous sequences, queue recovery, an advancing cellular
stream, and one UTC mapping for sensor records on BLE, USB and cellular TLS.
A legacy export reconnect uses the same pairing key.

The coexistence image also enables NuttX stack coloration, the stack monitor,
and a 256-byte scheduler guard. During `--export` and `--tiles`, the harness checks the
peak touched stack against the usable capacity for the tracker and all its
workers, including both IMU tasks. It warns below 20% free and fails below
15% free. The same workload runs with the hardware watchdog armed.
After warm-up, three restart, pairing, export and shutdown cycles must recover
the same free heap. The largest free block must stay at least 20 KiB and at
least as large as it was after warm-up. QEMU then touches the last word of the
console task's guard and requires the scheduler to assert.
On an interrupt-driven switch the assertion reuses the outgoing task's saved
registers to report the fault.

The application's firmware metadata reserves 16 KiB for the `motion_tracker`
entry/display task. This adds 1,536 bytes to its previous 14,848-byte reservation;
the communications and acquisition worker budgets are unchanged. At revision
`66c57797`, the [retained coexistence run](https://github.com/yongkyuns/nxrs/actions/runs/45913615465)
reported a 12,352-byte touched peak in all 46 samples of that task, with 14,328
usable bytes and only 1,976 bytes (13.8%) free. The observed requested-to-usable
difference is 520 bytes.
The same headroom gate had already failed in the [merged fixed-reader run](https://github.com/yongkyuns/nxrs/actions/runs/45894819103),
with 12,236 bytes used and 14.6% free.

Keeping the retained peak and observed 520-byte difference fixed, 16 KiB projects
15,864 usable bytes and 3,512 bytes (22.1%) free. These projected values are budget
arithmetic; the export and map workloads still measure actual usable capacity
and touched peaks. A host regression replays the old failure and checks that the
declared entry budget covers the retained peak at the existing 20% target. The
15% rejection floor, worker checks and native memory safeguards remain in force.

The later [export run at `3e841457`](https://github.com/yongkyuns/nxrs/actions/runs/45919882637)
measured 3,448 of 15,864 usable entry-task bytes free (21.7%). All tracked
workers passed the headroom gate. Three restart/export cycles recovered
251,528 B of free heap and a 113,352 B largest free block; the deliberate stack
guard corruption was detected. These are results for that revision's `--export`
workload. They do not replace the separate map measurement or qualify later
source changes. The extra 1,536 B is a requested runtime task reservation, not
an increase in the historical linked product's measured static SRAM.

For a five-minute soak, pass `--export --export-seconds 300 --timeout 440`.
The runner repeats the fault sequence and checks that the export catches up
after each minute. CI runs one sequence.

CoreS3 production qualification uses the physical display, both IMUs at
100 Hz, GNSS at 2 Hz and a cellular TLS collector while a physical iPhone
receives the stream. Compare source sequence ranges against the phone's
decoded counts. The firmware reports queue high-water and maximum time
between BLE service calls when the session ends. Check that the queue
drains after a stall and that the worst supported load leaves sustained
capacity for recovery. The 512-sample queue covers about 2.5 seconds
of complete blockage at the nominal source rate.

On the unbuffered TCP build, this control reproduced a 4.02-second BLE gap and
zero records during the hold. With bounded TCP writes enabled, three
wall-clock boots each delivered BLE records during the hold. The longest
receiver gap was 1.55 seconds and TLS telemetry resumed after release.

Run the same combined fixture with `--tiles` to test map delivery while
PPP is online. It verifies the cellular-capable profile refuses direct IP
map configuration and the PPP listener receives no map traffic. The BLE
client supplies both tiles during full-rate export. The test checks the
panel on both sides of the boundary and requires consecutive sample
sequences throughout the transfers.

### Failure registers and matching firmware

The separate `--tiles` run at `3e841457` failed before the first complete panel
frame. Its retained log contains 32,338 repeated
`xtensa_debug_handler: Unhandled debug cause 0x8` messages. The originating
fault remains unresolved: the old artifact omitted register state and the
matching coexistence firmware. A successful export run does not establish
that map startup passed.

On a scenario failure, [the dashboard runner](dashboard.py) now retains its QMP
connection, requests that the guest stop, and dumps registers from both emulated
cores before cleanup. The sidecar beside the console log, such as
`tiles.failure.json`, records the original exception, QMP version, image/ELF
paths, whether stopping succeeded, register text and partial diagnostic errors.
Inspect `DEBUGCAUSE`, saved exception PCs including `EPC6`, `EXCCAUSE` and
`EXCVADDR` against that run's ELF and link map. A BREAK can occur in an exception
vector, so the current handler PC alone need not identify the original fault.

The [ESP32-S3 CI artifact](../../../.github/workflows/nuttx-qemu.yml) retains
these sidecars with the upload/coexistence ELF, map, merged image, resolved
configuration/profile, binary hashes, ABI report and provenance. Use the pinned
tracker emulator above and the matching product files when reproducing a fault;
a standard-library qualification image is a different application. Diagnostics
are best effort: an unavailable monitor or artifact write adds context without
replacing the original failure. Successful runs keep their existing assertions,
and the five-second complete-frame deadline is unchanged. The new capture path
prepares diagnosis; it is not a fix or a passing native map result.

## Replay an iPhone recording

Share an NDJSON file from **Motion Tracker → Recordings**, then build the
native simulator and pass the file to its runner.

```sh
cargo nxrs build --app motion-tracker --profile nuttx-sim-tracker \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --target-c-source tests/nuttx-std/tracker-sim/native_init.c \
  --out target/nuttx-sim-tracker
python3 tests/nuttx-std/tracker-sim/native.py target/nuttx-sim-tracker/nuttx/nuttx \
  --replay /path/to/recording.ndjson --log target/nuttx-sim-tracker/replay.log
```

The runner validates the archive and stages it in a temporary fixture mount.
It starts playback when both BMI270 models are configured and the UART peer
has acknowledged GNSS configuration. IMU FIFO frames and GNSS fixes follow
the shared tracker reception timeline at 1× speed, including FIFO batches and
gaps. The console log contains the tracker’s normal USB reports. The runner
checks that both modeled FIFOs drained their recorded counts and that the UART
peer sent all fixes.

The archive begins with a version-1 `nxrs-motion-tracker` header and ends with
an explicit completion record. Sample rows preserve original sequences and
optional UTC. The native drivers assign new sequences and reception timestamps
during playback. IMU values are quantized to the profile’s BMI270 range.
The GNSS peer reconstructs position, speed and available UTC through UBX.
Samples with repeated timestamps retain their source order and share a due time.

The fixture accepts finalized recordings marked incomplete and preserves their
missing intervals. It rejects empty archives and requires a valid footer.
NuttX scheduling and device-read timing still affect when tasks observe samples.
