# ESP32-S3 NuttX preemption in Chromium

The witness boots a two-core NuttX image through the ESP32-S3 ROM in a local
ESP-EMU 1.2.0 browser instance. It uses the ESP32-S3 scheduler port and the
usual NuttX timer and inter-core interrupt handlers.

- On CPU 0, a NuttX watchdog expires while a low-priority thread spins. The
  timer handler wakes a higher-priority thread pinned to CPU 0.
- On CPU 1, a low-priority thread spins while CPU 0 calls
  `nxsched_smp_call_single`. The resulting interrupt wakes a higher-priority
  thread pinned to CPU 1.

Each case checks the interrupt context and core number, progress in the busy
loop, and that the higher-priority thread ran before the busy thread finished.
The loop contains loads, stores and branches with no calls. The browser test
requires both results and NuttShell with both guest cores active. The same
firmware also passed both cases in native Espressif QEMU.

## UART repair

The stock browser module returns zero from ESP32-S3 `UART_CLK_CONF`. During
startup NuttX reads the clock selector and subsequently divides by zero.
`patch_uart_clock.py` verifies the published Wasm module's SHA-256 and adds
readback for the three UART clock configuration registers. The values start
at the ESP32-S3 hardware reset values and retain guest writes. The original
UART write handler still runs. The fixture creates one machine per Wasm
instance and starts each run with a fresh instance. The added state lives
for that instance.

Espressif publishes the emulator as prebuilt binaries. The repair applies
locally to the released Wasm module and leaves the original file available for
the failure control. The witness covers clock register readback and guest
preemption. UART line timing and simultaneous execution on physical cores
require separate tests.
[ESP-EMU v1.2.0](https://github.com/espressif/esp-emulator/releases/tag/v1.2.0)

## Reproduce

Use the normal Nxrs ESP32-S3 build tool environment with GNU Make, the Xtensa
cross-compiler, `kconfig-tweak`, Kconfiglib, and `esptool.py` on `PATH`.
The build takes a prepared ESP HAL source directory. It archives the checkout's
NuttX and NuttX Apps pins into a fresh output tree and discards any staged ESP
HAL object files so they compile for two cores. It uses the upstream
`esp32s3-devkit:smp` defconfig with a small C-only test and NuttShell.

With Binaryen's `wasm-dis` and `wasm-as`, Python Playwright, and Chromium
installed, run from the Nxrs repository root:

```sh
out=target/esp32s3-preemption
esp_pkg=/path/to/esp-emu-1.2.0-wasm/www/pkg
bash tools/build-esp32s3-preemption.sh /path/to/esp-hal-3rdparty "$out"
python3 tests/browser/esp32s3-preemption/patch_uart_clock.py \
  "$esp_pkg/esp_emu_bg.wasm" "$out/esp-emu-s3-uart.wasm"
python3 tests/browser/esp32s3-preemption/run.py \
  --glue "$esp_pkg/esp_emu.js" --wasm "$out/esp-emu-s3-uart.wasm" \
  --firmware "$out/nuttx/nuttx.merged.bin"
```

Use `--browser` to select an installed Chrome executable. Passing the stock
`esp_emu_bg.wasm` with the same firmware reproduces the boot failure.

## Motion Tracker BLE pairing

The [standalone browser demo](../tracker-demo/README.md) runs the H4 controller,
GATT central and TLS peer in the browser worker.

`patch_tracker.py` extends the version-pinned UART clock repair with an ESP32-S3
UART2 register model and the existing emulated interrupt matrix. The patched
browser glue exposes UART2 byte input and output for an H4 controller. It also
serves the guest RNG register with `crypto.getRandomValues` and advertises the
existing NuttX simulator entropy capability only in a secure context where
Web Crypto is available. [Web Cryptography API](https://w3c.github.io/webcrypto/#Crypto-method-getRandomValues)

The Chromium test boots the two-core image with the standard dashboard init,
including the NuttX H4 controller registration. Bumble supplies the controller
and central from the test process. The central decodes the guest-rendered QR,
discovers the tracker GATT service, authenticates with OpenSSL using TLS 1.2
PSK, and reads three advancing snapshots with both IMUs and GNSS live. The
probe keeps the pairing key in memory and stores the decrypted test snapshots.

Use Python 3.13 or newer with Playwright and the
[dashboard peer dependencies](../../nuttx-std/tracker-sim/README.md#full-dashboard-and-bletls).
Build in the normal Nxrs ESP32-S3 tool environment with the pinned Mbed TLS
source archive supplied through `NXRS_TLS_ARCHIVE`.

```sh
out=target/tracker-esp-emu
www=/path/to/esp-emu-1.2.0-wasm/www
cargo nxrs build --app motion-tracker --profile esp32s3-esp-emu-tracker \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --target-c-source tests/nuttx-std/tracker-sim/dashboard_init.c \
  --out "$out/firmware"
python3 tests/browser/esp32s3-preemption/patch_tracker.py "$www/pkg" "$out/patched"
python3 tests/browser/esp32s3-preemption/tracker.py --ble \
  --bundle "$www" --patched-package "$out/patched" \
  --elf "$out/firmware/nuttx/nuttx" --report "$out/ble-smp.json"
```

Add `--tiles --timeout 105` to the BLE probe to exercise the GNSS map.
The guest receives a moving UBX fix from the fixture. The BLE peer supplies
two synthetic 170 × 170 tiles over the authenticated notification stream.
The probe also checks the sequence of each exported sensor. It verifies
that the second request precedes the boundary crossing, verifies both tile
colors on the guest panel before and after crossing, and saves those frames
beside the JSON report. Chrome runs the ESP32-S3 guest and the peer runs
locally through the emulated H4 link.

For an OpenStreetMap capture, add `--osm-tiles` and install the tile
relay's Python dependencies from the
[tracker map setup](../../../app/motion-tracker/README.md#gnss-street-map).
Rebuild this qualification image with
`NXRS_TARGET_C_FLAGS=-DTRACKER_MAP_PANEL` so the simulated LCD captures
the map square with its RGB565 colors. The host downloads the two requested
tiles over HTTPS using the relay's HTTP cache and raster conversion.
The browser receives both tiles through the same TLS and H4 path as the
synthetic check. The test uses a public London route at the tracker default
of zoom 18. Add `--map-zoom 16` or `--map-zoom 17` to compare wider views.
[OpenStreetMap attribution](https://www.openstreetmap.org/copyright)

Add `--move-mps 1.5` for walking, `--move-mps 15` for eastbound driving,
or `--move-mps -15` for the westbound crossing at zoom 18.
First run the real-tile probe once to populate the HTTP cache,
then place the timed report beside it. The replay loads the first tile,
advances GNSS every 500 ms, and crosses the boundary on schedule. It
reports tile request, transfer completion, and paint times in guest
milliseconds, plus any lead or lateness at the crossing. It checks the
full sensor sequence numbers throughout. This measures delivery from a
warm HTTP cache in ESP-EMU.

Add `--deny-entropy` to run the control with Web Crypto disabled in the
test page. UART2 still boots and the app refuses TLS pairing. The UART2 model
supports one guest per module and moves bytes at browser batch boundaries.
Physical UART line timing, RF and phone interoperability remain unqualified.

## Motion Tracker dashboard with the clock-only patch

The `esp32s3-esp-emu-tracker` qualification profile builds the production Rust
app and its NuttX providers with two cores. It reuses the QEMU dashboard's
guest-side I²C, SPI and RAM MTD device models. The browser probe connects the
existing NEO-M9N test peer to UART1 through ESP-EMU co-simulation.

| Path | Chromium result |
| --- | --- |
| UART0, UART1 | NuttShell accepted commands over UART0. The GNSS driver configured the simulated receiver over UART1, decoded live UBX fixes and restored the receiver's rate on exit. |
| Display and sensors | The app rendered its RGB565 dashboard, streamed live data from both BMI270 models, polled the FT6336 model, and reported 73% from the AXP2101 model. The panel PNG is read from guest RAM. Interactive touch and the physical CoreS3 I²C/SPI paths remain unqualified. |
| UART2 / H4 | The ordinary dashboard init stalled in Bluetooth setup with both cores active. The browser accepted the peer's pin claim and delivered no UART2 bytes. The ESP-EMU release documents UART0 and UART1 co-simulation. |
| BLE and TLS | The browser init skips H4 registration so the dashboard can run. An explicit `--ble` diagnostic reached NuttX `bt_hci_cmd_create` with the controller uninitialized and faulted. The probe also reported that the QEMU host entropy provider is unavailable, which blocks the existing TLS entropy admission path. |
| Settings | A second app launch read back the saved 250 ms interval from NuttX's RAM-backed settings device. The physical flash partition and persistence across guest resets remain unqualified. |

Board-level emulation still needs ILI9342C SPI rendering and D/C routing,
FT6336 touch injection, two BMI270 parts, and AXP2101 and AW9523B bring-up
behavior. The table covers the dashboard profile, which has no modem peer.
[ESP-EMU virtual parts](https://github.com/espressif/esp-emulator/blob/v1.2.0/docs/guides/PARTS.md)

Build and run with the tool environment above and the pinned Mbed TLS source
archive supplied through `NXRS_TLS_ARCHIVE`:

```sh
out=target/tracker-esp-emu
www=/path/to/esp-emu-1.2.0-wasm/www
cargo nxrs build --app motion-tracker --profile esp32s3-esp-emu-tracker \
  --target-c-source tests/nuttx-std/tracker-sim/devices.c \
  --target-c-source tests/browser/esp32s3-preemption/tracker_init.c \
  --out "$out/without-controller"
python3 tests/browser/esp32s3-preemption/tracker.py \
  --bundle "$www" --wasm target/esp32s3-preemption/esp-emu-s3-uart.wasm \
  --elf "$out/without-controller/nuttx/nuttx" --report "$out/dashboard.json"
```

The probe checks eight telemetry snapshots, the final live IMU and GNSS
values, receiver rate restoration, battery status, panel output and touch
polls. It saves a JSON report and panel PNG. For the H4 boot control, replace
`tracker_init.c` in the build with
`tests/nuttx-std/tracker-sim/dashboard_init.c` and use a separate output
directory. For the BLE diagnostic, pass
`--command 'motion_tracker --ble --frames 4 --stream-usb'` with the browser init.
