# Wi-Fi hardware qualification

This is a functional test, not a size/speed benchmark. It runs `wifi::Controller`
and the NuttX station provider. It requires a configured, up `wlan0`;
scan mode never connects to an AP. No credentials are stored in source/config.
Main runs the scan fixture's event loop; the provider adds one Rust worker.
NuttX/vendor radio-stack tasks are separate from that worker.
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
Permanent failure does not become a successful cleanup. Historical station
results below precede this refactor; the 13.1.0 retest covers scanning only.

Run the host tests with:

```sh
cargo +1.90.0 test --locked --offline -p nxrs-wifi
cargo +1.90.0 test --locked --offline -p nxrs-std-wifi --lib
```

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

Stable-release smoke checks (NuttX 13.1.0, 2026-10-11):

| Board | Scan results | Cleanup/control checks |
| --- | --- | --- |
| ESP32-S3 | Two scans: 11/16 results | Worker joined, status zero; injected failure returned one; 50 exact console echo round trips |
| Pico 2 W | Four scans across two boots: 8/7/6/10 results | Worker joined, status zero; both injected failures returned one; 50 echo round trips per boot |

Both Wi-Fi images pass all 63 target C/Rust ABI witnesses. The ESP image is
815,056 bytes (SHA-256 `f2acdcfc60e9c96d5474b91782b43c1c018882194c49a4f456fc4ad70ea1c27e`);
Pico is 463,496 bytes (`9db8e93f985713a02eb05ad074afdd94c8722eca8e514ce7c6566dd1eb682126`).
Association, DHCP and traffic were **not rerun** on 13.1.0; the historical
results below must not be treated as current station qualification.

Pre-upgrade ESP32-S3 tests (2026-10-10/11) passed scanning, worker shutdown,
unavailable-network timeout/reuse and the injected-failure control. Runtime
credentials were used only over the no-echo console input frame.

| Station profile | Complete qualification runs |
| --- | --- |
| Default 20 ms ARP waits | 1/5 instrumented trials; four first-send `NetworkUnreachable` failures, with no UDP reaching the host |
| Bounded 200 ms ARP waits | 10/10: five same-boot trials, then five hardware-reset trials |

Every successful run required association, DHCP, all 20 exact UDP echoes,
explicit disconnect, worker join and NSH status zero. NuttX collapses ARP
resolution failures into `NetworkUnreachable`. The new profile permits 200 ms
per ARP wait, still with five configured retries, and enables error-only
network diagnostics. This supports the ARP-timing explanation but does not
identify the radio/network source of delayed responses. No application packet
retries were added and no qualification gate was relaxed. The tested image
is 835,632 bytes, SHA-256
`64116d41e7d92cd9744bb0588ecae97172bc19cd2d2ac536ad92c4d01905dec3`,
and passed all 47 C/Rust ABI witnesses. Ten local runs are not long-term
reliability, RAM/throughput, lease-renewal or boot-integrity qualification.

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
Pre-upgrade Pico 2 W bring-up (2026-10-11): the IRQ-fixed 458,500-byte image
(SHA-256 `927d867f50821099d4db2e163495cae66ac4a282806b0816cf4123c92204a3a0`)
completed two scans, including reuse after a station failure. The first scan
explicitly returned NSH status zero. A later station attempt timed out and NSH
stopped responding. Association/DHCP/traffic and repeatability are therefore
**not qualified**; BOOTSEL recovery is required. Patches 0013 and 0015 fix
independently reproduced receive-buffer/credit and IRQ-state bugs, but do not
establish the cause of this remaining stall. Wi-Fi/BLE coexistence is untested.

### USB recovery

Pico radio development profiles enable the opt-in 1200-baud reset hook. Once
installed, use the explicit Pico USB port and its ROM chip serial:

```sh
python3 tools/pico-bootloader.py --port /dev/cu.usbmodem01 \
  --serial 22E9C0A2794832FA
```

Run from the repo root with `pyserial` installed; `--picotool PATH` can select
an existing picotool. The helper confirms BOOTSEL for that exact RP2350 before
reporting success. It sends no shell command and can work while NSH is occupied.
The hook still needs USB/timer interrupts and is not hardware watchdog recovery.
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
not imply disassociation. Pico 2 W and native desktop radios remain separate
qualification gates. Host-injected source tests prove lifecycle and normalization
behavior, not wireless interoperability.
