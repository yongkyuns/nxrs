# CoreS3 navigation and display

The initial `m5stack-cores3-gnss` profile targets M5Stack CoreS3 K128 with
Module GNSS M135 (NEO-M9N and BMI270). It is a development bring-up image;
physical boot, module power and sensor performance are not yet qualified.
It does not inherit the camera board’s flash layout or security deployment.

```sh
cargo nxrs build --app event-demo --platform cores3
```

The board registers raw buses. The profile binds the GNSS and IMU roles to their
UART and I²C resources. The GNSS HAL owns the UART and calls the UBX driver. The IMU HAL owns I²C,
BMI270 initialization and SI conversion. Navigation projects WGS84 fixes into
local coordinates. Services and apps contain no board or OS selection.
HAL-owned workers acquire data and publish bounded typed events to services.

| Resource | CoreS3 GPIO / setting |
| --- | --- |
| GNSS UART1 | TX17 → module RX; RX18 ← module TX; 38400 8N1 |
| Sensor I²C0 | SDA12, SCL11; 400 kHz; 50 ms driver timeout |
| Module BMI270 | **0x68**; leave onboard CoreS3 BMI270 at 0x69 |
| Memory | 16 MiB quad flash; 8 MiB octal PSRAM |

M-Bus silkscreen uses legacy ESP32 pin numbers, not CoreS3 GPIO numbers. Set
M135’s GNSS UART routing to legacy TX17/RX16, corresponding to CoreS3 TX17/RX18.
The GNSS HAL enables NAV-PVT in receiver RAM and restores its previous rate
on release; the receiver UART1 must retain its default UBX input/output support.
Select the module IMU’s alternate **0x68** address to avoid the onboard sensor.
The vendor documentation lists 0x69 while its sensor example uses 0x68; verify
the module address switch before applying power. Leave PPS disconnected.
The navigation profile preserves PMIC and charger defaults and leaves
display, camera, audio and 5 V boost output disabled. Battery operation and module supply under load
remain hardware qualification gates.

The compatible cellular candidate is **Stamp CatM S003 (SIM7080G)** for M135’s
reserved Stamp/COMX slot. Cellular is not enabled by this initial profile.
Its UART/AT protocol and link lifecycle belong to a modem HAL; services must
never send AT commands. Carrier/SIM compatibility, power, route ownership and
bounded packet-link teardown must be qualified before claiming cellular data
support. Magnetometer and barometer are also outside this initial slice.

References: [CoreS3](https://docs.m5stack.com/en/core/CoreS3),
[Module GNSS](https://docs.m5stack.com/en/module/Module_GNSS),
[vendor sensor example](https://github.com/m5stack/M5Module-GNSS/blob/main/examples/getSensorData/getSensorData.ino),
[Stamp CatM](https://docs.m5stack.com/en/stamp/Stamp_CatM).

### Acquisition wakes

GNSS waits on UART readiness and emits normalized UBX fixes from its HAL worker.
BMI270 IRQ and timer wakes use the same FIFO drain and typed-event path. At the
configured 100 Hz ODR and one-frame watermark, timer wakes are requested every
10 ms so fusion can predict per sample. The profile uses a 1 ms OS tick to limit
timeout quantization; scheduling and the sensor clock can still delay delivery.
The timer does not assert data readiness or invent samples; FIFO contents and
full/error status remain authoritative. Missed wake deadlines coalesce, and
cancellation wakes the wait immediately.

The [M135 v1.0 schematic](https://m5stack-doc.oss-cn-shenzhen.aliyuncs.com/561/SCH_Module_GNSS_V1.0.pdf)
leaves INT1/INT2 unconnected to the M-Bus. The stock profile omits `imu.irq-device`
and explicitly uses ODR-derived wakes. A wired board can supply a registered
rising-edge GPIO node; failure to acquire that configured IRQ remains an error.
Services contain neither timing nor device-wiring knowledge. Physical FIFO
throughput, timing, power and memory qualification remain open.

## Built-in display

The `m5stack-cores3-display` profile targets the original CoreS3 with the
320 × 240 ILI9342C panel. M5Stack also ships ILI9342E panels with a different
initialization sequence. Select the display profile on an ILI9342C unit.
The profile dedicates SPI2 to the LCD because GPIO35 serves as both LCD D/C
and microSD MISO. Touch input is separate from display output.

Apps depend on `nxrs-display` and use `display::open()`, `Display::size()`
and `Display::draw()`. Draw takes a borrowed row-major RGB565 rectangle and
completes synchronously. The ILI9342C Rust driver uses a 640-byte scanline
buffer. The BSP owns the SPI pins, AW9523B reset and AXP2101 backlight.
NuttX owns SPI transaction locking and the `/dev/spi2` endpoint.

~~~sh
cargo nxrs build --manifest-path tests/nuttx-std/display/Cargo.toml \
  --platform cores3 --profile m5stack-cores3-display \
  --out target/cores3-display
~~~

After installing the qualification image, run `display_qualification` from
NSH. Expect four vertical bars in red, green, blue and white, with a small
black square in the upper left, and the serial line
`DISPLAY PASS 320x240 RGB565`. The pattern checks color order, panel
orientation and edge coordinates visually. Host and target build validation
cover the software path. Physical display output remains unverified.

[CoreS3 hardware](https://docs.m5stack.com/en/core/CoreS3) and
[M5GFX panel initialization](https://github.com/m5stack/M5GFX/blob/master/src/lgfx/v1/panel/Panel_ILI9342.hpp)
describe the wiring and controller register values.

### LVGL qualification

NuttX builds LVGL 9.2.2. The Rust qualification draws one label through the
existing display HAL. It uses one 15 KiB RGB565 draw buffer and a 16 KiB LVGL
heap. The qualification C source only checks the Rust binding's ABI and LVGL
configuration at compile time.

~~~sh
cargo nxrs build --manifest-path tests/nuttx-std/display/lvgl/Cargo.toml \
  --platform cores3 --profile m5stack-cores3-display \
  --kconfig-enable GRAPHICS_LVGL --kconfig-enable LV_CONF_MINIMAL \
  --kconfig-enable LV_USE_LABEL --kconfig-set LV_MEM_SIZE_KILOBYTES=16 \
  --target-c-source tests/nuttx-std/display/lvgl/abi.c \
  --out target/cores3-lvgl
~~~

Run `lvgl_qualification` from NSH. Expect “CoreS3 display ready” centered on
the panel and `LVGL PASS 320x240 RGB565 (10 flushes)` on the console. Run it
again to check that the engine and display can be acquired again. Physical
panel output and touch input remain unqualified.

For a NuttX runtime check without hardware, use the same build command with
`--profile esp32s3-qemu-mock --features framebuffer --out target/cores3-lvgl-qemu`
in place of the platform, profile, and output options above. Boot
`target/cores3-lvgl-qemu/nuttx/nuttx.merged.bin` as the raw MTD drive on
Espressif QEMU's `esp32s3` machine. From NSH, run `lvgl_qualification` and
expect `LVGL FRAMEBUFFER PASS 320x240 RGB565 (10 flushes, 369 ink pixels, recovery)`.
The virtual display checks the order and contents of the RGB565 updates and
injects one write failure before two successful renders.
