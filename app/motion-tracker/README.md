# Motion tracker

## Device composition

Motion Tracker requires a primary IMU and GNSS. Profiles select the devices
and HAL providers. The same entry point runs without a display, touch input
or a secondary IMU when those capabilities are unavailable. The desktop
smoke test exercises the tracker with mock sensors and no display.

```sh
python3 tests/native/motion_tracker.py
```

[One-IMU NuttX acquisition fixture](../../tests/nuttx-std/tracker-sim/README.md#headless-one-imu-tracker)

## Dashboard

The CoreS3 opens on the graph. Swipe down to show **IMU 1**, **IMU 2**,
**GNSS** and the pairing control in one row. Select a view, swipe up, or tap
outside the row to hide it.

Each IMU tab plots the three axes of **Accel** or **Gyro** for the last
12 seconds, sampled for display at up to 5 Hz. Readings use the sensor's
own coordinate frame. **GNSS** plots up to 96 recent fixes on an offline,
north-up grid with a distance scale, current coordinates and ground speed.
Loss of fix breaks the trail. This view works without internet access.
The GNSS view overlays a street map when a tile is available.

The dashboard samples the latest observation from each sensor when it redraws
or publishes telemetry. Touch events and sensor faults have their own inbox.
The BLE export receives the full stream before the dashboard downsamples it.
The tracker expires IMU readings after 500 ms and GNSS fixes after three
seconds.

## GNSS street map

Open **GNSS** with a live fix. When a source is available, the screen shows
an OpenStreetMap view with the current position and recent trail. The
offline route plot remains available during fix loss, tile loading, and
network outages.

The tracker starts at zoom 18. Use `--map-zoom 17`, `--map-zoom 16`, or
`--map-zoom 15` for a wider view. Each step up halves the width and
height of geographic coverage. Higher zooms use the same bytes per tile
and can cross more tile boundaries while moving.
The viewport centers on the current GNSS fix even when the tracker is
stationary. A neighboring tile becomes eligible for download as soon as
it enters the viewport, before the tracker crosses into it. When the
tracker is moving, it gives the tile ahead priority after loading the tile
under the marker. Loaded tiles stay visible while the others arrive. If no
matching tile is available, the dashboard uses the route plot.

A transfer carries a lossless row encoding of the 170 × 170 tile. A
fully uncompressed reply, including the palette, row lengths and header,
is 14,666 bytes. The tile matches the CoreS3 map area pixel for
pixel with the menu closed. The tracker keeps up to four complete tiles and
one partial reply. It releases off-screen tiles before a new request and
stores each row losslessly in small blocks to limit heap pressure during
sensor export. The tracker downloads one tile at a time and can start
the next visible tile as soon as the previous one succeeds. Replacing a
request still observes the five-second interval. Tile arrival wakes the
display for a redraw.
Direct IP dialing uses a temporary 8 KiB stack and opens a
separate TLS session for the reply. The phone or relay fetches the PNG
over HTTPS and uses HTTP caching. It lightly sharpens the raster after
resizing to preserve thin labels in the 16-color palette. Tile coordinates
reveal the approximate area of the fix to the tile source. Downloads are
limited to the open GNSS viewport.

[OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/)

### Bluetooth through the phone

Pair in the updated Nxrs Home app and leave Motion Tracker open on the
phone. It uses its internet connection to fetch the visible tile and
returns it over the existing authenticated BLE/TLS connection. The iPhone
can use Wi-Fi or mobile data. Older companion clients can continue to read
telemetry from the tracker.

### Direct Wi-Fi or cellular

For operation without a phone, run the IPv4 relay inside your private
network. The tracker uses normal IP routing, so the same endpoint works
over an already connected Wi-Fi interface or Stamp PPP. The CoreS3
dual-IMU profile currently leaves onboard Wi-Fi disabled. Its Stamp
profile provides the direct bearer.

```sh
python3 -m pip install 'Pillow>=10,<14' 'requests-cache>=1.2,<2'
python3 tools/tracker-map/relay.py --bind RELAY_IPV4 --cert cert.pem --key key.pem
openssl x509 -in cert.pem -outform DER | openssl dgst -sha256
```

Use a named-curve P-256 certificate and provision the fingerprint
independently. Limit access to trusted devices at the network boundary.
Tile requests authenticate the relay by pin and carry no client credentials.
On the Stamp profile, use the relay with or without a telemetry collector.

```text
motion_tracker --cellular-apn YOUR_APN --map-relay IPV4:8444 --map-pin SHA256
```

When the phone is paired and connected, the tracker tries it first. It
uses the configured IP relay if the phone is absent, reports a fetch error,
or runs past the 25-second tile deadline.

### Tile protocol

The tile ID is the OpenStreetMap zoom and XYZ coordinates. Zoom is 15
through 18. Both coordinates are unsigned big-endian integers less than
`2^zoom`.
On BLE, the tracker includes an optional `"tile":[zoom,x,y]` field in its
dashboard snapshot. On the notification service, it sends tile requests
after the client's existing sensor export request. The companion returns
each tile on the same TLS connection while the tracker sends sensor
batches in the other direction. The legacy dashboard service also accepts
tile replies. Over IP, the tracker opens one pinned TLS 1.3 connection per
request, writes the 13 bytes `NXM5 | zoom:u8 | x:u32 | y:u32`, and reads one
response. Both paths use the same response bytes.

| Bytes | Meaning |
| --- | --- |
| 0–12 | Request header, including zoom and the requested X and Y |
| 13 | Status byte. Zero is success, one is temporarily unavailable |
| 14–45 | On success, 16 big-endian RGB565 palette entries |
| 46–215 | 170 row lengths in bytes, north row first. Each length is 1–85 |
| 216 onward | Rows in order, with no padding. An unavailable response has no body |

A row of 85 bytes packs the left pixel in the high nibble and the right
pixel in the low nibble. Shorter rows encode runs. Each byte stores the
run length minus one in the high nibble and the color index in the low
nibble. Expand each row to exactly 170 pixels. Split writes into TLS
chunks of at most 1,024 bytes. The tracker renders complete responses
matching its viewport. USB and cellular telemetry omit the map field.

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

Use `set RUST_BACKTRACE full` in NSH to print raw Rust panic addresses on
CoreS3. Keep the matching `nuttx/nuttx` ELF from each deployed build for
offline symbolization with `xtensa-esp32s3-elf-addr2line -f -C -e nuttx/nuttx <address>`.

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
starts with each authenticated connection. A 512-sample buffer covers about
2.5 seconds of acquisition at these rates while a connected receiver catches
up. If the buffer fills, the tracker delivers queued samples and reports an
incomplete export. Sensor acquisition and the display continue. The phone
also marks an export incomplete if it detects a sequence gap.

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
service uses suffixes `3420` (service), `3421` (ingress) and `3422` (egress).
Each notification starts with a little-endian 32-bit offset into the
connection's ciphertext. The phone holds later bytes in a 2 KiB window when
a notification goes missing. It delivers contiguous bytes to TLS and reports
the next offset as an ingress write command prefixed with `01`.
Before TLS, the phone tests its direct ATT write size (up to 244 bytes) with
an acknowledged, zero-padded `04` probe. In this mode, each ciphertext
write starts with `03` and a little-endian 32-bit stream offset. The phone
keeps up to four writes in one window and asks for a response on the last
write. The tracker rejects a gap and discards duplicate bytes. A rejected
tail causes the phone to replay the same window. The connection ends if a
write remains unconfirmed for two seconds.

If the tracker rejects the window probe, the phone tries `02` for larger
acknowledged writes. A tracker that rejects both probes uses the original
20-byte write size. Both fallback modes prefix ciphertext with `00`.
The tracker keeps up to 2 KiB of unacknowledged ciphertext. After
180 ms without acknowledgment progress, it resends the oldest notification
first. If the acknowledgment advances only through that notification, the
tracker retries the remaining bytes for receivers that discard them after a
gap. The original dashboard service uses
the unchanged `3400`, `3401` and `3402` UUIDs and handles, and is advertised
in the scan response. Dashboard snapshots retain the v1 NDJSON
layout used by USB and cellular. Each live reading has a monotonic
`received_ms` on the same clock as both IMUs and GNSS. Each has `utc_ms`
in Unix milliseconds after the first valid GNSS UTC solution, or `null`
before that. All three outputs share one UTC offset for the run. This pairs
the receiver's solution time with host reception and includes UART latency.
IMU samples drained in one FIFO read can share a reception timestamp. Use
the source sequence and nominal sampling interval to order those samples.

The notification dashboard advertises `imu_scale_bits`. It contains one
pair of IEEE float32 scale bit patterns per IMU, or `null` for a float-only
source. New clients request timed export with `4e 58 52 53 03`. The batch
starts with `a5 03`, a little-endian 16-bit payload length and a signed
64-bit UTC offset. An offset of `i64::MIN` means UTC is unavailable.
The offset plus the sample's monotonic timestamp gives the absolute time
unless the sample was received before the UTC anchor.

A data batch carries up to 16 records. The low five bits of the tag
identify IMU 1, IMU 2 or GNSS as 1, 2 or 3. The first record for a
source, one after a sequence gap, or one with a time delta above 255 ms
carries a 32-bit sequence and a
64-bit monotonic reception timestamp in milliseconds. When bit 7 is
set, the record instead carries an 8-bit time delta from that source's
previous record in the batch and advances its sequence by one. Bit 5
marks samples received before the UTC anchor. Bit 6 marks six signed
16-bit IMU counts. Multiplying each group of three counts by its scale
reconstructs the same float32 values used by the navigation filter.
The alternate IMU payload is six float32s. Both use acceleration then
gyroscope in X/Y/Z order. GNSS sends float64 latitude and longitude and
float32 ground speed. A five-byte payload contains only the source and
sequence of the first missing sample. Multibyte values are little-endian.

The `NXRS 01` and `NXRS 02` requests still select the original 32-bit
timestamp layouts. Version 1 uses full float records and an empty overflow
payload. Version 2 also accepts delta headers and identifies the first
missing source and sequence.

BLE and cellular share the telemetry stream publisher. Each connection keeps
one frame in progress and coalesces waiting snapshots to the latest value.
Sensor workers update shared latest readings through the observation service.
The dashboard and communications worker read them on independent deadlines.
During full-rate export, the sensor workers also admit ordered samples to the
bounded capture queue. The communications worker encodes each batch after
dequeueing and posts link changes to the dashboard inbox. The dashboard keeps
one strip buffer and runs at the profile's background priority on NuttX.

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

A lost PPP session is torn down and redialed by the cellular HAL, which wakes
communications when link state changes. The tracker reconnects a lost collector
session over the existing link when possible. Retry delays increase from one to
at most 30 seconds. The tracker keeps the latest snapshot and one record
in flight. Samples superseded during an outage are skipped. BLE and cellular
share one TLS owner task while acquisition and the dashboard continue independently.
The Stamp profile uses bounded NuttX TCP write buffering. The unbuffered sender
waits for acknowledgments even on a nonblocking socket, which can delay BLE
when TCP retransmits. The buffered sender queues writes in a fixed kernel pool.
A modem that cannot recover command mode reports the fault and stops redialing.

The CoreS3 tracker profiles enable an eight-second hardware watchdog. The tracker
feeds it after completing a report iteration and disarms it after its workers
stop. Missing GNSS fixes and cellular retries leave that health check active.
The interactive image boots to NSH after a reset. The next tracker launch logs
the hardware reset cause and can start a fresh BLE pairing session.

The [simulator qualification](../../tests/nuttx-std/tracker-sim/README.md)
exercises the full tracker and a local collector over PPP. The
[CoreS3 and Stamp test](../../tests/nuttx-std/tracker-hardware/README.md)
checks BLE delivery and TLS recovery on a real carrier. That test has not yet
been run on hardware.

## Record sensor data on iPhone

Connect from Motion Tracker and wait for **Sensor export**. Tap **Record Sensor
Data**, then **Stop Recording** and **Share Last Recording**. Use **Browse
Recordings** to share previous files. Recordings can contain precise location
and stay in the tracker’s local app storage until shared.

The tracker Swift module writes decoded full-rate measurements as versioned
NDJSON. Each sample carries its stream identity, tracker reception time,
sequence and sensor values. UTC is included when available. Sequence gaps and
connection interruptions are recorded explicitly. An interrupted recording can
resume on the same tracker clock and is marked incomplete when saved.

[Replay the file through the NuttX IMU and GNSS device models](../../tests/nuttx-std/tracker-sim/README.md#replay-an-iphone-recording).
