# Wi-Fi hardware qualification

This is a functional test, not a size/speed benchmark. It runs `wifi::Controller`
and the NuttX station provider. It requires a configured, up `wlan0`;
scan mode never connects to an AP. No credentials are stored in source/config.
Main runs the scan fixture's event loop; the provider adds one Rust worker.
NuttX/vendor radio-stack tasks are separate from that worker.
The separate [GATT coexistence fixture](../radio/README.md) reuses the same
station connection and cleanup helper while exchanging BLE and UDP data.
The bounded observer counts results and requests stop without logging or
forwarding them. Console output is outside
dispatch and includes only a result count, not network names.

Wi-Fi is one crate (`nxrs-wifi`, imported as `wifi`). Its contract is in
`hal/wifi/src/api.rs`; private std execution is in `hal/wifi/src/worker.rs`.
The NuttX implementation is private under
`hal/wifi/src/providers/nuttx/`, and its target C helpers are under
`hal/wifi/ffi/nuttx/`. The platform builder adds those helpers automatically.
Queues, Crossbeam selection and scan/stop policy stay in this fixture, not a
Wi-Fi service or the HAL. Its library target contains the event-loop example so
lifecycle/fairness tests can run on hosts.

Terminal faults remain visible after shutdown even if their event could not be
queued. Native address cleanup is fallible: a failure retains the driver for
explicit stop retry; after owner Drop the worker retries with capped backoff.
Permanent failure does not become a successful cleanup.

Run the host tests with:

```sh
cargo +1.90.0 test --locked --offline -p nxrs-wifi
cargo +1.90.0 test --locked --offline -p nxrs-std-wifi --lib
```

## Desktop adapter

On macOS 11+ with the Xcode SDK, the same scan owner can use CoreWLAN:

```sh
NXRS_PLATFORM=macos cargo +1.90.0 run --locked -p nxrs-std-wifi
```

This selects the ordinary `wifi` API and existing worker; it does not add a
service or Rust runtime. Opening, scanning and stopping do not change radio
power or disassociate an observed host link. `wifi::open(sink)` keeps credentials
volatile and therefore disables macOS joins: [CoreWLAN stores joined-network
passwords and has no join-once equivalent](https://developer.apple.com/forums/thread/805392).
Applications may explicitly opt in with
`wifi::open_with_storage(sink, wifi::CredentialStorage::SystemManaged)`.
Credentials may then remain in OS storage after failure, disconnect or stop;
the HAL does not remove them. `capabilities().credential_storage` describes the
provider's native join behavior; Linux and NuttX continue using volatile
credentials even when storage is permitted. No platform-specific feature flag
or different command/event API is needed.

macOS joins require exclusive control of the interface. CoreWLAN offers no
per-client association lease; existing-link and replacement-BSSID checks cannot
prevent races with another process. Closing may undo a join made by this
controller, but leaves an observed host link alone. Location permission belongs
to the host application, not a HAL permission dialog. The initial CLI attempt
returned `PermissionDenied` after provider close/join. After granting permission
to a foreground test application, the actual native bridge passed three
scan/link/release cycles: 10, 12 and 12 networks, including the current access
point, with the host association unchanged. This verifies native scanning, not
the CLI owner in that permission context or real association/keychain behavior.
SDK-backed fake-object tests cover ownership,
permission refusal, no-save refusal before association, normalization and
failure rollback. These tests do not qualify real joins or OS keychain behavior.

Linux uses the same worker with NetworkManager over system GIO/D-Bus. Select
`NXRS_PLATFORM=linux` with the command above. It provides scan/link observation
and temporary open/WPA2-personal station activation, refuses ambiguous Wi-Fi
interfaces and never powers a radio or replaces an existing association. Owned
connections use volatile, client-bound profiles, not saved network settings.
It reports unknown RSSI rather than converting signal-quality percent into
invented dBm. The same profile enables [BLE scanning/GATT](../ble/README.md).
The default `native` profile enables neither radio.
Build prerequisites on Debian/Ubuntu are `pkg-config` and `libglib2.0-dev`.
`python3 -B tests/host/test_linux_radio_native.py` additionally needs a C
compiler and `dbus-daemon`; run `cargo +1.90.0 fetch --locked` first. It checks
real IPC against a private fake service, native archive/link metadata, and the
portable Rust owner's positive/injected-failure paths. The C tests use
ASan/UBSan. These checks do not qualify Linux RF hardware.

## NuttX builds

Use the pinned tools and private std setup from [NuttX std](../../../docs/nuttx-std.md).
The default builder expects Linux. Native macOS ESP builds additionally require
`NXRS_ESP32S3_NATIVE_TOOLS=1` and a Mac-hosted tools environment selected through
`NXRS_QEMU_TOOLS_DIR`; a Linux tools installation cannot be reused on macOS.
The ordinary firmware builder applies the existing NuttX/std fixes; this test
uses the tracked [radio patchset](../../../docs/upstream-patchsets.md), not a
modified upstream checkout. Build locally, with two jobs:

```sh
NXRS_NUTTX_BUILD_JOBS=2 bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/wifi/Cargo.toml \
  --app-package nxrs-std-wifi --bin nxrs-std-wifi --command wifi_test \
  --priority 100 --stack-size 8192 --platform esp32s3-wifi \
  --out target/wifi-station-device --size-optimized \
  --trace-only-backtrace --panic-immediate-abort \
  --target-c-source tests/nuttx-std/wifi/ffi/credentials.c
```

The separate ESP profile enables DHCP without WAPI credentials or automatic
boot-time association/DHCP. Wireless/network debug-info logs are disabled:
the pinned upstream driver can print passwords when wireless info is enabled.
It includes the recursive mutex support Espressif requires and stock Wi-Fi
packet sizing; the smaller NSH packet pool cannot satisfy the driver's quota.
Flash `target/wifi-station-device/nuttx/nuttx.bin` at offset zero; it preserves the
same addresses without the merged file's erased-flash padding to 16 MiB.
Run `ifup wlan0` if needed, then
`wifi_test` in NSH. A pass requires `NXRS_WIFI_SCAN_PASS`, a clean return to NSH,
and `echo $?` equal to zero. A print followed by a fault is not a pass. Repeat
from fresh boots; do not infer process-runtime reentry from one success. The
negative control `wifi_test fail` performs the same cleanup but exits with an
injected error: it must print no pass marker and NSH must report nonzero status.

Hardware qualification (NuttX 13.1.0, 2026-10-11):

| Board | Complete station runs | Failure/recovery checks |
| --- | --- | --- |
| ESP32-S3 | 7/7 across three boots; 140 exact UDP round trips | Wrong password and unavailable AP: no PASS, no UDP, NSH status one; valid reconnect passed after each |
| Pico 2 W | 7/7 across three boots; 140 exact UDP round trips | Same negative controls and successful reconnects |

Each board completed three same-boot trials, two negative-control recovery
trials, and two reset/reboot trials. These were fresh firmware boots, not
power-removal tests. Each also passed three scans, including reuse after an
injected scan failure (NSH status one, no PASS), and 50 exact USB console echo
round trips. These historical images passed the earlier 63-observation target
C/Rust ABI gate; current prepared builds use the expanded 77-observation gate.

Qualified raw images: ESP32-S3, 880,660 bytes, SHA-256
`4760c45f1e3497fc9c3ba097a6a7473c5a27223b48e031dd1feaeb240d53bcd1`;
Pico 2 W, 469,492 bytes,
`b6bf83de626d10315c2f8084efa3605df5cf52ff2a1133aa9b350713a5b9d8ad`.
These are functional-test profiles, not a C/Rust footprint comparison.

Scanning alone does not qualify association, DHCP or traffic. Station bring-up
on 13.1.0 identified two configuration omissions: BCMF defaults to ad-hoc mode,
so the common HAL must select infrastructure mode before authentication; and
both profiles request broadcast DHCP replies, so they must enable
`NET_BROADCAST` or IPv4 input drops those replies. Neither fix needs a
board-specific HAL branch or an upstream dependency modification.

Both profiles use bounded 200 ms ARP waits with five retries. The default
20 ms wait produced first-send `NetworkUnreachable` failures on ESP and Pico.
Increasing that native wait is not an application packet retry and does not
prove why radio/network ARP replies were delayed. Qualification still requires
association, DHCP, all 20 exact UDP echoes, explicit disconnect, worker join
and NSH status zero.

## Association, DHCP and actual traffic

For Pico 2 W, use the same command above with `--platform pico2w-wifi` and
`--out target/pico2w-wifi-device`. On macOS, select
`NXRS_NUTTX_NATIVE_TOOLS=1`; install the pinned ARM nightly and GNU ARM tools
described in the std setup. The profile stages verified CYW43439 firmware into
an ignored cache automatically. Ensure the Python Kconfiglib commands and GNU
sed are on `PATH`; macOS's older `kconfig-conf` cannot parse this NuttX revision.
It does not clone Pico SDK or embed credentials.
Keep its `radio-firmware.json`, patch ledgers and cached `LICENSE.RP` with the
build evidence/distribution. GPIO25 is radio chip-select, so this profile must
not use the non-wireless Pico's LED driver. Enter BOOTSEL, verify RP2350 identity
with `picotool info -a`, and back up flash before loading the ELF with picotool.
USB enumeration alone does not identify the board or qualify its Wi-Fi.
Pico NSH's USB handshake requires three consecutive newlines; the station
harness sends these automatically. An existing NSH can enter BOOTSEL with
`reboot bootloader`, avoiding a manual button press.
Patches 0013 and 0015 fix independently reproduced receive-buffer/credit and
IRQ-state bugs. NuttX 13.1.0 also includes the USB OUT rearm fix; the current
Pico image can scan and reconnect after station failure.

### USB recovery

Pico radio development profiles enable the opt-in 1200-baud reset hook. Once
installed, use the explicit Pico USB port and its ROM chip serial:

```sh
python3 tools/pico-bootloader.py --port /dev/cu.usbmodem01 \
  --serial CHIP_SERIAL
```

Run from the repo root with `pyserial` installed; `--picotool PATH` can select
an existing picotool. The helper confirms BOOTSEL for that exact RP2350 before
reporting success. It sends no shell command and can work while NSH is occupied.
The hook still needs USB/timer interrupts and is not hardware watchdog recovery.
The Pico radio profiles also select PICOBOOT-only recovery: a software-triggered
reset exposes the flashing interface without a mass-storage disk. This avoids
mounting a disk that disappears at reboot; a local flash/restore cycle verified
that interface layout and the final boot. Physical BOOTSEL and `board_reset(3)`
retain normal UF2 support. The upstream option defaults off outside these profiles.
On macOS leave BOOTSEL with
`python3 tools/pico-bootloader.py --reboot --serial CHIP_SERIAL` so the disk is
unmounted before it disappears. The helper selects media by ROM chip identity,
not its volume label, and expands only that USB device's driver subtree.
A busy or ambiguous disk aborts the reset when mass storage is present. If Spotlight
holds that virtual disk, explicitly add `--force-boot-disk`: force is permitted
only after rechecking the chip identity and stock ROM disk layout, never for a
normal storage disk. No system-wide automount setting is changed. Flashing and
verification can still run in BOOTSEL, including a critical full-flash restore.
Anyone with USB access can trigger it; disable `CDCACM_1200BPS_BOOTLOADER` in
production unless that maintenance access is intended.

Put the WPA2 password in a file **outside the repo**, owned by you and readable
only by you (`chmod 600`). Do not use a password argument, environment variable,
NSH command or build setting. With the Mac on the same reachable LAN and the
flashed board at NSH, run this using the Python environment that provides
`pyserial`/esptool:

```sh
python3 tests/nuttx-std/wifi/run.py --port /dev/cu.usbmodem14201 \
  --host-ip MAC_LAN_IPV4 --ssid TEST_SSID \
  --password-file /tmp/nxrs-wifi-password
```

Use `--open-network` instead of the file only for an intentionally open test AP.
The harness starts a local UDP echo endpoint, waits until firmware has disabled
console echo, then sends a length-delimited credential frame. It never prints
or saves raw serial data. The fixture checks actual driver carrier state (not
configured SSID), a DHCP ACK/lease, twenty exact 16/64/256/512-byte round trips,
explicit disassociation, worker join and zero NSH exit status. This proves local
bidirectional networking, not DNS, Internet access, throughput or long-term
reliability. AP client isolation/Mac firewall rules can prevent local echo even
when association and DHCP succeed.

Before qualification is complete, repeat from fresh boots, test an intentionally
wrong password and an unavailable SSID (no PASS), then reconnect with valid
credentials. Do not disable another user's AP to test loss. A successful scan
or host simulation is not evidence that these network gates passed.

Limits: the ESP scan read can block its provider worker for up to five seconds;
the consuming event loop remains responsive, but native cancellation is not
claimed. BCMF instead returns EAGAIN while scanning; the common provider retries
reads for up to six seconds without restarting the scan. That retry deadline
cannot preempt a blocking ioctl. The bounded driver table may omit networks, and its encryption flags
cannot reliably distinguish WPA2. Association and DHCP run on the one provider
worker with configured bounds; stop is nonblocking admission, not native-call
preemption. The idle monitor samples carrier/lease expiry every 500 ms; native
operations and full delivery queues can delay monitoring. Owned DHCP addresses
are cleared on failure, observed loss/expiry and session stop; automatic lease
renewal and DNS installation are not implemented yet. Disconnect is explicit;
stopping callbacks alone does
not imply disassociation. Native join failures can surface as `Timeout` or
`InvalidConfig`; the drivers do not reliably distinguish a rejected password
from an unavailable AP. Desktop association remains unqualified. Device
Wi-Fi/BLE coexistence has [separate combined-radio results](../radio/README.md#portable-client-results).
Host-injected source tests prove lifecycle and normalization
behavior, not wireless interoperability. These local runs do not qualify
long-term reliability, RAM/throughput, lease renewal or boot integrity.
