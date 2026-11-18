# Matter service foundation

The external Matter fixture uses the gateway’s durable identity store, detection
subscription and shared encoded video input.
[Reproduction and host evidence](../tests/matter/README.md).

## Configure the bridge

Prepare the `catalogue` SDK fixture and start its camera executable with
`--config` and an absolute path to a private JSON file. This variant exposes
one existing catalogue camera and its occupancy sensor. The executable uses
the configured IPv4 address for the WebRTC socket and its ICE candidate.

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
  "state_directory": "/absolute/private/matter-state"
}
```

For the `lan-interface` variant, choose a LAN with IPv6 for Matter discovery
and control. Set `ice_address` to its IPv4 address for media. If `lan_interface`
is omitted, the bridge resolves that address to one LAN interface. An explicit
interface must own the address. DNS-SD, IPv4 and IPv6 Matter transport, and
WebRTC sockets all use the resolved interface. Keep the controller reachable
through the selected LAN. [Isolation test](../tests/matter/README.md#lan-interface-isolation).
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

The `snapshots` and later variants also need an absolute `snapshot_ffmpeg` path in
that JSON. It captures JPEGs from the same authorized H.264 reader used for
live playback. The gateway allows one capture at a time with a one-second
interval between captures. It limits JPEG output to 48 KiB, checks live privacy
and publication identity through delivery, and reaps the encoder before
releasing the capture slot. [Snapshot test](../tests/matter/README.md#matter-jpeg-snapshots).

For source-side microphone control, use the `capture-privacy` SDK variant
and pass `--microphone` to `stage_matter.py`. Add
`"microphone_control": {"address": "127.0.0.1:18081", "token_file": "/absolute/private/microphone.token"}`
to the bridge deployment JSON, substituting the gateway's internal listener
and staged credential. Ingest must use that listener too. The controller
credential applies to one camera. The bridge renews a 1.5-second capture
permit only while a fabric is commissioned and the persisted microphone
setting allows capture. Ingest reads the permit using its own publisher
identity. [End-to-end witness](../tests/matter/README.md#microphone-capture-control).

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
webrtc_camera --config "$deployment" --fabric-status
webrtc_camera --config "$deployment" --remove-sole-fabric "$fabric_index"
webrtc_camera --config "$deployment" --open-commissioning-window
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
endpoint grant also ended its pending and active offers. Joining all retired
session resources under load still needs qualification.

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
