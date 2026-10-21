# Wi-Fi and Bluetooth qualification

Optional local tests for the shared Wi-Fi and BLE APIs on ESP32-S3 and Pico 2 W.
A Mac/Linux host provides network echoes. GATT uses either the host's Bluetooth
adapter or a second board through the [portable GATT client](../ble/README.md#portable-gatt-client).
The native peripheral is a test fixture, not a production peripheral HAL.

## What a pass means

Each case requires association/DHCP, exact message contents, confirmed
disconnects, joined workers and zero NSH status. A coexistence case also requires
100 acknowledged 20-byte GATT writes with exact readback and verified network
traffic between the first and last GATT writes. TCP must half-close and receive
natural peer EOF before Wi-Fi teardown.

Network messages cycle through 16/64/256/512 bytes with 10 ms pacing, a two-second
deadline and no application retries. Normal coexistence stops after the GATT
workload; fixed studies use 1,000 echoes. Variable totals are not throughput
measurements. A failed case remains failed; cleanup or a later pass cannot rescue it.

Historical NuttX 13.1.0 portable-client checks completed eight standalone GATT
cases in both directions and four combined cases on the same boot:

| GATT client | UDP echoes | TCP echoes |
| --- | ---: | ---: |
| Pico 2 W | 893 | 866 |
| ESP32-S3 | 735 | 676 |

Each combined case completed 100 exact GATT pairs, verified network overlap and
clean teardown. These are observed passes, not a guarantee of long-term radio
reliability. Pairing, bonding, notifications, multiple clients, saturation and
minimum RAM/image size are outside this fixture's scope.

These device measurements predate the diagnostic cleanup. Host/native
regressions check the reduced sources; no new device qualification is claimed.

## Known issues

Pico Wi-Fi has an unresolved delivery limitation on the tested home-network
path. It also reproduces with the pristine Pico SDK 2.3.1 C driver, without
Rust, nxrs or NuttX. In the 2026-10-19 control, 735 exchanges completed before
the next 512-byte UDP request timed out. A capture covering the full workload
recorded all 735 request/reply pairs, no capture drops and no failed request at
the Mac ingress tap. The outgoing frame was valid before driver submission.

Earlier NuttX tests also lost UDP traffic with an ESP station as the echo peer:
this is not Mac-only. Direct Pico-to-ESP access-point controls completed 1,000
UDP and TCP echoes each. Those earlier peer controls were not the same SDK
experiment, so they do not establish a loss rate or a common cause.

This is a known platform/network interoperability issue, **not a confirmed
Raspberry Pi firmware defect**. Driver acceptance is not proof of radio
delivery. The exact cause within the controller, RF link, AP or host receive
path remains unconfirmed. Further upstream/platform RCA is deferred; it is
not a blocker for continuing nxrs application/service development.

Other retained limits are intermittent Pico BLE discovery misses and reason-8
connection timeouts before application traffic, plus TCP exchanges exceeding
the fixture's two-second deadline. They are not established as the same fault.
The separate scan-ownership correction passed 24 warm Mac-to-Pico GATT sessions
(2,400 exact pairs); it fixes failed-start/stop cleanup, not these unexplained
radio symptoms. No retries or wider deadlines hide these limits.

Investigation-only packet/DHCP taps, SPI observers, fault readbacks, capture
supervisors and C-socket comparison modes are not part of the maintained
fixture. A compact private evidence archive retains the SDK reproduction.

## Build and run

Use the [normal NuttX toolchain](../../../docs/nuttx-std.md) and choose
`pico2w-radio` or `esp32s3-radio`. The ESP profile enables software coexistence.
No full Pico SDK checkout is required: the normal builder stages pinned radio
firmware on demand.

~~~sh
NXRS_NUTTX_BUILD_JOBS=2 bash tools/build-nuttx-std-app.sh \
  --app-manifest tests/nuttx-std/radio/Cargo.toml \
  --app-package nxrs-std-radio --bin nxrs-std-radio --command radio_test \
  --priority 100 --stack-size 8192 --platform pico2w-radio \
  --out target/radio-pico --size-optimized --trace-only-backtrace \
  --panic-immediate-abort \
  --target-c-source tests/nuttx-std/wifi/ffi/credentials.c \
  --target-c-source tests/nuttx-std/radio/ffi/gatt.c

python3 -m venv target/radio-host
target/radio-host/bin/pip install -r tests/nuttx-std/radio/requirements.txt
target/radio-host/bin/python tests/nuttx-std/radio/run.py \
  --port DEVICE_USB_PORT --host-ip HOST_LAN_IPV4 --ssid TEST_SSID \
  --password-file /tmp/private-wifi-password
~~~

Back up the board before flashing with the existing board tools. The password
file contains only the passphrase, belongs to you, has mode 600 or stricter and
lives outside the repository. Credentials travel in a no-echo USB frame; they
are not embedded in firmware. macOS requires Bluetooth permission for a host
GATT peer; Linux requires BlueZ and adapter access. Run one fixture at a time.

| Runner option | Workload |
| --- | --- |
| `--transport tcp` | TCP instead of UDP, with the same payloads and GATT workload |
| `--peer-port PERIPHERAL_USB_PORT` | Shared GATT client on the DUT, peripheral on the other board; no host Bluetooth needed |
| `--study wifi-only` | 1,000 echoes without GATT |
| `--study coexist` | 1,000 echoes overlapping GATT; verified final-value reads continue until traffic finishes |
| `--ble-only` | 100 exact GATT pairs and disconnect, without Wi-Fi credentials or a traffic worker |

The second-board mode excludes study controls. Both boards use this radio
image; swap their serial ports to reverse roles. For Pico software BOOTSEL
entry use the [USB recovery helper](../../../tools/pico-bootloader.py), which
unmounts the ROM disk before reboot.

All retained dependency corrections are [tracked patches](../../../docs/upstream-patchsets.md)
applied to isolated build copies. Keep their build ledgers with firmware.
They cover integration and correctness defects; they do not claim to cure the
known radio limitation. Qualification profiles are not minimum-footprint
production configurations.
