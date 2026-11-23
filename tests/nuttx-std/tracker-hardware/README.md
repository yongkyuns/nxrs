# CoreS3 Motion Tracker cellular dropout test

**Physical validation pending.** This procedure has yet to run on a CoreS3
with a soldered Stamp and a live carrier. The host tests exercise the bench
logic. The [simulator test](../tracker-sim/README.md) covers BLE and PPP in NuttX
with an emulated modem and peer.

## Setup

- Use the CoreS3, M135 and S003 Stamp CatM with an active LTE-M MicroSIM and
  IPv4 APN. Disconnect USB and battery power before fitting the Stamp. On the
  M135 V1.0, close JP2 for Stamp TX to CoreS3 GPIO7 and JP4 for Stamp RX from
  GPIO13. Leave JP1 and JP3 open. Keep GNSS on M5-Bus pins 15 and 16 and SW4
  at `0x68`. Fit the LTE antenna before powering the stack.
  [M135 schematic](https://m5stack-doc.oss-cn-shenzhen.aliyuncs.com/561/SCH_Module_GNSS_V1.0.pdf) · [Stamp CatM hardware](https://docs.m5stack.com/en/stamp/Stamp_CatM)
- Use a Mac with Bluetooth and Python 3.13+ built with OpenSSL PSK support.
  The BLE probe uses the same TLS 1.2 PSK and GATT characteristics as the iPhone
  app. It keeps one connection and exits on disconnect. The collector needs
  OpenSSL and an IPv4 TCP endpoint the SIM can reach. Use a private APN route
  or a firewall-restricted test endpoint. A TCP forward can reach the
  collector's default loopback listener. Enforce access at the network because
  the pinned protocol authenticates the collector to the tracker.

From the repository root, build the production image and prepare the host
tools. Activate the virtual environment and set `BENCH` in each host terminal.

```bash
cargo nxrs fetch tls
cargo nxrs build --app motion-tracker --profile m5stack-cores3-stamp-catm --out target/cores3-stamp

umask 077
BENCH=tests/nuttx-std/tracker-hardware
python3 -m venv target/cores3-bench
source target/cores3-bench/bin/activate
python -m pip install -r "$BENCH/requirements.txt"
python -c 'import ssl; assert ssl.HAS_PSK'
```

## Back up and flash

Keep the backup private. Identify the CoreS3 port with
`python -m serial.tools.list_ports -v`. Hold the CoreS3 download/reset button
until the green LED lights, then release it. Set `PORT` to the identified port
and confirm `flash-id` reports 16 MB before reading flash. The image
starts at `0x0` and fits below the settings partition at `0xf00000`.

```bash
esptool --chip esp32s3 --port "$PORT" --before no-reset --after no-reset-stub flash-id
```

```bash
BACKUP=$(mktemp -d "${TMPDIR:-/tmp}/cores3-before-cellular.XXXXXX")
esptool --chip esp32s3 --port "$PORT" --before no-reset --after no-reset-stub read-flash 0x0 0x1000000 "$BACKUP/core.bin" &&
  esptool --chip esp32s3 --port "$PORT" --before no-reset --after no-reset-stub verify-flash 0x0 "$BACKUP/core.bin" &&
  shasum -a 256 "$BACKUP/core.bin" > "$BACKUP/core.sha256"
```

After backup verification succeeds, write and verify the image.

```bash
shasum -a 256 -c "$BACKUP/core.sha256" &&
  esptool --chip esp32s3 --port "$PORT" --before no-reset --after no-reset-stub write-flash 0x0 target/cores3-stamp/nuttx/nuttx.bin &&
  esptool --chip esp32s3 --port "$PORT" --before no-reset --after watchdog-reset verify-flash 0x0 target/cores3-stamp/nuttx/nuttx.bin
```

For rollback, enter download mode and use the write and verify commands with
`"$BACKUP/core.bin"` after confirming its digest.

## Run one outage

Start a fresh collector and capture log. Its first line reports the port and
certificate pin. If running it on a separate host, copy `bench.py`, use that
host's Python and bind to the test interface with `--listen IPV4`. Restarting
the collector generates a new certificate and pin.

```bash
python "$BENCH/bench.py" collector --port 8443 | tee target/collector-1.jsonl
```

On the CoreS3 USB console
(`python -m serial.tools.miniterm --dtr 0 --rts 0 --eol CR --raw "$PORT" 115200`),
run this command with the carrier APN, reachable IPv4 endpoint, and pin.

```text
motion_tracker --ble --interval-ms 500 --stream-usb --cellular-apn APN --collector IPV4:PORT --collector-pin SHA256
```

Once the collector receives data and USB shows two live IMUs, open the Mac BLE
probe and use a local photo of the pairing QR code or omit `--qr-image` for a
hidden prompt. Keep the QR photo and any raw console logs local. USB frames can
contain location. The probe and collector logs retain only timing and IMU
sequence metadata from telemetry.

```bash
python "$BENCH/bench.py" ble --seconds 300 --qr-image /path/to/local-qr.jpg | tee target/ble-1.jsonl
```

1. Collect at least 15 seconds of live BLE and TLS telemetry. Check for
   cellular `Online` and `collector TLS online` in the USB console.
2. Use a carrier-side data disconnect on a controlled APN or passive
   attenuation of the LTE antenna while it remains connected. Keep CoreS3
   power, BLE and the collector unchanged. At cellular `Failed(LinkLost)`,
   note the latest USB IMU 1 `received_ms` as `LOSS_MS`. Hold the outage for
   at least 15 more seconds. Confirm new uploads stop while BLE continues.
3. Note the latest USB IMU 1 `received_ms` as `RESTORED_MS` immediately before
   restoring cellular service. Verify a new cellular `Online` and
   `collector TLS online`. Keep BLE and uploads running for 15 seconds, then
   stop the probe and review the result.

```bash
python "$BENCH/bench.py" report --ble target/ble-1.jsonl --collector target/collector-1.jsonl \
  --loss-ms "$LOSS_MS" --restored-ms "$RESTORED_MS"
```

Pass requires the console to confirm PPP loss and recovery. The report
requires one authenticated BLE session, advancing readings from both IMUs,
at least four BLE frames in each phase, and a maximum host receive gap of
2.5 seconds. A new TLS session must deliver an IMU sample generated after
restoration while BLE is still active. Record the recovery time and fault
method. Repeat for three independent runs with fresh captures.

If cellular data goes silent and NuttX still reports PPP online, record it as
an IP blackhole. Blocking the collector alone exercises TLS reconnect over
existing PPP. A CoreS3 reset invalidates the BLE continuity measurement.

## iPhone full-rate BLE export

With the same qualified cellular collector and the CoreS3 display active,
open Motion Tracker on iPhone and scan the CoreS3 pairing code. Confirm the
acceleration and gyroscope plots respond on both IMUs, and the map shows the
tracker's GNSS fix. Observe the **Sensor export** counters for at least 60
seconds after all sources are live. Require zero new sequence gaps and zero
reconnects, roughly 6,000 new samples from each IMU and 120 new valid fixes,
and fresh cellular records at the collector throughout the same window.
Record the rates and count deltas without saving location or pairing keys.
