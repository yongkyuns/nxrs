# BLE hardware qualification

This fixture uses `ble::Controller` and the NuttX scanner. BLE is one crate
(`nxrs-ble`, imported as `ble`): its contract is in `hal/ble/src/api.rs`, its
private std worker is in `hal/ble/src/worker.rs`, the NuttX adapter is under
`hal/ble/src/providers/nuttx/`, and its
target C helper is in `hal/ble/ffi/nuttx/`. The platform builder adds that
helper automatically. The fixture scans for
eight seconds, counts distinct peers, stops the scan, joins the worker and
returns to NSH. It does **not** qualify connections, GATT, pairing, notifications
or Wi-Fi/BLE coexistence. The public contract contains scan operations only;
there is no GATT value buffer in scan queue slots. ESP32-S3 supports LE, not
Classic BT.

The common controller is allocation-free and owns no thread or queue. This
fixture chooses an eight-event bounded queue and one event-loop wait point;
the native provider adds one 8 KiB worker. Native NuttX stack tasks are separate.
The worker holds its process-global radio lease until native stop/close proves
quiescence; `finish()` explicitly joins the thread. It retains
events when the queue is full, and retains terminal errors even if their wakeup
was not delivered. Discovery is best-effort: native reports and the session's
32-peer registry are bounded. Registry exhaustion emits `DiscoveryLimited`
once per scan and omits new peers, while keeping the scanner usable and
previous IDs stable. A new provider session is needed to reset that registry.
If native stop fails, a live owner can retry teardown; an abandoned worker uses
capped backoff without freeing active callbacks. Permanent failure retains
the resource rather than claiming successful cleanup.

The stable-release smoke checks below exercise this scan API/lifecycle refactor.
Older recovery measurements remain historical; host tests alone do not qualify radios.

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
For repeated-start qualification, run ten scans without reboot, then
`ble_test fail` and another normal scan. The injected failure happens **after**
the eight-second scan and native stop; an earlier native failure is not a
passing injection test. Repeat the sequence after a fresh boot.

Local stable-release checks (NuttX 13.1.0, 2026-10-11):

| Board | Observed result |
| --- | --- |
| ESP32-S3 | With patch 0017, four scans after a fresh boot each found 24 peers and returned zero with the worker joined. A full eight-second injected failure returned one, followed by a successful scan. |
| Pico 2 W | With patch 0017, 22 scans across two fresh boots found 23–27 peers and returned zero with the worker joined. Each boot included a full eight-second injected failure (status one) followed by a successful scan. |

Pico passed 100 exact console echo round trips across these two boots;
ESP passed 50. Test commands fit their configured shell limits (64 bytes on
ESP, 80 on Pico). Late Pico
`Results received while not scanning` diagnostics and false scan-start `Io`
errors before the fix were traced to command-buffer reuse in the generic
NuttX HCI host, not a rejected controller command. Patch 0017 fixes ownership
and expired-waiter cleanup; it adds no application retry. Neither diagnostic
recurred in the final Pico sequence. These short checks do not establish
long-term reliability, physical transport-fault recovery or USB interoperability.
The ESP adapter uses NuttX's `bnep0`, protected by a host C-bridge test.
The tested ESP `nuttx.bin` is 497,248 bytes, SHA-256
`1fc219e725dfd97d14e2ba7aa47e80dffcb2cb33aa688ab7faa94d9a4359b878`;
the patched Pico image is 469,912 bytes (168 bytes more), SHA-256
`81c55174d38294b37fbd8f4ae6d398fbcc4f017b36a93d46ceab7844e356fdb1`.
Both pass all 63 target C/Rust ABI witnesses. Earlier ESP simple-boot images
emitted a ROM SHA-256 mismatch warning despite verified writes; these scan
checks do not qualify boot integrity.

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
The pre-upgrade 462,920-byte recovery image passed all 47 C/Rust ABI witnesses
(SHA-256 `a6c20d0ce218694f4bcc1891d303237d56114ee952fa3b8d16a1a785ddcd987c`).
It was flashed and verified on 2026-10-11. The new
[USB recovery hook](../wifi/README.md#usb-recovery) adds 160 bytes to the previous
image and uses one embedded timer, not another thread; reset passed at an idle
prompt and during a verified foreground `sleep 30`, without a manual BOOTSEL
press. This is boot/reset
qualification, **not Bluetooth qualification**. Current scan observations are
above; repeated-start smoke checks now pass, while long-term reliability,
coexistence and low-power behavior remain unqualified. Back up flash, verify
the RP2350 identity,
then load the ELF with `picotool load -v -x IMAGE -t elf`. Use `ble_test` and the
same positive/negative gates above once NSH responds.

macOS/CoreBluetooth and Linux/BlueZ providers remain planned, not implemented
or qualified by these host tests.
