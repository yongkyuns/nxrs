# Motion tracker

## Headless portability

The headless build uses the profile's primary IMU and emits version 2 JSON.
The desktop smoke test builds and runs the application with one mock IMU.
The NuttX QEMU profile runs the same headless application with a modeled
BMI270 on I²C. Both checks require fresh IMU telemetry and a clean exit.

```sh
python3 tests/native/motion_tracker.py
```

[Headless NuttX build and verification](../../tests/nuttx-std/tracker-sim/README.md#headless-one-imu-tracker)

## Dashboard

The CoreS3 opens on the graph. Swipe down to show **IMU 1**, **IMU 2**,
**GNSS** and the pairing control in one row. Select a view, swipe up, or tap
outside the row to hide it.

Each IMU tab plots the three axes of **Accel** or **Gyro** for the last
12 seconds, sampled for display at up to 5 Hz. Readings use the sensor's
own coordinate frame. **GNSS** plots up to 96 recent fixes on an offline,
north-up grid with a distance scale, current coordinates and ground speed.
Loss of fix breaks the trail. This view works without internet access.
Street maps are a separate integration.

The dashboard samples the latest observation from each sensor when it redraws
or publishes telemetry. Touch events and sensor faults have their own inbox.
The BLE export receives the full stream before the dashboard downsamples it.
The tracker expires IMU readings after 500 ms and GNSS fixes after three
seconds.

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
motion_tracker --battery
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

`motion_tracker --battery` prints one AXP2101 reading and exits. It reports
the estimated charge percentage, the direction of charge and whether
external power is available. A missing pack or unavailable estimate has
its own status. The command can be run from the NuttX shell before starting
the live dashboard.

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
   If the phone needs to scan again, tap **Pair** on the CoreS3 after it
   disconnects. The board opens the menu automatically.

The CoreS3 sends the latest dashboard snapshot at up to 1 Hz. Current iPhone
builds use the notification service and request binary sensor data after
TLS pairing. The authenticated notification stream carries both record types.
With older firmware, the app shows **Dashboard only**. Earlier clients can
still read the dashboard using the original service and 20-byte GATT reads.

The export sends each received IMU sample at the configured 100 Hz and each
valid GNSS fix at 2 Hz on the dual-IMU CoreS3 profiles. The GNSS driver verifies
a 500 ms navigation period in receiver RAM and restores the previous
configuration on shutdown. The phone shows acceleration and gyroscope for
both IMUs, recent fixes on the map, observed rates, and sequence gaps. Export
starts with each authenticated connection. If the tracker fills its export
buffer, it delivers the samples already queued and reports an incomplete
export. Sensor acquisition and the display continue. The phone also marks an
export incomplete if it detects a sequence gap.

Pairing is opt-in for each run. The TLS 1.2 PSK cipher is
`TLS_PSK_WITH_AES_128_GCM_SHA256` and the PSK identity is `nxrs-tracker-v1`.
TLS authenticates both peers through possession of a generated 256-bit key.
The key is shown on the device, used for a one-hour
session, and kept out of persistent settings. The display returns to the
graph when the phone authenticates, then shows **Linked** or **Paired** while
the key remains valid. Tap **Pair** again when the one-hour session expires.
This pairing suite has no forward secrecy. The NuttX random device checks the Bluetooth PHY state under
the vendor mutex before sampling. Physical entropy and throughput qualification
remain separate from the firmware build.
[Espressif RNG requirements](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/system/random.html)

The BLE UUID prefix is `3CB1DE6E-A955-49B7-BE4F-C4572D3F`. The notification
service uses suffixes `3410` (service), `3411` (phone writes ciphertext) and
`3412` (phone subscribes to ciphertext). The original dashboard service uses
the unchanged `3400`, `3401` and `3402` UUIDs and handles, and is advertised
in the scan response. Dashboard snapshots retain the v1 NDJSON
layout used by USB and cellular. Binary export begins after the authenticated
client sends the bytes `4e 58 52 53 01`. Each batch starts with `a5 01` and a
little-endian 16-bit payload length. A zero payload length reports capture
buffer overflow. A data batch contains up to 16 records. Each record
has a one-byte kind (IMU 1, IMU 2, GNSS are 1, 2, 3), the low 32 bits of the
source sequence, and the low 32 bits of monotonic reception time in
milliseconds. All numbers are little-endian. IMU values are six float32s
(acceleration, then gyroscope in X/Y/Z order). GNSS values are float64 latitude
and longitude, then float32 ground speed in m/s.

BLE and cellular share the telemetry stream publisher. Each connection keeps
one frame in progress and coalesces waiting snapshots to the latest value.
The communications worker posts link changes to the dashboard inbox. Pairing,
wire encoding and transport-specific connection policy stay with the tracker.

## Stamp CatM cellular

The `m5stack-cores3-stamp-catm` profile adds the M5Stack S003 (SIM7080G) to
CoreS3 + M135. It uses the Stamp’s TX pad **JP2** to CoreS3 GPIO7 and the Stamp’s
RX pad **JP4** to GPIO13. JP1 and JP3 must stay open with GNSS using M-Bus
pins 15 and 16. The Stamp UART is 115200 8N1. Check the M135 PCB revision
against the [M135 schematic](https://m5stack-doc.oss-cn-shenzhen.aliyuncs.com/561/SCH_Module_GNSS_V1.0.pdf).
The [Stamp documentation](https://docs.m5stack.com/en/stamp/Stamp_CatM)
covers SIM and antenna installation.

```sh
cargo nxrs build --app motion-tracker --profile m5stack-cores3-stamp-catm
```

Use your carrier’s IPv4 APN. Provide the collector’s IPv4 address, TLS port and
SHA-256 fingerprint of its DER-encoded leaf certificate as 64 hex digits.

```text
motion_tracker --cellular-apn YOUR_APN --collector IPV4:PORT --collector-pin SHA256
```

The tracker sends v1 NDJSON over a one-way TLS 1.3 stream after authenticating
the pinned collector certificate. The collector reads newline-delimited records
without sending application data back. Use a named-curve P-256 certificate
and provision its fingerprint through a trusted channel. The pin authenticates the collector.
Device authentication at the collector would require a separate enrollment
mechanism. Use `--cellular-apn YOUR_APN` on its own to bring up PPP for diagnostics.

A lost PPP session is torn down before redial. A lost collector connection
reconnects over the existing link when possible. Retry delays increase from one
to at most 30 seconds. The tracker keeps the latest snapshot and one record
in flight. Samples superseded during an outage are skipped. BLE and cellular
share one TLS owner task while acquisition and the dashboard continue independently.
The Stamp profile uses bounded NuttX TCP write buffering. The unbuffered sender
waits for acknowledgments even on a nonblocking socket, which can delay BLE
when TCP retransmits. The buffered sender queues writes in a fixed kernel pool.
A modem that cannot recover command mode reports the fault and stops redialing.

The [simulator qualification](../../tests/nuttx-std/tracker-sim/README.md)
exercises the full tracker and a local collector over PPP. The
[CoreS3 and Stamp test](../../tests/nuttx-std/tracker-hardware/README.md)
checks BLE delivery and TLS recovery on a real carrier. That test has not yet
been run on hardware.
