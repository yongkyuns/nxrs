# CoreS3 navigation hardware

The initial `m5stack-cores3-gnss` profile targets M5Stack CoreS3 K128 with
Module GNSS M135 (NEO-M9N and BMI270). It is a development bring-up image;
physical boot, module power and sensor performance are not yet qualified.
It does not inherit the camera board’s flash layout or security deployment.

```sh
cargo firmware --app event-demo --platform m5stack-cores3-gnss
```

The board registers raw buses. GNSS owns UART/UBX configuration and decoding; IMU owns I²C,
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
PMIC/charger defaults are preserved; optional display, camera, audio and 5 V
boost outputs are not enabled. Battery operation and module supply under load
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
