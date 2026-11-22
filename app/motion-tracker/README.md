# Motion tracker

The CoreS3 opens on the graph. Swipe down to show **IMU 1**, **IMU 2**,
**GNSS** and the pairing control in one row. Select a view, swipe up, or tap
outside the row to hide it.
Each IMU tab plots the three axes of **Accel** or **Gyro** for the last
12 seconds, sampled for display at up to 5 Hz. Readings use the sensor's
own coordinate frame. **GNSS** plots up to 96 recent fixes on an offline,
north-up grid with a distance scale, current coordinates and ground speed.
Loss of fix breaks the trail. This view works without internet access.
Street maps are a separate integration.

The dashboard uses a 32-slot event inbox. A slow display or console can cause
sample drops without blocking the sensor workers. Sequence gaps are visible
in telemetry. The tracker expires IMU readings after 500 ms and GNSS fixes
after three seconds.

## CoreS3 and M135 setup

| Input | Wiring |
| --- | --- |
| IMU 1 | CoreS3 BMI270 at `0x69` on the internal I²C bus |
| IMU 2 | M135 BMI270 on the same bus, with SW4 selecting `0x68` |
| GNSS | M135 NEO-M9N at 38,400 baud, with the module TX on M5-Bus pin 15 and RX on pin 16 |
| Display | Built-in ILI9342C panel on SPI2 |
| Touch | Built-in FT6336 on internal I²C at `0x38` |

Attach the M135 antenna for an outdoor fix. The CoreS3 display binding targets
the ILI9342C. M5Stack's August 2026 revision uses ILI9342E and needs separate
qualification. See the [CoreS3 pin map and revision
history](https://docs.m5stack.com/en/core/CoreS3) and the
[M135 switch and pin map](https://docs.m5stack.com/en/module/Module_GNSS).

The `m5stack-cores3-dual-imu` profile uses internal RAM. Enabling octal PSRAM
stalled the tested CoreS3 during startup. The profile reserves 16 KiB at flash offset
`0xf00000` for NuttX named settings. Use a dedicated firmware layout with that
region available. The app stores its versioned report interval under
`motion.interval`. The default is 200 ms and an explicit update accepts
100–2000 ms. Settings contain no location history or credential material.

```sh
python3 upstream/mbedtls/fetch.py
cargo nxrs build --app motion-tracker --profile m5stack-cores3-dual-imu
```

After loading the firmware on the matching board, run at the NuttX console:

```text
motion_tracker
motion_tracker --interval-ms 500 --frames 20
motion_tracker --frames 20
```

Use the bounded runs to confirm each IMU is live, the interval survives restart,
and acquisition can close and reopen. Open the GNSS tab outdoors after the
receiver acquires a fix. For a bench capture, add `--stream-usb`. That option
includes precise coordinates while a fix is fresh and emits one versioned JSON
object per line. The first and second entries in `imu` are the onboard and
module sensors. Each live entry carries the source sequence and monotonic
reception timestamp. The IMU entries include nominal sample period, acceleration
in m/s² and angular velocity in rad/s. Live GNSS entries include latitude and
longitude in degrees and speed in m/s. The dashboard's speed is in km/h.

## Live iPhone data over Bluetooth

1. Run `motion_tracker`, swipe down and tap **Pair**. **Cancel** closes the QR
   panel and ends that pairing attempt. The `--ble` option opens the same panel
   at startup.
2. Open **Motion Tracker** from Nxrs Home Settings or the setup screen.
   Tap **Scan pairing code** and point the iPhone at the CoreS3. Allow camera
   and Bluetooth access when prompted.
3. The app shows both accelerometers, GNSS status, and location when a fix is
   fresh. It clears stale readings and reconnects to the paired tracker after a
   temporary drop or when you return to the screen. The pairing key stays in
   memory while the tracker screen is open. Tap Disconnect to clear it.

The CoreS3 serves a BLE GATT byte stream. The phone relays TLS ciphertext over
20-byte reads and acknowledged writes. The device publishes the latest snapshot
at up to 1 Hz and waits for the output to drain between records. Use the USB
stream for higher-cadence bench captures.

Pairing is opt-in for each run. The TLS 1.2 PSK cipher is
`TLS_PSK_WITH_AES_128_GCM_SHA256` and the PSK identity is `nxrs-tracker-v1`.
TLS authenticates both peers through possession
of a generated 256-bit key. The key is shown on the device, used for a one-hour
session, and kept out of persistent settings. The display returns to the
graph when the phone authenticates, then shows **Linked** or **Paired** while
the key remains valid. Tap **Pair** again when the one-hour session expires.
This pairing suite has no
forward secrecy. The NuttX random device checks the Bluetooth PHY state under
the vendor mutex before sampling. Physical entropy and throughput qualification
remain separate from the firmware build.
[Espressif RNG requirements](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/system/random.html)

The BLE UUIDs end in `3400` (service), `3401` (phone writes ciphertext), and
`3402` (phone reads ciphertext). Their full prefix is
`3CB1DE6E-A955-49B7-BE4F-C4572D3F`. Android can use the same GATT byte stream and
TLS configuration. Each decrypted record is the same v1 NDJSON frame used by
USB. Frame readers handle split or joined lines and reject records longer
than 768 bytes including the newline. A future modem can carry the same frames
over authenticated TLS/TCP with its own enrollment policy.
