# CoreS3 navigation hardware

The initial `m5stack-cores3-gnss` profile targets M5Stack CoreS3 K128 with
Module GNSS M135 (NEO-M9N and BMI270). It is a development bring-up image;
physical boot, module power and sensor performance are not yet qualified.
It does not inherit the camera board’s flash layout or security deployment.

```sh
cargo firmware --app event-demo --platform m5stack-cores3-gnss
```

The board registers raw buses. GNSS owns UART/NMEA decoding; IMU owns I²C,
BMI270 initialization and SI conversion. Navigation projects WGS84 fixes into
local coordinates. Services and apps contain no board or OS selection. Neither
HAL adds a worker thread; the existing services own their polling schedule.

| Resource | CoreS3 GPIO / setting |
| --- | --- |
| GNSS UART1 | TX17 → module RX; RX18 ← module TX; 38400 8N1 |
| Sensor I²C0 | SDA12, SCL11; 400 kHz; 50 ms driver timeout |
| Module BMI270 | **0x68**; leave onboard CoreS3 BMI270 at 0x69 |
| Memory | 16 MiB quad flash; 8 MiB octal PSRAM |

M-Bus silkscreen uses legacy ESP32 pin numbers, not CoreS3 GPIO numbers. Set
M135’s GNSS UART routing to legacy TX17/RX16, corresponding to CoreS3 TX17/RX18.
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
