# BLE hardware qualification

This fixture uses `ble::Controller` and the NuttX scanner. BLE is one crate
(`nxrs-ble`, imported as `ble`): its contract is in `hal/ble/src/api.rs`, its
private NuttX implementation is under `hal/ble/src/providers/nuttx/`, and its
target C helper is in `hal/ble/ffi/nuttx/`. The platform builder adds that
helper automatically. The fixture scans for
eight seconds, counts distinct peers, stops the scan, joins the worker and
returns to NSH. It does **not** qualify connections, GATT, pairing, notifications
or Wi-Fi/BLE coexistence. The provider advertises `scan=true`, `central=false`
and rejects unsupported operations. ESP32-S3 supports LE, not Classic BT.

The common controller is allocation-free and owns no thread or queue. This
fixture chooses an eight-event bounded queue and one event-loop wait point;
the native provider adds one 8 KiB worker. Native NuttX stack tasks are separate.
The provider holds its process-global radio lease until stop and join, retains
events when the queue is full, and retains terminal errors even if their wakeup
was not delivered. Discovery is best-effort: native reports and the session's
32-peer registry are bounded. Registry exhaustion fails rather than reusing IDs.

Use the pinned [NuttX std tools](../../../docs/nuttx-std.md). On macOS, select
the existing native ESP tools with `NXRS_ESP32S3_NATIVE_TOOLS=1` and
`NXRS_QEMU_TOOLS_DIR`; a Linux compiler bundle cannot run on macOS.

```sh
NXRS_NUTTX_BUILD_JOBS=2 bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/ble/Cargo.toml \
  --app-package nxrs-std-ble --bin nxrs-std-ble --command ble_test \
  --priority 100 --stack-size 8192 --platform esp32s3-ble \
  --out target/ble-scan-device --size-optimized \
  --trace-only-backtrace --panic-immediate-abort
```

Back up the connected board before flashing. Flash the unpadded `nuttx.bin` at
offset zero. At NSH, run `ble_test`; require `NXRS_BLE_SCAN_PASS`,
`shutdown=joined`, a clean prompt and `echo $?` equal to zero. Repeat across
fresh boots and without reboot. `ble_test fail` must emit no PASS and return
nonzero; a print followed by a crash is never success. No advertisers in range
means this positive scan test is inconclusive, not proof of a broken controller.

Local ESP32-S3 qualification (2026-10-10): two scans in the same boot and one
after a hardware reset each reported 20 peers, joined the worker and returned
NSH status zero. The injected-failure run returned status one with no PASS.
The initial `hci0` binding failed; the corrected adapter uses NuttX's `bnep0`,
protected by a host C-bridge regression test. The tested `nuttx.bin` is 490,104
bytes, SHA-256 `9ae5bf88ec49cd5ba7143312f90bbb8f64fb78870f77264cb837423afc510ce6`.
This simple-boot image still emits a ROM SHA-256 mismatch warning despite
esptool verifying the flash write; radio passes do not qualify boot integrity.

Host tests exercise lifecycle, cancellation, bounded delivery, stale IDs,
timeouts and UUID decoding. They do not test an actual radio:

```sh
cargo +1.90.0 test --locked --offline -p nxrs-ble
```

## Pico 2 W: experimental transport

Use the same build command with `--platform pico2w-ble` and a separate output
directory. Native macOS ARM builds use `NXRS_NUTTX_NATIVE_TOOLS=1`, the pinned
ARM nightly, GNU ARM tools, Kconfiglib and GNU sed described in the Wi-Fi fixture.
The builder stages the hash-pinned Wi-Fi image and a 6,164-byte Bluetooth
controller patch image only for this profile. Retain their license, provenance
and patch ledgers with the build; no Pico SDK runtime is linked into nxrs.

The [experimental dependency patch](../../../docs/upstream-patchsets.md) uses
the existing radio daemon, bounded HCI ring processing and a shared-chip power
lease. Host tests exercise protocol framing, concurrent senders, backplane
window locking, stale sends, failed stops and idle-shutdown serialization.
The local 462,920-byte image passes all 47 C/Rust ABI witnesses
(SHA-256 `a6c20d0ce218694f4bcc1891d303237d56114ee952fa3b8d16a1a785ddcd987c`).
It was flashed and verified on 2026-10-11. The new
[USB recovery hook](../wifi/README.md#usb-recovery) adds 160 bytes to the previous
image and uses one embedded timer, not another thread; reset passed at an idle
prompt and during a verified foreground `sleep 30`, without a manual BOOTSEL
press. This is boot/reset
qualification, **not Bluetooth qualification**. Scanning, coexistence and
low-power behavior remain unqualified. Back up flash, verify the RP2350 identity,
then load the ELF with `picotool load -v -x IMAGE -t elf`. Use `ble_test` and the
same positive/negative gates above once NSH responds.

macOS/CoreBluetooth and Linux/BlueZ providers remain planned, not implemented
or qualified by these host tests.
