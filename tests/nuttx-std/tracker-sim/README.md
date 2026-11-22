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
The modem model answers AT commands, negotiates PPP, drops one IPCP reply to
check retransmission, returns UDP payloads and checks two complete link
lifetimes. The GNSS model verifies the requested configuration and its
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

Each scenario uses UART2 for one external peer. The modem/PPP and full
dashboard/BLE runs provide separate coverage. The simulator models the panel,
touch, controller and flash. Physical RF, native phone behavior and persistence
across power cycles require hardware.
