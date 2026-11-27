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

The runtime composes acquisition, screen and communications. The `screen`
module owns gestures, the pairing view, chart history and map presentation,
including projection, viewport selection and the display tile cache. The
`communications` module owns BLE and collector delivery, tile requests and
reply decoding, and the snapshot format used by BLE, TLS and USB diagnostics.

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
seconds. These are presentation freshness budgets, not reasons to terminate
acquisition. `live` means the provider supplies a capture timestamp and its
known age is within budget. `received` means a sample arrived recently but
its measurement age is unknown. The display labels that uncertainty. Waiting
expires if a source never reports; an explicit GNSS no-fix report immediately
removes the current position while retaining the last fix as history.

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

### Direct Wi-Fi

For operation without a phone, run the pinned IPv4 relay on a private,
unmetered Wi-Fi network. The tracker uses the OS default route. A
cellular-capable profile rejects direct relay configuration because that
route could use PPP. The stock CoreS3 profile leaves Wi-Fi disabled. Use
the paired phone for maps on the Stamp profile.

```sh
python3 -m pip install 'Pillow>=10,<14' 'requests-cache>=1.2,<2'
python3 tools/tracker-map/relay.py --bind RELAY_IPV4 --cert cert.pem --key key.pem
openssl x509 -in cert.pem -outform DER | openssl dgst -sha256
```

Use a named-curve P-256 certificate and provision the fingerprint
independently. Limit access to trusted devices at the network boundary.
Tile requests authenticate the relay by pin and carry no client credentials.

```text
motion_tracker --map-relay IPV4:8444 --map-pin SHA256
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
and acquisition can close and reopen. Swipe down and open **GNSS** to see
satellites, signal strength by constellation, estimated accuracy, and PDOP.
The chart shows up to 24 strongest signals, including while the receiver
searches for a fix. Open **Map** for the route and position. For a bench
capture, add `--stream-usb`. That option
includes precise coordinates while a fix is fresh and emits one versioned JSON
object per line. The first and second entries in `imu` are the onboard and
module sensors. Each live entry carries the source sequence, monotonic timestamp
and whether it represents capture or reception. The IMU entries include nominal
sample period, acceleration
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
Version 4 also records receiver no-fix transitions and the first typed terminal
sensor failure. No fix is normal availability information; it does not end IMU
recording. A terminal source failure closes the strict export after its admitted
prefix and refuses another export in that capture lifetime. Acquisition of
unaffected sources and the dashboard continue.

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
layout shared with USB and add the `received` and `unavailable` state values.
Clients with a closed enumeration of the former states need an update; retaining
the older binary layouts does not guarantee those dashboards accept new states.
Data-bearing readings keep the legacy `received_ms` field
on the device's monotonic clock. USB adds `timestamp_us` and `time_source`.
Corrected BMI270 samples and PPS-aligned GNSS fixes use `capture`. Other
readings use `reception`. Each reading has `utc_ms` in Unix milliseconds only
when its capture time is covered by a capture-qualified GNSS UTC mapping.
Reception-only measurements retain `null`: UART receipt does not establish
the solution's capture time. A mapping covers the half-open interval from
its fix timestamp through the three-second GNSS freshness budget. A newer
qualified fix replaces it; no fix or a GNSS fault invalidates the current
mapping. There is no indefinite clock holdover and no claimed oscillator-error
bound. Queued export samples retain the offset valid at admission, so a delayed
fix or clock correction cannot rewrite their UTC. A batch ends before records
with a different known offset.
Binary recordings encode the same monotonic timestamps at millisecond resolution.

The notification dashboard advertises `imu_scale_bits`. It contains one
pair of IEEE float32 scale bit patterns per IMU, or `null` for a float-only
source. The dashboard advertises `export_version: 4`. New clients request
version 4 with `4e 58 52 53 04`; version 3 remains available with the last byte
`03`. A normal batch starts with `a5 04` (or `a5 03`), a little-endian
16-bit payload length and a signed
64-bit UTC offset. An offset of `i64::MIN` means UTC is unavailable.
The offset plus the sample's monotonic timestamp gives the absolute time
unless the record has the no-UTC flag.

A data batch carries up to 16 records. Version 4 uses the low four bits of
the tag to identify IMU 1, IMU 2 or GNSS as 1, 2 or 3. Version 3 uses the
low five bits. The first record for a
source, one after a sequence gap, or one with a time delta above 255 ms
carries a 32-bit sequence and a
64-bit monotonic timestamp in milliseconds. When bit 7 is
set, the record instead carries an 8-bit time delta from that source's
previous record in the batch and advances its sequence by one. Bit 5
marks an unavailable UTC mapping. In version 4, bit 4 marks a receipt timestamp
whose measurement age is unknown and requires bit 5; without bit 4 the timestamp
is capture-qualified.
Version 1–3 records do not prove timestamp provenance. Bit 6 marks six signed
16-bit IMU counts. Multiplying each group of three counts by its scale
reconstructs the same float32 values used by the navigation filter.
The alternate IMU payload is six float32s. Both use acceleration then
gyroscope in X/Y/Z order. GNSS sends float64 latitude and longitude and
float32 ground speed. A five-byte payload contains only the source and
sequence of the first missing sample. Multibyte values are little-endian.

Version 4 adds a no-fix record with tag `04` and no flags: a 32-bit acquisition
generation, a 64-bit monotonic receipt time in milliseconds, and a 64-bit
receiver epoch in milliseconds (`u64::MAX` when unknown). It has no sample
sequence and does not participate in delta coding. A terminal two-byte payload
contains source (1 = IMU 1, 2 = IMU 2, 3 = GNSS) followed by cause:

| Cause | Meaning |
| --- | --- |
| 1 | Unsupported source |
| 2 | Source busy |
| 3 | Acquisition timeout |
| 4 | Sensor continuity/overflow failure |
| 5 | Device I/O failure |
| 6 | Invalid data |
| 7 | GNSS availability evidence lost from the export queue |

The first terminal sensor cause is retained separately from the sample queue.
A five-byte first-missing-sample payload still reports capture queue overflow.
Legacy streams omit no-fix records and close after the admitted prefix when
a sensor fails; they never relabel that failure as queue overflow or complete
recording. Clients that need explicit causes and provenance must request v4.

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

The collector receives v1 NDJSON records with `"kind":"summary"`. Each record
carries two IMU health strings in `imu`, `imu1_received_ms` when live, and the
latest GNSS fix or its health state. Raw IMU samples stay on the local USB and
BLE paths. Map tiles travel over BLE or the unmetered Wi-Fi relay.

By default the tracker starts at most one upload per minute, with a
288-byte limit per record including the newline. That caps summary payload
at about 0.42 MB per day during continuous operation. Carrier usage also
includes TLS, TCP and PPP traffic. Set `--cellular-report-secs 1..86400` to
change this interval independently of the dashboard and BLE cadence.
Reconnects preserve the rate limit and never replay a backlog.

The TLS 1.3 stream is one-way. Use a named-curve P-256 collector certificate
and provision its fingerprint through a trusted channel. The pin authenticates
the collector. Device authentication at the collector requires separate
enrollment. Use `--cellular-apn YOUR_APN` on its own to bring up PPP for diagnostics.

A lost PPP session is torn down and redialed by the cellular HAL, which wakes
communications when link state changes. The tracker reconnects a lost collector
session over the existing link when possible. Retry delays increase from one to
at most 30 seconds. The tracker samples current readings when a report is due
and keeps one record in flight. BLE and cellular
share one TLS owner task while acquisition and the dashboard continue independently.
The Stamp profile uses bounded NuttX TCP write buffering. The unbuffered sender
waits for acknowledgments even on a nonblocking socket, which can delay BLE
when TCP retransmits. The buffered sender queues writes in a fixed kernel pool.
A modem that cannot recover command mode reports the fault and stops redialing.

The CoreS3 tracker profiles enable an eight-second hardware watchdog. The tracker
feeds it after completing a supervisor iteration. Missing GNSS fixes, contained
sensor faults, and cellular retries leave that health check active. Shutdown
requests all owners to stop before joining any, retries failed cancellation
requests at most three times, and feeds only during one 12-second software
cleanup budget. The modem's 10.2-second command-mode cleanup fits inside that
budget. The tracker disarms only after all acquisition, touch and communications
owners have retired. Incomplete shutdown leaves the hardware timer armed.
The interactive image boots to NSH after a reset. The next tracker launch logs
the hardware reset cause and can start a fresh BLE pairing session.

### Failure containment and remaining qualification

Returned display, touch and USB diagnostic errors disable the affected optional
capability. They do not terminate sensing. Dashboard `output_faults` is a sticky
bit mask: display = 1, touch = 2, USB = 4, communications = 8; an absent field
means zero. It describes faults observed during this run, including a capability
that later recovered. Sensor state is reported separately. Native calls that
block the supervisor still rely on the hardware watchdog; returned-error
containment is not a worst-case native-call timing guarantee.

Cellular admission retries Busy/I/O at most three times with one- and two-second
delays. Invalid configuration, unsupported hardware and a quarantined modem
remain unavailable. This does not prevent BLE pairing. A poisoned TLS identity
is replaced only after every session borrowing it retires, with retry delays
bounded at 30 seconds and a fresh pairing key. Map retry and unfinished dial
deadlines remain in the communications wait set.

BLE delivery is bounded by actual provider progress when available. Native
cumulative acknowledgments or legacy reads advance an opaque progress marker;
newly queued bytes and retransmissions do not. An authenticated stream with
pending output and no progress for 12 seconds is retired. A continuously busy
stream whose acknowledgments advance is healthy. Providers without progress
evidence bound a stalled local write without claiming peer acknowledgment.
Transport progress proves consumption at the link, not durable phone recording.

BMI270 ambiguous FIFO snapshots retry on cancellable delivery cadence until
the declared FIFO retention budget expires. Overflow and permanent device I/O
remain terminal for the acquisition lifetime: there is no silent FIFO flush or
sequence reset. Reuse requires a fresh owner after successful cleanup; failed
cleanup keeps the endpoint quarantined. This patch does not automatically reopen
a terminal sensor, enable unattended startup, or qualify a new interrupt/mutex
worst-case timing bound. The host tests exercise these decisions; target builds,
resource comparison and the physical tests below remain separate evidence.

Joined IMU sessions retain acquisition and cleanup errors independently. The
navigation owner preserves that evidence after releasing the session handle,
and the tracker reports both. The stream's first terminal cause remains the
acquisition error when both fail. Startup errors still use the existing single
error return, and cleanup detail retains one aggregate cleanup cause rather
than a per-register failure history.

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

The tracker Swift module writes decoded full-rate measurements as version-2
NDJSON. Each sample carries its stream identity, monotonic `timestamp_ms`,
`timestamp_provenance` (`capture`, `received`, or legacy `unknown`), sequence
and sensor values. UTC is included when supported by the record's evidence.
No-fix transitions retain generation, receipt time and receiver epoch separately
from measurements. A terminal source failure is written before an incomplete
footer, even if no measurement was admitted. Sequence gaps and connection
interruptions are recorded explicitly. An interrupted recording can resume
on the same tracker clock and remains incomplete when saved.

The native replay fixture has a narrower contract than recording. It preserves
legacy v1 arrival-scheduling playback and accepts complete v2 capture-qualified
measurement histories. It rejects v2 histories whose no-fix, receipt-only,
unknown-timing or terminal-failure evidence the native device models cannot
faithfully reproduce.

[Replay the file through the NuttX IMU and GNSS device models](../../tests/nuttx-std/tracker-sim/README.md#replay-an-iphone-recording).
