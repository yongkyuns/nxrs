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

Use the production app with its dashboard feature disabled. The qualification
profile registers one modeled BMI270 on NuttX's I²C driver and exposes NSH over
UART0. The headless JSON stream is version 2 with one IMU entry. The verifier
checks live readings, a successful exit, and reacquisition in the same boot.

```sh
cargo nxrs build --app motion-tracker --no-default-features \
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
the old key, authenticated telemetry, a paused BLE reader, reconnect, GNSS
freshness and recovery, battery state, and a clean app restart with preferences
retained. The dashboard uses `-icount shift=5,sleep=off`. Use
`--wall-clock --frames 22 --timeout 40` for a shorter real-time control.

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
  --qemu /path/to/patched/qemu-system-xtensa --network-qemu qemu-system-xtensa
```

The first dial is rejected. The following cases close the collector socket and
terminate PPP while IMU and GNSS reports continue. The collector compares its
decrypted records byte for byte with the app’s USB output and checks that a newer
live snapshot arrives after each disruption. A fresh boot with a wrong pin
exercises repeated TLS attempts and requires zero uploaded records. Use
`--wall-clock` to run both scenarios without clock acceleration.

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

The simulated phone scans the dashboard QR code and keeps one authenticated
BLE connection open. After both links deliver live data, the Stamp terminates
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

On the unbuffered TCP build, this control reproduced a 4.02-second BLE gap and
zero records during the hold. With bounded TCP writes enabled, three
wall-clock boots each delivered BLE records during the hold. The longest
receiver gap was 1.55 seconds and TLS telemetry resumed after release.
