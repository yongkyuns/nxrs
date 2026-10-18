# BLE hardware qualification

This fixture uses `ble::Controller` and the NuttX scanner. BLE is one crate
(`nxrs-ble`, imported as `ble`): its contract is in `hal/ble/src/api.rs`, its
private std worker is in `hal/ble/src/worker.rs`, the NuttX adapter is under
`hal/ble/src/providers/nuttx/`, and its
target C helper is in `hal/ble/ffi/nuttx/`. The platform builder adds that
helper automatically. The fixture scans for
eight seconds, counts distinct peers, stops the scan, joins the worker and
returns to NSH. It does **not** qualify connections, GATT, pairing, notifications
or Wi-Fi/BLE coexistence. This fixture exercises scan operations only;
there is no GATT value buffer in scan queue slots. ESP32-S3 supports LE, not
Classic BT.

The same scan fixture also builds for macOS with the explicit desktop profile:

~~~sh
NXRS_PLATFORM=macos cargo run --locked -p nxrs-std-ble --bin nxrs-std-ble
~~~

It never requests permission UI or changes Bluetooth power. A missing host-app
permission returns `PermissionDenied`, not a qualification pass. CoreBluetooth
uses opaque peer identities; it does not expose Bluetooth MAC addresses. The
ordinary `native` profile does not enable radios. macOS GATT uses the separate
client test below; scanning alone does not qualify it.
A local eight-second Mac scan discovered peers, stopped, and joined its
worker successfully. This qualifies that scan path, not connections or coexistence.

For Linux scanning, use `NXRS_PLATFORM=linux` with the same command. It uses
BlueZ and system GIO (`pkg-config`, `libglib2.0-dev` on Debian/Ubuntu), requires
an already powered adapter, and never pairs, clears device caches or changes
power. It releases only its own [discovery session](https://bluez.readthedocs.io/en/latest/adapter-api/).
Fresh BlueZ report signals identify candidates; local filtering is still needed
because BlueZ merges filters across clients. Cached device entries alone do not
count. `python3 -B tests/host/test_linux_radio_native.py` tests the actual bridge
with ASan/UBSan, native linking and the Rust owner against private D-Bus services
(`cc`, `dbus-daemon` and the pinned Rust toolchain are required). No system radio
is used. The same tests cover Linux GATT and all four portable client scenarios;
Linux RF hardware is not qualified.

For the separate native peripheral/host-central connection test, see
[GATT and simultaneous Wi-Fi traffic](../radio/README.md). Its results do not
qualify the portable GATT client described below.

`ble::gatt::open` is a separate client capability with bounded values,
callback-safe deferred completion and timeout/recovery handling. The scanner and
older native fixture do not qualify it. See the client test below and
[ownership and limits](../../../docs/hal-platform-architecture.md#ownership-and-shared-behavior).

For macOS, build the same scenario and set `PEER_PORT` to the other board's
NSH console. That board needs the matching radio peripheral firmware and an
idle shell; the runner starts each peripheral case itself.

~~~sh
NXRS_PLATFORM=macos cargo build --locked -p nxrs-std-ble --bin gatt-client
python3 tests/nuttx-std/ble/client.py --native-client target/debug/gatt-client \
  --peer-port "$PEER_PORT" --case legacy --case cancel --case values --case full
~~~

On Linux, select `NXRS_PLATFORM=linux` in the build command. GATT uses the same
scenario and API, with a private GIO executor for BlueZ IPC and the existing
owner wakeup. Its private-bus tests require exact ordered records and cleanup
from the real Rust client, not just fake C results. They are not hardware tests.
Use a peripheral exclusively owned by the application: BlueZ disconnects are
system-wide, not per-client leases. The adapter refuses an already-connected
peer, but cannot prevent another program from racing it. It never pairs or
changes host power, trust or device caches. See
[ownership and limits](../../../docs/hal-platform-architecture.md#ownership-and-shared-behavior).

The native path requires the same exact records and actual process exit zero,
plus the device peer's independent cleanup/status checks. It does not fabricate
a shell prompt or status for the native process. Output and process lifetimes
are bounded. Local Mac-to-ESP checks passed cancellation, boundary/error values
and a normal 100-pair exchange with confirmed cleanup. A separate run missed
the peer in its five-second discovery window. Before versioning, Mac/Pico
cancellation passed, but the extended-value case used the old service range
(handles 1–3), missing
the additional characteristics after 100 correct echo pairs. An independent
ESP client read those values from the same Pico, and the Pico confirmed the
Mac's exchange. This isolates an old central discovery layout, not missing
Pico attributes. Device-only forget attempts did not resolve the extended case;
no global host reset is performed. The versioned Mac/Pico checks below now pass;
portable-client coexistence has [separate combined-image results](../radio/README.md#portable-client-results).
These short runs are not a reliability guarantee.

## Portable GATT client

The additional `gatt-client` binary uses the public scan and GATT APIs, not the
legacy central ioctls. Build it with the command below, replacing
`--bin nxrs-std-ble --command ble_test` with
`--bin gatt-client --command gatt_client`. The same binary targets
`esp32s3-ble` or `pico2w-ble`; use separate output directories.

Start the separate radio fixture's `radio_test ble-only` on the peer, then run
`gatt_client`. It requires one UUID-matched peer, stops scanning, discovers the
characteristic, and checks 100 acknowledged writes and exact 20-byte readbacks.
It then disconnects, joins the scan worker, and waits for release of the native
callback context before reporting PASS. Check the shell exit status too.
Restart the peer fixture before each invocation; repeating this sequence tests
reconnect without retaining handles from the previous connection.

There is one event-loop wait point, shared by scanner and GATT wakeups. The
discovery window is five seconds, connect/disconnect bounds are 15 seconds,
and individual ATT operations have five seconds within a 50-second primary
budget. Cleanup has separate bounds and cannot turn a failed test into a pass.
`gatt_client cancel` cancels an admitted discovery request and requires recovery
quiescence; it does not establish that the request reached the radio first.

The native peer also provides two explicit corner-case modes:

- `radio_test ble-cancel` requires one connection and its completed disconnect,
  no application reads/writes, and successful callback teardown. Pair it with
  `gatt_client cancel`; a cancelled session is not a failed echo test.
- `radio_test ble-values` retains the ordinary 100-pair echo and exercises fixed
  44-, 64- and 65-byte read-only values. Pair it with `gatt_client values`:
  a remote ATT error must preserve the link, 44/64-byte values must be complete,
  and the 65-byte value must fail rather than return a 64-byte prefix. The local
  capacity error must then reach recovery quiescence.

All modes publish one immutable, versioned database:

| Service | UUID | Handles | Workload |
| --- | --- | --- | --- |
| V1 | `4e585253-0001-4000-8000-000000000001` | 1–3 | Original echo; `gatt-client legacy` |
| V2 | `4e585253-0002-4000-8000-000000000001` | 4–12 | Echo and extended values; default, `cancel`, `values` |

Both services retain the original echo characteristic UUID; discovery must be
scoped to the selected service. V2 is advertised directly; V1 is in the scan
response so both legacy packets fit their 31-byte limits. The public runner
accepts `--case legacy` alongside the other cases and requires a distinct
compatibility result. HAL APIs, board identity and operation deadlines do not
change. A V2 pass does not establish recovery of the earlier expanded-V1 cache.
Peripheral cleanup also waits for its disconnect callbacks and native link-idle
state before removing callbacks. A late event must not spill into the next case.

The original fixture added attributes to V1 without cache invalidation, leaving
some clients with the old three-handle range. This migration restores that V1
layout and gives extended tests a separate schema, rather than clearing host
state or rotating UUIDs between runs. It is not a general database-update
mechanism. Products with a changing database need the standard
[Service Changed/Database Hash handling](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-62/out/en/host/generic-attribute-profile--gatt-.html),
not a test-mode-dependent attribute table.

The Mac→Pico 2 W check passed V1 compatibility followed by V2 cancellation,
extended values/errors and normal transfer (4/4). A separate combined-image
run passed the same sequence in both Pico→ESP32-S3 and ESP32-S3→Pico directions
(8/8), swapping roles without reflashing. Each direction checked 100 V1 and
200 V2 exact 20-byte write/read pairs, complete 44/64-byte reads, rejected
65-byte overflow and link preservation after remote ATT error 14. Cases ran
without resets and required cleanup plus actual process/NSH zero status.
Original full flashes were restored, readback-verified and boot-checked.
These BLE-only checks do not establish long-term RF reliability or clear the
old expanded-V1 cache. Wi-Fi coexistence has
[separate device checks](../radio/README.md#portable-client-results).

With those images already flashed, the public two-board runner needs only
`pyserial`, not a host Bluetooth adapter. Swap the ports to reverse the roles:

```sh
python3 tests/nuttx-std/ble/client.py \
  --client-port CLIENT_USB_PORT --peer-port PERIPHERAL_USB_PORT \
  --case legacy --case cancel --case values --case full
```

The combined [radio image](../radio/README.md) also exposes
`radio_test ble-client [cancel|values|legacy]`. With that image on both boards,
add `--client-app radio_test` to the runner. It invokes the same shared client
scenario without starting Wi-Fi, so roles can be swapped without reflashing.
The exact acceptance checks and deadlines are unchanged.

The ordered cases prove re-acquisition after cancellation and a local transfer
error without rebooting. They stop on the first failure, never retry a payload,
and require both endpoints' exact lifecycle records and zero shell status.
Flashing, full backup/restore and identity checks remain separate responsibilities.
Host parser tests alone are not radio proof; qualified device cases are listed
below. Flashing helpers must still verify the selected board and images.

Host payload, cancellation and deadline tests run without radio access:
`cargo test -p nxrs-std-ble --lib --bin gatt-client`. The thin binary delegates
to the shared test scenario in `src/scenario.rs`; the radio fixture reuses it
for portable-client coexistence. Target builds and device
qualification remain separate checks.

Earlier BLE-only device checks used the expanded-V1 fixture, before this
migration. They do not qualify the V2 images:

| Portable client | Native peripheral | Case set | Passed | Exact 20-byte pairs |
| --- | --- | --- | ---: | ---: |
| ESP32-S3 (2026-10-17) | Pico 2 W | Cancellation → values/errors → normal, no reboot between cases | 3/3 | 200 |
| Pico 2 W (2026-10-17) | ESP32-S3 | Cancellation → values/errors → normal, no reboot between cases | 3/3 | 200 |

Every case confirmed client link teardown, scan-worker join, native callback-context
release and zero shell status on both endpoints. Peripheral cleanup then covered
advertising stop and callback removal, before the stronger disconnect barrier
above was added. Both sequences also checked
remote ATT error/link preservation, complete 44/64-byte values, rejected
65-byte overflow, and re-acquisition after both cancellation and overflow.
The original full flashes were restored and readback-verified; both final
restore/boot-health checks passed for both sequences. An earlier Pico normal-only
two-session sequence also passed.
Those expanded-V1 runs did not cover portable-client Wi-Fi coexistence. They
do not establish long-term RF reliability or resolve the separate Mac/Pico
extended-characteristic discovery failure.
An earlier ESP post-restore console check failed once; a separate bounded check
passed without another reset or flash change. That failure remains recorded.

The common controller is allocation-free and owns no thread or queue. This
fixture chooses an eight-event bounded queue and one event-loop wait point;
the NuttX provider adds one 8 KiB worker. The host SDK worker uses 128 KiB;
this does not change MCU stacks. Native NuttX stack tasks are separate.
The worker holds its process-global radio lease until native stop/close proves
quiescence; `finish()` explicitly joins the thread. It retains
events when the queue is full, and retains terminal errors even if their wakeup
was not delivered. Discovery is best-effort: native reports and the session's
32-peer registry are bounded. Registry exhaustion emits `DiscoveryLimited`
once per scan and omits new peers, while keeping the scanner usable and
previous IDs stable. A new provider session is needed to reset that registry.
`limited=false` describes that registry, not an absence of missed RF reports.
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

For discovery diagnosis, `ble_test fixture` filters this same eight-second
scan to the separate radio fixture's service UUID. It emits identity-free
`NXRS_BLE_WITNESS_READY`, `SEEN` and `DONE` records instead of a scan PASS.
Zero matches is a completed negative observation, not successful discovery.
The host reader in [witness.py](../radio/witness.py) checks ordering, counts,
worker shutdown and shell exit status; its caller owns the serial port.
A second receiver seeing the UUID during a missed host scan establishes
that an advertisement was transmitted and received during that window. It
does not distinguish host software from RF reception differences, and an
additional active scanner can itself affect the radio environment.

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
These historical images passed the earlier 63-observation target C/Rust ABI
gate; current prepared builds use the expanded 77-observation gate. Earlier
ESP simple-boot images
emitted a ROM SHA-256 mismatch warning despite verified writes; these scan
checks do not qualify boot integrity.

Host tests exercise lifecycle, cancellation, bounded delivery, stale IDs,
timeouts and UUID decoding. They do not test an actual radio:

```sh
cargo +1.90.0 test --locked --offline -p nxrs-ble
python3 tests/host/test_gatt_native.py
```

## Pico 2 W: experimental transport

Use the same build command with `--platform pico2w-ble` and a separate output
directory. Native macOS ARM builds use `NXRS_NUTTX_NATIVE_TOOLS=1`, the pinned
ARM nightly, GNU ARM tools, Kconfiglib and GNU sed described in the Wi-Fi fixture.
The builder stages the hash-pinned Wi-Fi/BLE `wb43439` image and selected Bluetooth
controller patch only for radio profiles. The current SDK 2.3.1 pin uses the
6,970-byte `.0065` patch; `radio-firmware.json` records the exact selection.
Retain their license, provenance
and patch ledgers with the build; no Pico SDK runtime is linked into nxrs.

The [experimental dependency patch](../../../docs/upstream-patchsets.md) uses
the existing radio daemon, bounded HCI ring processing and a shared-chip power
lease. Host tests exercise protocol framing, concurrent senders, backplane
window locking, stale sends, failed stops and idle-shutdown serialization.
The [USB recovery hook](../wifi/README.md#usb-recovery) uses one embedded timer,
not another thread. Reset passed at idle and during a foreground `sleep 30`,
without a manual BOOTSEL press. This is boot/reset qualification, not radio
qualification. Back up flash and verify the RP2350 identity before loading.
Use the recovery helper to unmount the ROM disk before rebooting; avoid a raw
`picotool -x` reboot while macOS has that disk mounted. Use `ble_test` and the
same positive/negative gates above once NSH responds. The combined images have
[separate coexistence results](../radio/README.md#portable-client-results);
long-term reliability and low-power behavior remain unqualified.

macOS has separate SDK tests and device results above. Linux scan/GATT IPC has
private-service tests, not hardware qualification. NuttX results do not qualify
desktop GATT.
