# Matter gateway

The Matter gateway exposes one existing Nxrs camera and its occupancy sensor.
It uses the gateway's durable identity store, detection subscription and
shared encoded video input. [Host qualification](../tests/matter/README.md).
[Architecture and application integration guide](integration.md).

The current ESP32 camera uses a video-only media stream. Its Matter Camera
configuration is for development testing. Production use requires qualified
microphone capture, product-issued attestation and device conformance.
[Camera qualification requirements](../tests/matter/conformance.md).

## iPhone compatibility on the LAN

In the November 2026 tests, **Home Assistant** played live video from the
Freenove ESP32-S3 with its OV3660 camera. The iPhone showed the real image
updating. An independent receiver decoded 30 H.264 frames at 320×240, and the
bridge reported zero active WebRTC peers after the iPhone live view closed.
The controller also retrieved two distinct 640×360 JPEGs.

**Google Home** commissioned the development bridge through a Google Matter
hub with a Control Bridge test integration. The iPhone showed both Camera
and Occupancy Sensor tiles, and the sensor displayed Occupied when the test
fixture published that state. The Camera tile offered no playback control.
The bridge received no Camera AV Stream Management or WebRTC commands when
the tile was opened with a generated video source ready.

The Google trial exposed oversized UDP reports during metadata discovery and
after a bridge restart. The SDK patch sizes both report paths for the
exchange's transport, so UDP reports are chunked within the 1,232-byte
datagram budget. TCP keeps its large payload support for camera signaling
and snapshots. Both iPhone trials used development attestation on the LAN.
[Pairing and UDP regression tests](../tests/matter/README.md#pair-from-a-phone-on-the-lan).

## Build the gateway candidate

Use Python 3.12, a local rs-matter Git checkout containing the pinned
revision, the two crate archives from [the dependency pins](../tests/matter/versions.json),
and that file's Rust toolchain. Build from the committed Nxrs sources.

```sh
python3.12 tools/build-matter-gateway.py "$new_build_directory" \
  --sdk "$rs_matter_checkout" \
  --webrtc-archive "$str0m_archive" --dnssd-archive "$astro_dnssd_archive"
```

Pass `--offline` when the lockfile dependencies are cached. The build uses
the `capture-privacy` variant in a disposable checkout and writes
`nxrs-matter-camera` and a `build.json` receipt. Verify the executable SHA-256
against the receipt before copying the binary to the gateway host. The
receipt identifies both source revisions, the prepared lockfile and the
target platform. Pairing material, device state and live services are managed
separately.

On Linux, install the native build and DNS-SD dependencies from the
[Debian qualification instructions](../tests/matter/README.md#reproduce-the-linux-runtime-checks).
The Debian ARM64 trial built with `CARGO_BUILD_JOBS=1`, 5 GiB of RAM and
4 GiB of temporary swap.

## Configure the bridge

Start `nxrs-matter-camera` with `--config` and an absolute path to a private
JSON file. Use provisioned attestation material and specify the gateway
host's wired power source. The executable uses the configured IPv4 address
for the WebRTC socket and its ICE candidate.

```json
{
  "version": 1,
  "site_id": "home",
  "camera_id": "porch",
  "control": "127.0.0.1:18080",
  "rtsp": "127.0.0.1:8554",
  "ice_address": "192.0.2.10",
  "lan_interface": "enp1s0",
  "media_token_file": "/absolute/private/camera-source.token",
  "detection_token_file": "/absolute/private/detections.token",
  "factory_data": "/absolute/private/factory.json",
  "attestation_directory": "/absolute/private/matter-attestation",
  "state_directory": "/absolute/private/matter-state",
  "snapshot_ffmpeg": "/usr/bin/ffmpeg",
  "host_power_source": "ac"
}
```

Choose a LAN with IPv6 for Matter discovery and control. Set `ice_address`
to its IPv4 address for media. If `lan_interface`
is omitted, the bridge resolves that address to one LAN interface. An explicit
interface must own the address. DNS-SD, IPv4 and IPv6 Matter transport, and
WebRTC sockets all use the resolved interface. Keep the controller reachable
through the selected LAN. [Isolation test](../tests/matter/README.md#lan-interface-isolation).

For direct playback through a VPN, the viewer needs a route to Home Assistant
for signaling and UDP access to the bridge's advertised ICE address for media.
Keep Matter commissioning and control on the selected LAN.
[Routed playback test](../tests/matter/README.md#playback-across-a-routed-vpn).

On macOS, Matter TCP binds the configured IPv4 address and the selected
interface's IPv6 addresses at startup. Restart the bridge after those addresses
change. UDP checks the incoming interface, including for multicast traffic.

Stage separate credentials for one existing camera using its catalogue ID.
Use a fresh service identity prefix for each enrollment.

```sh
python3 gateway/tools/stage_matter.py "$control_config" "$new_private_directory" \
  --camera "$camera_id" --identity "$service_identity"
```

Review the staged `control.json` before activating it under
[strict gateway authorization](../deploy/strict-services.md). Preserve the
existing durable deny list during deployment. Set `media_token_file` and
`detection_token_file` in the bridge configuration to the staged
`media.token` and `detections.token` files. The bridge resolves the RTSP path
from the gateway catalogue on reconnect. The detector subscription verifies
the authorized site.

Use `--initialize-state --open-commissioning-window` on first installation
to create the state and pair. Subsequent starts use `--config` alone.
Endpoint assignments and unique IDs persist across catalogue reordering and
path changes. A deployment selecting a different site or camera needs its own
state and enrollment. [Configured bridge test](../tests/matter/README.md#configured-camera-and-durable-endpoints).

### Run as a Linux systemd service

Install the verified executable and the supplied service and account files.
These commands are run from the Nxrs checkout on the gateway host.

```sh
sudo install -D -m 0644 gateway/deploy/nxrs-matter.sysusers /etc/sysusers.d/nxrs-matter.conf
sudo systemd-sysusers /etc/sysusers.d/nxrs-matter.conf
sudo install -D -m 0755 "$gateway_package/nxrs-matter-camera" /usr/local/libexec/nxrs-matter-camera
sudo install -D -m 0644 gateway/deploy/nxrs-matter.service /etc/systemd/system/nxrs-matter.service
sudo systemctl daemon-reload
```

Place `config.json`, the two scoped tokens, factory data and attestation under
`/etc/nxrs-matter/`. Give the `nxrs-matter` account ownership, with mode `0700`
for directories and `0600` for files. Set `state_directory` to
`/var/lib/nxrs-matter/state`. The unit mounts the configuration read-only and
gives the service write access to `/var/lib/nxrs-matter`.

For a new enrollment, start gateway control and MediaMTX so the bridge can
validate the configured camera mapping before creating state. Create the private
parent directory and commission with the service stopped. After pairing, stop
the foreground command and enable the service. Preserve the existing state
directory on subsequent installations or restarts.

```sh
sudo install -d -m 0700 -o nxrs-matter -g nxrs-matter /var/lib/nxrs-matter
sudo -u nxrs-matter /usr/local/libexec/nxrs-matter-camera \
  --config /etc/nxrs-matter/config.json --initialize-state --open-commissioning-window
sudo systemctl enable --now nxrs-matter.service
```

An enrolled bridge can start with gateway control and MediaMTX offline. It
keeps its fabric and camera identity and accepts new streams when its
authorized source returns. A LAN outage can end an active WebRTC session.
Open a fresh stream with the existing enrollment once the link returns.
After the selected LAN IPv4 address changes, update `ice_address` and restart
the bridge with its existing state directory.

Investigate an exited bridge before restarting `nxrs-matter.service`. The
unit preserves its fabric across restart and the gateway control and media
services manage their own processes. The release unit uses the default device
attestation policy. [Service file](../deploy/nxrs-matter.service) ·
[Debian deployment and network recovery results](../tests/matter/evidence.json).

### Run in a macOS login session

Use the same macOS account for foreground commissioning and the launch
agent so both have access to the private files and state. Stop the foreground
bridge after pairing. Save the following as
`~/Library/LaunchAgents/org.nxrs.matter.plist`, replacing the paths with
absolute paths to the verified executable and private configuration.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.nxrs.matter</string>
  <key>ProgramArguments</key>
  <array>
    <string>/absolute/path/nxrs-matter-camera</string>
    <string>--config</string>
    <string>/absolute/private/config.json</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>ExitTimeOut</key><integer>30</integer>
  <key>Umask</key><integer>63</integer>
</dict>
</plist>
```

Load the agent now or let macOS load it when the account next signs in.

```sh
matter_agent="$HOME/Library/LaunchAgents/org.nxrs.matter.plist"
plutil -lint "$matter_agent"
launchctl bootstrap "gui/$(id -u)" "$matter_agent"
launchctl print "gui/$(id -u)/org.nxrs.matter"
```

Use `launchctl bootout "gui/$(id -u)/org.nxrs.matter"` to stop the agent.
Wait for `launchctl print` to report that the service is gone before loading
it again. Keep the same state directory to retain the Matter enrollment.

### Capture and privacy

An absolute `snapshot_ffmpeg` path provides JPEGs from the same authorized H.264 reader used for
live playback. The gateway allows one capture at a time with a one-second
interval between captures. It limits JPEG output to 48 KiB, checks live privacy
and publication identity through delivery, and reaps the encoder before
releasing the capture slot. [Snapshot test](../tests/matter/README.md#matter-jpeg-snapshots).

Software live privacy covers Matter viewers and snapshots. For Matter control
of the encrypted archive, render the strict bundle with
`--recording-volume UUID --recording-privacy`. Stage the camera with
`stage_matter.py --recording` and add
`"recording_control": {"address": "127.0.0.1:18081", "token_file": "/absolute/private/recording.token"}`
to the bridge configuration. Substitute the actual gateway internal listener.
Each camera has its own recorder credential. Managed recording starts private.
An explicit Matter write allows recording and persists the setting. Turning
privacy on confirms native recorder retirement before the write succeeds.
The native permission expires when the bridge stops renewing it. The same
permission controls the gateway detection feed. Closing privacy withdraws its
publisher session and marks Matter occupancy unavailable. Resuming requires
a fresh observation after a new session starts. In-band video annotations and
on-camera processing still need privacy enforcement across the media path.
[Guarded recording qualification](../tests/matter/README.md#guarded-recording-privacy).

For source-side microphone control, pass `--microphone` to `stage_matter.py`. Add
`"microphone_control": {"address": "127.0.0.1:18081", "token_file": "/absolute/private/microphone.token"}`
to the bridge deployment JSON, substituting the gateway's internal listener
and staged credential. Ingest must use that listener too. The controller
credential applies to one camera. The bridge renews a 1.5-second capture
permit only while a fabric is commissioned. Ingest uses its own publisher
identity and reports confirmation after the authenticated camera acknowledges.
Mute closes audio admission while that confirmation is pending. Unmute keeps
the publication silent until the camera confirms and the gateway saves the
setting. A pending or unavailable state returns Matter Busy. Mute remains
requested after a timeout. Update control and ingest before enabling this
configuration on the bridge.
[End-to-end witness](../tests/matter/README.md#microphone-capture-control).

The `shutdown-drain` variant bounds pending offers, ICE and outbound signaling.
SIGTERM and SIGINT close peer admission and join the snapshot worker. The
bridge keeps Matter transport running for the cancellation reply and a
bounded cleanup window before exit.
[Lifecycle test](../tests/matter/README.md#negotiation-deadlines-and-graceful-shutdown).

The `final-fabric` variant retires stored Matter installation records in the
same commit as the last operational fabric. Factory onboarding and configured
endpoint identity survive, along with the node's rollback-protection counters.
Commissioning after removal requires a new operator-opened window after
restart. [Removal test](../tests/matter/README.md#last-fabric-removal-and-restart).

The `commissioning-recovery` variant stores the new fabric and its allocation
cursor in one durable write. If a controller misses the confirmation, an
already enrolled administrator can remove the unused fabric through Matter.
For a failed first enrollment with no reachable administrator, stop the
bridge and use the same deployment configuration to inspect the saved indices.

```sh
nxrs-matter-camera --config "$deployment" --fabric-status
nxrs-matter-camera --config "$deployment" --remove-sole-fabric "$fabric_index"
nxrs-matter-camera --config "$deployment" --open-commissioning-window
```

Run the removal command only for the operator-confirmed abandoned enrollment.
It requires the exact sole index and exclusive access to the state directory.
These maintenance commands do not open Matter or read media credentials.
Removal keeps factory onboarding and both endpoint identities.
[Recovery test](../tests/matter/README.md#interruption-during-commissioning).

## Gateway services

For each camera, run one `media::Reader` on the owning executor. It opens a
credentialed loopback RTSP/TCP connection and forwards H.264 pictures with their
source timestamps. Each viewer subscribes through `Video`. Eight access units
are shared across viewers. A lagging viewer resumes at a new keyframe. Source
loss or replacement ends viewers of the retired source. Matter offer handlers
use `subscribe_current()` at admission. It requires a validated H.264 keyframe
and binds the peer to that publication while it waits for SDP or ICE. The
next publication requires a fresh offer. The caller can cancel
the reader by dropping its future. Give it a strict gateway `camera_source`
credential scoped to the camera. The gateway reconciles active RTSP sessions
against its durable denials. The reader also rechecks the same path with RTSP
DESCRIBE every three seconds, allowing two seconds for a response. This check
opens no media track. Failure retires the source even if the original media
socket can still deliver video. Revoking the shared source ends every Matter
peer using it. At the Matter layer, removing one commissioned fabric stopped
its active stream while a viewer on the surviving fabric kept receiving frames
from the same reader. With two viewers on one fabric, withdrawing a viewer’s
endpoint grant also ended its pending and active offers. The 24-hour Linux
fixture verified generated-video session teardown under four-viewer load.
[Soak qualification](../tests/matter/README.md#four-viewer-linux-soak).

`occupancy::Feed::open` takes a loopback control address, a camera ID and
a private credential file. Run the subscription on the owning executor. Its
callback receives changes in confirmed presence. On silence, disconnection,
malformed input, revocation or cancellation, it withdraws the last level.
An incomplete event cannot extend freshness. Reconnection consumes the current
snapshot without replaying the source's historical transitions.

`persistence::Store` owns one writer lock and a bounded, versioned snapshot.
`initialize` requires a nonexistent absolute directory; `open` requires valid
existing state. Neither repairs corrupt state by resetting a commissioned node.
Each successful mutation syncs a new snapshot, atomically replaces the old file,
then syncs the directory. A failed mutation makes further access fail until
restart. Callers must retire network sessions before releasing the owner.

`commissioning::FactoryData` reads a private verifier file and binds its
fingerprint to the node store. The responder can authenticate a commissioner
without holding the printed setup code. The host fixture checks the scalar
and curve point before creating state, and the SDK owns the window timer
and multicast advertisement. [Commissioning and restart test](../tests/matter/README.md#operator-initiated-commissioning).

Linux and macOS use std file operations with private file/directory permissions,
exclusive ownership and link checks. Other OS bindings return unavailable;
the snapshot format remains portable. The trusted local filesystem must preserve
file locks, atomic rename and sync semantics. State is private, not encrypted;
the checksum detects corruption, not malicious replacement by the same user.

The store creates no workers. It bounds state to 512 keys, 64 KiB per value and
1 MiB total. The macOS fixture restarts independent controllers after a process
interruption during a viewer permission update or a second fabric’s record
deletion. The visible access follows the committed snapshot. Transactions
across SDK records, schema migrations, multi-camera endpoint allocation and
system power loss remain open. [Recovery test](../tests/matter/README.md#interrupted-access-changes).
