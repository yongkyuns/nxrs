# GATT and Wi-Fi/BLE coexistence

Optional local tests for ESP32-S3 and Pico 2 W. The firmware uses the shared
Wi-Fi controller and a native NuttX GATT **peripheral fixture**; a Mac/Linux
host supplies BLE central operations and UDP/TCP echo service. This tests
board integration, not a production Bluetooth peripheral API.
The fixture preserves the original V1 echo service and advertises a separate
immutable V2 extended-test service; see the
[schema and compatibility case](../ble/README.md#portable-gatt-client).

For the shared `ble::gatt` **client** API and two-board tests, use the
[portable client qualification](../ble/README.md#portable-gatt-client).
The combined image can also compose that same client workload with Wi-Fi:
add `--peer-port PERIPHERAL_USB_PORT` to the runner command below, with this
radio image on both boards. Swap `--port` and `--peer-port` to reverse roles.
This mode needs only `pyserial`, not host Bluetooth permission or Bleak. It
reuses the same event-loop wait point for GATT and Wi-Fi observations, starts
network traffic after the first acknowledged write, and requires at least six
verified echoes before the final exact read. Separate
[portable-client results](#portable-client-results) cover this composition;
native-peripheral passes alone do not qualify it.

## Acceptance contract

A coexistence pass requires all of the following:

- Station association and DHCP through the shared Wi-Fi controller.
- 100 acknowledged 20-byte GATT writes, each followed by an exact readback.
  Both ends check sequence and every payload byte.
- Exact network echoes cycling through 16/64/256/512-byte payloads, with
  completed echoes advancing between the first and last GATT writes.
- Confirmed BLE disconnect, explicit Wi-Fi disconnect, joined workers and
  zero NSH exit status. Cleanup failures remain failures.
- For TCP, half-close followed by natural peer EOF before Wi-Fi teardown.

Each message has a two-second deadline and 10 ms pacing; there are no
application payload retries. Default sessions end after the BLE workload;
their variable echo totals are not throughput comparisons. Fixed studies use
1,000 echoes. GATT timing includes the acknowledged write, readback and host
Bluetooth scheduling, not just radio latency.

The current peripheral fixture waits for both its disconnect callback and
native link-idle state before declaring cleanup. Earlier results covered
advertising stop and callback removal, not this stronger peer-side barrier.

These tests do not establish saturation performance, radio loss rates,
pairing/bonding, encryption, notifications, multiple clients, or optimized
RAM/image size. Qualification stacks and diagnostics are deliberately larger
than a minimal application.

## What the device results establish

Retained local NuttX 13.1.0 results, reviewed 2026-10-18. Each reported complete
session also passed teardown. Warm passes do not erase failed sessions.

### Portable-client results

The current combined images passed eight standalone GATT cases in both
directions, followed by Pico UDP → ESP UDP → Pico TCP → ESP TCP (**4/4**).
All twelve cases used the same boot, without reflashing between roles.

| Client | Network | Exact GATT pairs | Total verified echoes |
| --- | --- | ---: | ---: |
| Pico 2 W | UDP | 100 | 893 |
| ESP32-S3 | UDP | 100 | 735 |
| Pico 2 W | TCP | 100 | 866 |
| ESP32-S3 | TCP | 100 | 676 |

Each completed case confirmed association/DHCP, exact payloads, BLE and Wi-Fi
disconnect, joined workers and zero shell statuses. Both TCP cases confirmed
natural peer EOF. Both original full flashes were restored, readback-verified
and boot-checked after each experiment. Echo counts are workload totals, not
throughput comparisons. Each coexistence case also verified at least six
echoes between the first and last GATT writes; exact overlap totals were not
retained in this run.

An earlier four-case batch passed **3/4**: the final ESP TCP case missed the
Pico during the unchanged five-second UUID scan, before connection or network
traffic. A separate cold control passed. The current warm pass does not erase
that failure or establish its cause; discovery reliability remains unresolved.

### Native-peripheral and network controls

| Configuration | Selected evidence | Conclusion |
| --- | --- | --- |
| ESP32-S3, Mac peer, UDP and TCP | Four C/Rust sessions per transport: 800 GATT pairs; 1,153 UDP and 1,165 TCP echoes | Exact concurrent data passed in these sessions. Discovery/setup failures have also occurred. |
| Pico 2 W, direct ESP AP peer, Wi-Fi only | Both socket paths completed 1,000 UDP and 1,000 TCP echoes each | Data and clean teardown passed; TCP observed natural EOF. |
| Pico 2 W, direct ESP AP peer, concurrent GATT | Normal `.0065` image, eight-session warm sequence: UDP 4/4, TCP-labelled 3/4; 7,000 echoes and 700 GATT pairs | The failed case stopped during BLE setup, before any GATT write or echo. This is not a TCP failure or a demonstrated reliability fix. |
| Pico 2 W, home network, UDP | Both C/Rust socket paths have passed full 1,000-message controls and also lost echoes, with GATT active or idle | Zero-loss qualification failed. SDK controls and an ESP station echo peer also lost traffic; neither Rust sockets nor active GATT is necessary for the loss. |
| Pico 2 W, home network, TCP | Latest C/Rust controls completed 1,000 echoes each, with and without 100 GATT pairs | Delivery and EOF passed. Native retransmissions and roughly 1.5-second recovery pauses remain relevant to application deadlines. |
| Pico 2 W, native BLE-only fixture | Matched ESP/Mac central sequence: ESP 7/7, Mac 7/8; 1,400 exact pairs, stopped at first failure | Established links passed. The Mac failure was discovery before a connection request; a separate normal-image Mac batch also reproduced a reason-8 connection timeout. |

The C control replaces only socket calls with native POSIX calls. It retains
the same Rust worker, stack, Wi-Fi ownership and GATT lifecycle; it is **not**
a comparison with a wholly C application. All network topology controls changed
the peer/path, not router settings.

### Open reliability limits

Two BLE symptoms remain separate:

- **Discovery:** Mac sometimes reports other devices but not the fixture in
  the original five-second UUID-selected scan. A temporary SDK/BTstack
  reference using the same `.0065` firmware also missed discovery. Neither
  Bleak nor extra instrumentation is required for the miss. Mac RF reception
  versus OS report delivery is unresolved.
- **Connection timeout:** a normal, uninstrumented NuttX image has reported a
  connection followed by reason 8 before the Mac's connected callback or any
  fixture-characteristic access. An earlier instrumented case had a 15 ms
  interval and 360 ms supervision timeout. Clean digital SPI comparisons do
  not prove RF delivery or exclude OS-managed ATT/control traffic.

Small sequential SDK/NuttX and scan-policy batches did not establish a fix or
a failure rate. No packet monitor was used, and no retry or wider timeout
rescues a failed case.

For Pico UDP, diagnostics captured losses in both directions. Some missing
outbound requests coincided with a firmware-wide failed MAC transmission;
another loss involved a response the peer had already echoed. Empty socket
queues and no retained RX checkpoint do not locate the loss inside the
AP/RF/controller path. Passing direct-AP controls, clean SPI samples and good
idle RSSI do not rule out a driver fault.

TCP's successful kernel recovery is useful, but not proof of a loss-free radio.
NuttX `NET_TCP_RTO=3` uses half-second units: its initial RTO is 1.5 seconds.
Both socket paths paused. UDP provides no delivery guarantee
([RFC 768](https://www.rfc-editor.org/rfc/rfc768)), but that does not excuse a
possible driver defect. Intermittent association/startup failures also remain
unqualified.

## Build and run

Use the repository's normal local NuttX toolchain setup. Choose
`pico2w-radio` or `esp32s3-radio`; the ESP profile enables both software
coexistence settings. No additional RTOS or host BLE runtime is installed
unless these optional tests are requested.

```sh
bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/radio/Cargo.toml \
  --app-package nxrs-std-radio --bin nxrs-std-radio --command radio_test \
  --priority 100 --stack-size 8192 --platform pico2w-radio \
  --out target/radio-pico --size-optimized --trace-only-backtrace \
  --panic-immediate-abort \
  --target-c-source tests/nuttx-std/wifi/ffi/credentials.c \
  --target-c-source tests/nuttx-std/radio/ffi/gatt.c \
  --target-c-source tests/nuttx-std/radio/ffi/socket.c \
  --target-c-source tests/nuttx-std/radio/ffi/stats.c

python3 -m venv target/radio-host
target/radio-host/bin/pip install -r tests/nuttx-std/radio/requirements.txt
target/radio-host/bin/python tests/nuttx-std/radio/run.py \
  --port DEVICE_USB_PORT --host-ip HOST_LAN_IPV4 --ssid TEST_SSID \
  --password-file /tmp/private-wifi-password
```

Flash with the existing board tools before running. Preserve the original
image and use the [Pico USB recovery helper](../../../tools/pico-bootloader.py)
for software BOOTSEL/reboot; unmount the ROM disk before rebooting it.

The password file contains only the passphrase, belongs to you, is outside the
repository and has mode 600 or stricter. Credentials travel in a no-echo USB
frame; they are not embedded in firmware or printed by the runner. macOS must
authorize Bluetooth for the process; Linux needs BlueZ and adapter permission.
Run only one fixture at a time; ambiguous advertisers fail rather than choosing
by name.

| Runner option | Purpose |
| --- | --- |
| `--transport tcp` | TCP instead of default UDP, with the same payloads and BLE workload |
| `--peer-port PERIPHERAL_USB_PORT` | Portable GATT client plus Wi-Fi on the DUT; the other board supplies the native peripheral. No study/C-socket controls in this mode. |
| `--socket-implementation c` | Native POSIX socket control; Rust is the default |
| `--study wifi-only` | Exactly 1,000 echoes without starting GATT, on the combined-radio image |
| `--study coexist` | Exactly 1,000 echoes; traffic starts after the first GATT write, and verified final-value reads continue until traffic ends |
| `--ble-only` | 100 exact GATT pairs and disconnect, without credentials, association, sockets or traffic worker |

The BLE-only NSH command is `radio_test ble-only`. The completion marker is a
barrier, not a pass: counts, disconnect, joins and status zero must still follow.
Native HCI startup may initialize the shared Wi-Fi controller even in this mode;
it is not a Wi-Fi-free image. Use a fresh boot for matched configuration controls,
not to hide a failed teardown.

The peripheral keeps static callback storage alive after the command exits;
stopping advertising is not a disconnect barrier. The shared client adapter
instead uses deferred, serialized LPWORK callbacks and explicit quiescence,
not the legacy blocking GATT ioctls.

## Dependency corrections and board configuration

Every dependency change is a registered, hash-recorded
[upstream patchset](../../../docs/upstream-patchsets.md), applied by the normal
builder to an isolated source copy. The registry is the detailed patch index;
this README does not duplicate it. Native sanitizer witnesses protect the
reproduced software contracts, not the cause of every physical failure.

Important corrections include allocator pairing, ATT capacity/discovery
handling, disconnect callback lifetime, TCP poll/retransmission progress,
GSPI frame bounds/alignment, DMA lifetime and correct PIO/DMA register handling.
These changes have targeted tests and are not claimed to cure all observed
RF/startup failures. Rust/native `pollfd` layout and initialization corrections
are separate libc/std patches. Clean builds check 77 ABI contracts against
their actual Cargo libc; unpatched poll imports, `ppoll`, and unsupported raw
`SIG_IGN` use remain rejected.

The pinned NuttX release has a Pico 2 base port, not a separate Pico 2 W port.
Patch 0011 supplies the wireless adaptation. Pins match the
[SDK Pico 2 W definition](https://github.com/raspberrypi/pico-sdk/blob/2.1.1/src/boards/include/boards/pico2_w.h):
GP23 enable, GP24 shared data/IRQ, GP25 chip-select and GP29 clock/shared VSYS.
Direct GP25 LED and ADC use are disabled. The combined `wb43439` firmware is
used, not the Wi-Fi-only image. This audit does not verify power or antenna
integrity, analogue waveforms, or establish a cause from the RP2350 revision.

Pico assets follow SDK 2.3.1's `.0065` Bluetooth patch. Wi-Fi/CLM and calibration
bytes are unchanged from the retained pin. `radio-firmware.json` records
revision, hashes and patch version; older revision-scoped caches are preserved.
`NXRS_PICO_RADIO_RELEASE=pico-sdk-2.1.1` selects the retained comparison pin.
The asset update did not demonstrate a timeout fix.

Patch 0028 aligns only Pico 2 W's PA coefficients with the pinned SDK reference.
Matched controls still had association/UDP failures. Likewise, read-only
aggregation-policy comparisons and small SDK-limit controls did not justify
production tuning. No speculative radio settings were added.

Honoring the already-configured 2,048-byte GSPI frame limit adds 4,128 bytes
across eight static slots in this Pico profile; it fixes a capacity mismatch,
not a small-packet loss diagnosis. The temporary board-init task uses an 8 KiB
stack after the 2 KiB startup stack corrupted TLS; it exits before the test.
These are integration/qualification resource choices, not measured minimum
application overhead.

## Optional diagnostics

Normal radio profiles include numeric native protocol/driver counters and
bounded failure snapshots. Values are global, not per-socket or atomic.
Unsupported layouts are unavailable, not zero errors. A queued TX or successful
SPI return is not an over-air ACK. Failed receives remain failed after the
250 ms late sample or controller query; those observations add no payload retry.

Extra instrumentation is opt-in and changes timing. Do not use instrumented
images for performance/footprint comparisons or treat an observed pass as a fix.

| Build option | Evidence and cost |
| --- | --- |
| `--kconfig-enable RP23XX_CYW43439_READ_OBSERVER` plus `--target-c-source tests/nuttx-std/radio/ffi/spi_capture.c` | Passive PIO/DMA digital read comparison, no jumper; one PIO block, one DMA channel, 2,056-byte buffer. Validate its known register pattern before interpreting data. |
| Add `--kconfig-enable RP23XX_CYW43439_WRITE_OBSERVER` | Compare outgoing commands/requested bytes with the same buffer; not analogue or controller-reception proof. |
| `--kconfig-enable BLUETOOTH_LINK_TRACE` | Link-event ticks and up to 11 buffered setup milestones, printed at disconnect. Check `tick_hz`; task timestamps are not RF timestamps. |

The default-off BCMF frame/daemon observers and the field meanings they expose
are documented beside their patches in
[the patch index](../../../docs/upstream-patchsets.md). UDP sequence tracing is
not applied to TCP fragments. Keep raw RF/identity logs private.

Pico radio syslog uses a bounded 4 KiB RAM log, read/cleared with `dmesg -c`.
Clear boot logs before each session and verify an empty reread; old uncleared
captures are cumulative. CDC syslog needs DTR, deliberately disabled by the
runner. Scan-only/client profiles may use different logging; do not require a
RAM-log command where the selected profile does not provide one.

Each normal build records configuration, image/input hashes, ABI results,
vendor assets and applied patch ledgers. Retain those together with the result:
an old image hash alone does not qualify current source. Historical images and
private debugging controls are not permanent SDK/build dependencies.
