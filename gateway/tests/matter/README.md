# Matter camera feasibility fixture

This opt-in fixture tests the protocol choice for [#388](https://github.com/yongkyuns/nxrs/issues/388).
It is separate from the live gateway and camera firmware. The external SDK uses the identity store, occupancy subscription and RTSP reader from
`gateway/matter` through a fixture adapter.
The issue is the canonical architecture and release checklist.

`../matter_reference.py` commissions the rs-matter camera example through the
independent Home Assistant Matter Server, then uses aiortc to decode H.264. It
requires 60 frames with advancing timestamps and changing decoded images, and an
acknowledged Matter session teardown. Reported FPS is **decoded fixture FPS**,
not iPhone rendering or physical camera throughput.
Candidates are removed from both directions of SDP so playback must use the
Matter ICE-candidate commands; inline candidates cannot conceal broken trickle.
`../matter_browser_reference.py` separately exercises Home Assistant's actual
camera dialog in headless Chrome. It checks presented images, H.264 reception
and browser peer closure. Neither witness establishes physical iPhone behavior.

## Pins and local corrections

`versions.json`, the Cargo/npm lockfiles and receiver constraints record the
tested dependencies. Rust 1.98.1 is explicit for this fixture; Nxrs firmware and
the existing gateway retain their toolchains. The upstream workspace has no
lockfile, so the captured Cargo.lock is necessary for a repeatable resolution.

`rs-matter-camera.patch` applies to the exact SDK revision in `versions.json`:

- Preserve the media-section index in outbound ICE candidates. The original
  SDK emitted null for both identifiers; the independent receiver rejected it.
- Read revision-2 stream lists, retaining the handler's single-stream limit.
  Empty, malformed and multiple-stream lists fail rather than silently choosing.
- Implement the example's missing camera-initiated offer/answer flow. Retain the
  locally created media ID; it does not produce a remote `MediaAdded` event.
- Replace the example's in-place blob files with the Nxrs snapshot store. Require
  explicit initialization and terminate the fixture after a storage failure,
  including failures that an SDK caller might otherwise ignore.
- Accept the browser's audio-first/video-second layout, reject unused audio,
  and keep the video's actual media-section index in trickled candidates.

`str0m-rejected-media.patch` corrects the published WebRTC engine's empty format
list on rejected media. [RFC 3264 section 6](https://www.rfc-editor.org/rfc/rfc3264#section-6)
requires a format even when the port is zero. Retain the first offered format;
do not negotiate that codec or echo all unsupported audio codec names. The
preparer verifies the published crate checksum and patches a private copy,
leaving Cargo's shared source cache untouched.

The patch changes Apache-2.0 upstream sources and retains their original notices.
These are qualification patches, not a vendored production Matter runtime.
Two SDK protocol tests and two media-layout tests cover the corrections.

## Reproduce on macOS

Use Python 3.12, Node 24.22.3, Rust 1.98.1 and FFmpeg with libx264. From the
repository root, choose a fresh SDK checkout and a private runtime directory:

```sh
nxrs_root="$PWD"
matter_fixture="$nxrs_root/gateway/tests/matter"
matter_sdk="$(mktemp -d)/rs-matter"
matter_runtime="$(mktemp -d)"
chmod 700 "$matter_runtime"
git clone https://github.com/project-chip/rs-matter.git "$matter_sdk"
git -C "$matter_sdk" checkout a530a2972b860162527a9ec9df4ee62156b0ad69
curl --fail --location https://static.crates.io/crates/str0m/str0m-0.18.1.crate \
  --output "$matter_runtime/str0m.crate"
python3.12 tools/prepare-matter-fixture.py --sdk "$matter_sdk" \
  --webrtc-archive "$matter_runtime/str0m.crate"
cargo +1.98.1 build --locked --manifest-path "$matter_sdk/Cargo.toml" \
  -p rs-matter-examples --bin webrtc_camera --features webrtc,astro-dnssd
cargo +1.98.1 test --locked --manifest-path "$matter_sdk/Cargo.toml" \
  -p rs-matter-dm --lib
cargo +1.98.1 test --locked --manifest-path "$matter_sdk/Cargo.toml" \
  -p rs-matter-examples --bin webrtc_camera --features webrtc,astro-dnssd
cp "$matter_fixture/package.json" "$matter_fixture/package-lock.json" "$matter_runtime/"
npm ci --prefix "$matter_runtime" --ignore-scripts
python3.12 -m venv "$matter_runtime/venv"
"$matter_runtime/venv/bin/pip" install \
  -r "$nxrs_root/gateway/tests/media-requirements.txt" \
  -c "$matter_fixture/receiver-constraints.txt"
mkdir "$matter_runtime/device-tmp" "$matter_runtime/controller"
ffmpeg -hide_banner -loglevel error -f lavfi \
  -i testsrc2=size=1920x1080:rate=30 -t 2 -c:v libx264 -threads 2 \
  -preset ultrafast -tune zerolatency -profile:v baseline -g 30 -bf 0 \
  -x264-params repeat-headers=1:aud=1 "$matter_runtime/fixture.h264"
```

Start these processes in separate terminals with the same paths. Substitute the
actual LAN interface for `en1`. First verify ports 5540 and 15580 are free. The
example uses public test attestation credentials and is only a lab fixture.
Keep its logs and state private: SDK/controller logs can contain pairing and
session secrets. Do not use existing home enrollment or gateway state.

```sh
"$matter_runtime/node_modules/.bin/matter-server" \
  --storage-path "$matter_runtime/controller" --primary-interface en1 \
  --port 15580 --listen-address 127.0.0.1 --enable-test-net-dcl --dcl-offline \
  --disable-ota --disable-thread-diagnostics > "$matter_runtime/controller.log" 2>&1
```

```sh
TMPDIR="$matter_runtime/device-tmp" WEBRTC_CAMERA_STATE="$matter_runtime/state" \
  WEBRTC_CAMERA_H264="$matter_runtime/fixture.h264" \
  WEBRTC_CAMERA_FPS=30 RUST_LOG=warn "$matter_sdk/target/debug/webrtc_camera" \
  --initialize-state \
  > "$matter_runtime/device.log" 2>&1
```

Commission only this fixture into an empty isolated controller using its LAN IP:

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_reference.py \
  --test-commission-ip <fixture-lan-ip> --flow inventory
"$matter_runtime/venv/bin/python" gateway/tests/matter_reference.py --flow receiver-offer
"$matter_runtime/venv/bin/python" gateway/tests/matter_reference.py --flow camera-offer
```

Restart the fixture with the same private state **without** `--initialize-state`
to check retained commissioning. Initialization refuses an existing directory;
missing or corrupt state is never automatically reset.
Run alternating playback flows at least six times to exceed the four-session
table cumulatively. Stop only these two fixture processes when finished.

## Reproduce the Linux runtime checks

Use the same dependency pins and SDK build command with
`--variant shutdown-drain`. The tested Linux environment was Debian
13 ARM64 in QEMU with native Avahi DNS-SD. On that guest, install these
packages before building the SDK, gateway control and RTSP publisher.

```sh
sudo apt-get install --no-install-recommends \
  build-essential cmake pkg-config libclang-dev \
  avahi-daemon dbus libavahi-compat-libdnssd-dev \
  ffmpeg libavcodec-dev libavdevice-dev libavfilter-dev libavformat-dev \
  libavutil-dev libswresample-dev libswscale-dev
sudo systemctl start avahi-daemon
```

Use the selected guest interface and its assigned address for `--interface`
and `--ip`. Run the [catalogue](#configured-camera-and-durable-endpoints),
[shutdown](#negotiation-deadlines-and-graceful-shutdown),
[snapshot](#matter-jpeg-snapshots), and
[last-fabric](#last-fabric-removal-and-restart) tests sequentially with
`--ffmpeg /usr/bin/ffmpeg` and a fresh runtime directory for each. They occupy
the standard Matter port during their own run. The Linux removal trial uses
the normal command path. The debugger interruption trials use macOS.
[Linux evidence](evidence.json).

## LAN interface isolation

Prepare the SDK with `--variant lan-interface` and provide the published
`astro-dnssd-0.3.10.crate` as `--dnssd-archive`. The preparer verifies its
checksum and applies an interface option to native DNS-SD browse. Build
with `--features webrtc,astro-dnssd` and the same native packages above.

Run `gateway/tests/matter_lan_reference.py` with the
[snapshot test](#matter-jpeg-snapshots) arguments except `--interface` and
`--ip`. Run it in a disposable Linux guest with Avahi and sudo access. It
creates two private veth networks and removes them on exit. It checks
IPv6 multicast discovery and TCP/UDP on both networks, including attempts
to reach the selected addresses through the excluded network. The selected
peer commissions over IPv6, decodes H.264 and reads a JPEG over IPv6 TCP.
A restart without an explicit interface field exercises inference from
`ice_address` with the original enrollment. To reproduce the older
behavior, use the `shutdown-drain` binary and `--expect-unscoped`.
[Results](evidence.json).

## Playback across a routed VPN

In a disposable Debian guest with WireGuard and nftables, run
`gateway/tests/matter_routed_reference.py` with the same fixture arguments as
the [Linux soak](#four-viewer-linux-soak). It uses two private network namespaces.
The Matter controller commissions from the selected LAN; the receiver reaches
its test WebSocket through WireGuard.

The first trial forwards signaling while dropping the camera's WebRTC UDP.
It checks that UDP reached the router and that the unanswered session retires.
The second trial routes media to the same Matter bridge through source NAT.
Both offer directions must decode changing H.264 images and release their
sessions. The VPN viewer cannot connect to the bridge's Matter TCP port.
This qualifies the routed media path on Linux. The Home Assistant iPhone app
on cellular and the deployment's Tailscale routing still need physical tests.

## Four-viewer Linux soak

Build the [gateway candidate](../../matter/README.md#build-the-gateway-candidate)
in the disposable Linux guest. Run `gateway/tests/matter_soak_reference.py`
with the same executable arguments as the LAN test, `--sdk` for the pinned
SDK's test attestation chain, and a fresh runtime directory. The default is
24 hours with samples every 60 seconds. For a rehearsal, add
`--duration 120 --interval 10`.

Four independently commissioned Matter fabrics decode the same H.264
publication over WebRTC. A fifth offer must fail at capacity. The run checks
expiry of an unanswered offer while other peers keep playing, graceful
restart with retained fabrics, replacement of one revoked fabric, and final
release of all media sessions. The surviving viewers must keep frame gaps
under one second during offer expiry and fabric replacement.
`samples.jsonl` records source and per-viewer
decoded FPS, frame gaps, bridge RSS, threads and descriptors. `result.json`
is written after a successful teardown. Keep controller state and raw logs
private. The earlier TLS fixture completed 24 hours at 14.55–15.446 decoded
FPS, and all four viewers recovered in 5.955 seconds after the restart.
The merged release at `fd89e5c7` recovered four viewers in 6.946 seconds in
its two-minute rehearsal.
[Measured results](evidence.json).
This fixture uses generated video in a guest. Physical ESP32 streaming,
iPhone rendering and microphone audio require separate tests.

The isolated packet trace from the `lan-interface` build showed that RTSP
continued while new DNS-SD registration blocked the bridge thread and paused
all three surviving RTP streams. The new variant gives registration and
deregistration to a bounded worker that joins on exit. It also retries an
SDP Answer explicitly rejected with Matter `NotFound`, while the original
session and its permission still apply. The SDK reads the peer's command
status before completing the invoke, so the retry can distinguish this
registration race from transport and authorization failures.

## Automated persistence qualification

With those manual fixture processes stopped, this command owns fresh isolated
controller/device processes and joins them on exit. It refuses occupied ports
and an existing runtime directory. Use the same tool versions as above.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_state_reference.py \
  --device "$matter_sdk/target/debug/webrtc_camera" \
  --controller "$matter_runtime/node_modules/.bin/matter-server" \
  --video "$matter_runtime/fixture.h264" --runtime "$matter_runtime/state-trial" \
  --interface en1 --ip <fixture-lan-ip>
```

It commissions once, decodes after abrupt process termination and restart,
discards an incomplete staging file, refuses a corrupt snapshot, resumes after
restoring its own checkpoint, and forces a real SDK attribute write to fail.
The last case requires the node to stop. Recovery playback has one 30-second
budget, including controller retries. The private runtime retains raw logs and
sanitized `results.json`; only the latter belongs in evidence.

This fixture covers a corrupt snapshot and modeled interrupted staging. For
process loss during actual administrative writes, run the
[access recovery test](#interrupted-access-changes).

## Home Assistant browser qualification

Keep the isolated controller and generated camera running. Use a separate
Python 3.14.10 environment with `homeassistant-constraints.txt` as constraints
when installing Home Assistant 2026.11.2, Playwright 1.65.2,
`matter-python-client` and `matter-ble-proxy`. The installed Chrome version is
recorded in `versions.json`; the witness records the actual browser version.
This pip-based macOS instance is a development fixture, not a qualified
production Home Assistant deployment.

Start Home Assistant with a new private `--config` directory. Enable only
`frontend`, `api`, `config`, `websocket_api`, and `logger`; set HTTP's
`server_host: 127.0.0.1` and `server_port: 18123`. Complete onboarding with a
test-only account and confirm the pending HTTP address within five minutes
(Home Assistant's supported `http/config/promote` WebSocket command). Otherwise
Home Assistant rolls back that address. Add the Matter integration using
`ws://127.0.0.1:15580/ws`, and verify the generated camera is discovered.

Save authentication from an isolated browser context after signing in, then
close it. This state file contains credentials and must remain private:

```sh
python -m playwright open --channel chrome \
  --save-storage "$matter_runtime/browser.json" http://127.0.0.1:18123
python gateway/tests/matter_browser_reference.py --entity camera.acme_test \
  --storage-state "$matter_runtime/browser.json" \
  --output "$matter_runtime/browser-result.json"
```

The witness opens the normal camera dialog without rewriting its offer. It
requires 60 presented frames with changing sampled pixels, records video
dimensions and presentation/decoder counters, and closes the dialog. Chrome
must report increasing media time. For macOS WebKit, use the same environment
to run `python -m playwright install webkit` and pass `--browser-engine webkit`
to the browser witness or to `ingest_av_reference.py` with its browser
arguments. WebKit must report increasing RTP timestamps, compositor frame
counts and presentation times because its live MediaStream callback can
report zero for media time. Repeat to exercise session reuse. The combined
ingest witness also checks release of the Matter session. The physical release
trial measures rendering on the iPhone.

For thumbnail qualification on Home Assistant 2026.11.2, the frontend's
width-only request sends a zero height. The Matter Server API requires every
supplied dimension to be positive. Apply [the thumbnail patch](home-assistant-thumbnail.patch)
to a private copy of the installed `homeassistant/` source tree and run the
fixture with that copy first on `PYTHONPATH`. The patch omits zero dimensions
when Home Assistant constructs the Matter snapshot request.
[Matter Server snapshot API](https://github.com/matter-js/matterjs-server/blob/fc8aa79611aebbe3090a30813e7211f707b2131f/docs/websockets_api.md#camera_snapshot).

Add `--check-thumbnail` to either browser test. It sends the frontend's
`width=1024&height=0` request through the Home Assistant camera proxy and
requires HTTP 200 with a decodable JPEG while the live view is open.

## Bridge and occupancy automation

Prepare a fresh SDK checkout with `--variant bridge`, then rebuild the same
camera binary. Use an empty controller,
fresh fixture state and the same generated video. Set `WEBRTC_CAMERA_TRIAL_DIR`
to a new mode-0700 directory for each process launch. The qualification
binary binds its control socket there. Its inputs specify the detector state
explicitly so the experiment isolates Matter reports and automation behavior.

After commissioning, use the camera and binary sensor IDs discovered in
Home Assistant. Add this automation to the isolated Home Assistant config,
substituting its occupancy entity ID, and load it before running the witness.

```yaml
automation:
  - alias: Nxrs occupancy qualification
    triggers:
      - trigger: state
        entity_id: binary_sensor.occupancysensor_occupancy
    actions:
      - event: nxrs_fixture_occupancy
        event_data:
          sensor: "{{ trigger.entity_id }}"
          observed: "{{ trigger.to_state.state }}"
```

Start with the sensor unavailable. Run the command in the isolated Home
Assistant / Playwright environment. It observes every emitted automation event,
checks for extra transitions and verifies actual camera playback during the
last period of sensor unavailability.

```sh
python gateway/tests/matter_topology_reference.py \
  --sensor binary_sensor.occupancysensor_occupancy --camera camera.camera \
  --control "$WEBRTC_CAMERA_TRIAL_DIR/occupancy.sock" \
  --storage-state "$matter_runtime/browser.json" --output "$matter_runtime/topology.json"
```

To reproduce the negative control, put `Bridged::CLUSTER` before
`Occupancy::CLUSTER` in endpoint 3's cluster list and rebuild. A recovery
with changed occupancy then emits the cached value first and fails the witness.
The positive ordering is specific to the tested report subscription. See
[conformance.md](conformance.md) for remaining topology requirements.

## Occupancy from gateway control

Keep the isolated controller and Home Assistant running with the automation above.
Stop the test device and supply its commissioned **test** state to the witness.
The script starts its own device and gateway, creates a private detection-only
credential and uses the authenticated publisher API to send labelled observations.
Build the gateway with `cargo build --locked --manifest-path gateway/Cargo.toml
-p nxrs-gateway-control` and use the SDK built with the bridge patch.

```sh
python gateway/tests/matter_gateway_reference.py \
  --gateway gateway/target/debug/nxrs-gateway-control --device "$matter_sdk/target/debug/webrtc_camera" \
  --state "$matter_runtime/state" --video "$matter_runtime/fixture.h264" \
  --sensor binary_sensor.occupancysensor_occupancy --camera camera.camera \
  --storage-state "$matter_runtime/browser.json" --runtime "$matter_runtime/gateway-trial"
```

The automation must observe person, expiry, clear from a new publisher session,
loss of the gateway, fresh person after restart and loss of subscriber access
while publishing continues. An update from the retired publisher must receive
HTTP 409. The camera dialog must still present advancing images. Only
`result.json` contains shareable test results.

## Shared live video and source replacement

Prepare a fresh checkout with `tools/prepare-matter-fixture.py --variant rtsp`
using the SDK and WebRTC pins above. Build the camera binary with the same
locked build command. Build gateway control as above and build the [Nxrs RTSP publisher](../../publisher/README.md)
and use the MediaMTX version in `versions.json`. Keep only the isolated
controller and Home Assistant running, with a commissioned test **bridge**
state and the corresponding authenticated browser state. In the Home Assistant
Python environment, run:

```sh
python gateway/tests/matter_rtsp_reference.py \
  --gateway gateway/target/debug/nxrs-gateway-control \
  --mediamtx /absolute/path/to/mediamtx --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --device "$matter_sdk/target/debug/webrtc_camera" --state "$matter_runtime/state" \
  --storage-state "$matter_runtime/browser.json" --camera camera.camera \
  --runtime "$matter_runtime/rtsp-trial"
```

The witness starts a strict Nxrs gateway, MediaMTX and publishers on loopback
with private fixture credentials. Two independent camera dialogs must
present the changing red-marked source through one reader. Removing the
publisher with both dialogs open must retire both peers and clear the Matter
`CurrentSessions` attribute. A fresh dialog must render the blue-marked
replacement at its new dimensions and RTP clock intervals. Stopping the
device must release the reader while the replacement publisher stays live.
Repeat with a fresh runtime per fault. With `--fault control-loss`, gateway
control stops while the source keeps publishing. Both Matter sessions must end,
and restarting the controller must admit a new reader of the same publisher.
With `--fault revoke --receiver-python "$matter_runtime/venv/bin/python"`,
two independent PyAV receivers join the same RTSP publication. One shares the
revoked credential and must lose access. The other must keep decoding.
After a control restart, the revoked credential must receive RTSP 401 while
the authorized receiver can reopen. Only `result.json` is intended for
sharing. The fabric witness below exercises independent administrators. Physical
camera input, product access policy and iPhone rendering still need qualification.

The concurrent Nxrs Home trial used the [ingest A/V fixture](#matter-microphone-mute-and-live-privacy)
with a private HTTPS viewer and a disposable iPhone simulator. The app used
its ordinary enrollment and WHEP playback. While the Home Assistant dialog
presented video, simulator screen captures showed both source marker colors
and MediaMTX showed both readers on one publication. Backgrounding the app
retired its WHEP reader. The physical release trial measures rendering on the
iPhone. [Concurrent playback results](evidence.json).

## Source replacement during negotiation

Prepare the pinned SDK with `--variant stream-continuity`. Run
`gateway/tests/matter_publication_reference.py` with the
[snapshot test](#matter-jpeg-snapshots) arguments and a fresh private
runtime directory. Use `--sdk` with the packaged bridge to provision its
development attestation in the fixture.

The controller decodes a 640×360 publication, leaves a camera offer
unanswered, and withholds ICE candidates on a second session after receiving
the answer. Replacing the publisher must end both negotiations with
`NoUserMedia` and clear the camera’s session list. A new session decodes the
replacement at 320×240. The test kills MediaMTX with that session active and
checks that the peer retires and stops receiving RTP. Both offer commands
return `InvalidInState` during the outage and after the media server starts
with an empty path. A third publication yields 45 decoded frames through
the same enrollment and stable camera identity, with the bridge and
gateway control process still running. The merged macOS package at
`fd89e5c7` recovered in 5.882 seconds and ended with zero sessions.
[Host measurements](evidence.json).

## Configured camera and durable endpoints

Prepare the pinned SDK with `--variant catalogue` and use the build command
above. Run `gateway/tests/matter_catalogue_reference.py` with the same
arguments as the [commissioning test](#operator-initiated-commissioning)
and a fresh runtime directory. The test provisions the deployment JSON
and exercises the gateway's enrollment stager. For the `tls-management`
variant, also supply the pinned checkout as `--sdk` and an absolute
`--ffmpeg` path.

Two publishers have different frame sizes and report independent occupancy.
The controller verifies the selected video, occupancy and nominated ICE
address, then repeats the checks after swapping media paths and reordering
the catalogue. The new credentials are scoped to the selected camera and
existing viewer access still works. The controller checks durable endpoint IDs,
rejects site or camera changes against the original state, and withdraws the
media credential while the selected camera continues to report occupancy.
[Results](evidence.json).

## Session capacity and retirement

Prepare the pinned SDK with `--variant sessions`. This variant uses the
same deployment configuration as `catalogue`. Run
`gateway/tests/matter_session_reference.py` with the arguments used by the
[commissioning test](#operator-initiated-commissioning) and a fresh runtime.

The test commissions two fabrics and fills four slots with two decoding peers
and two unanswered offers. A fifth offer must return `RESOURCE_EXHAUSTED`
with the same camera stream reference count. A successful `EndSession` reply
requires the receiver's nominated camera UDP port to be available for rebinding.
The test opens a replacement peer, removes the second fabric, and verifies that
its UDP port is released while the surviving controller still decodes video.
After closing the remaining sessions, the original stream reference count
must be restored.

The driver polls its peers as an owned group. Each session has one pending
negotiation signal, one ICE signal and one terminal reason. The peer accepts
at most 16 remote ICE candidates within one negotiation.

The packaged bridge binds H.264 negotiation to the current publication’s SPS.
It selects a compatible mode 1 payload in a viewer offer and checks the
receiver’s supported level in the answer to a camera offer. The prepared SDK’s
`browser_offer_tests` check both level rejection and payload selection.

## Matter JPEG snapshots

Prepare the pinned SDK with `--variant stream-continuity`. Run
`gateway/tests/matter_snapshot_reference.py` with the
[commissioning test](#operator-initiated-commissioning) arguments plus an
absolute `--ffmpeg` path and a fresh runtime directory.
For the packaged bridge, pass the pinned checkout as `--sdk` to provision
the test attestation bundle and bind to the selected LAN interface.

Two independently commissioned fabrics request the same JPEG stream. The
first capture holds the encoder while the second fabric receives Matter
`Busy` and live video advances. The first controller decodes a 640×360
JPEG. After the source changes, the second controller decodes a 320×180
JPEG. The test verifies source colors, dimensions, one RTSP reader, and
release of the snapshot allocation.
Separate commands check live privacy, source unavailability and the capture
interval. The fixture pauses a disposable encoder and verifies its process
has exited before a source-loss error returns to the controller.

`CaptureSnapshot` is a large-message command. The fixture limits the JPEG
to 48 KiB within the pinned SDK's 64 KiB interaction buffer.
[Pinned cluster model](https://raw.githubusercontent.com/project-chip/connectedhomeip/v1.5.1.0/src/app/zap-templates/zcl/data-model/chip/camera-av-stream-management-cluster.xml) · [Results](evidence.json).

## Negotiation deadlines and graceful shutdown

Prepare the pinned SDK with `--variant shutdown-drain`. Run
`gateway/tests/matter_shutdown_reference.py` with the
[snapshot test](#matter-jpeg-snapshots) arguments and a fresh runtime.

The bridge gives peers 20 seconds to establish ICE and DTLS, including time
spent waiting for an answer. A single outbound Matter exchange can occupy the
signaling loop for three seconds. Local peer teardown joins before attempting
an outbound End notification.

The test opens an unanswered offer and a separate peer that never supplies ICE,
then pauses their disposable controller. Both peers release their UDP ports and
stream references while the other fabric keeps decoding. The paused controller
cannot acknowledge either End notification. During shutdown, the process has a decoding peer, a
pending offer and an active JPEG encoder. A separate Matter read completes while
the capture is pending. SIGTERM closes admission, retires peers, and reaps
the encoder. The controller must observe capture cancellation, release its
snapshot stream and receive the live peer's Matter `UserHangup` reason.
The bridge gives those replies and notifications one second before emitting
the node's `ShutDown` event, then keeps the transport up for one more second
for subscription delivery. The same publisher and enrolled controller
resume playback after restart.
[Results](evidence.json).

## Last-fabric removal and restart

Prepare the pinned SDK with `--variant final-fabric`. Run
`gateway/tests/matter_final_fabric_reference.py` with the
[snapshot test](#matter-jpeg-snapshots) arguments and a fresh runtime directory.
On macOS, repeat with `--lldb /usr/bin/lldb --breakpoint before-replace` and
`--breakpoint after-sync`, using a different runtime directory for each run.

The controller changes the node label and microphone mute with a subscription
and video session open. Removing a second fabric first preserves those
settings on the surviving fabric. Removing the last fabric deletes its
operational record and the installation's settings and subscriptions together.
An enhanced invitation issued by that fabric also closes while the node stays
up. When an operator explicitly reopens commissioning after restart, a fresh
controller sees the configured endpoint IDs, default label and muted
microphone, and can decode video.

The breakpoint runs kill the isolated device before the removal snapshot is
renamed and after it is synced. The first restores the old fabric, settings
and video session capability. The second leaves it decommissioned and allows
fresh commissioning with the same endpoint identities. The test reads only
record keys from the disposable store. [Results](evidence.json).

## Interruption during commissioning

Prepare the pinned SDK with `--variant commissioning-recovery`. Run
`gateway/tests/matter_commissioning_recovery_reference.py` with the
[snapshot test](#matter-jpeg-snapshots) arguments plus `--lldb /usr/bin/lldb`.
Use a fresh runtime directory for each of `--boundary before-replace` and
`--boundary after-sync`. Repeat both with `--existing-fabric` to exercise a
bridge that already has an administrator.

The test kills the bridge at the actual `CommissioningComplete` storage
commit, before any response. Before replacement, the new fabric and allocator
cursor remain unchanged. After sync, both survive. When the first controller
misses the confirmation and has no device to manage, local recovery refuses
a running bridge or the wrong index and permits an explicit sole-fabric
removal with the media credential unavailable. With two fabrics, local
recovery refuses removal. The surviving administrator can remove the unused
one and commission a replacement. The test decodes video after recovery.
[Results](evidence.json).

## Timestamped audio/video publication

Run this isolated host test with the pinned receiver environment, FFmpeg with
libopus and libx264, the strict gateway, MediaMTX and the SDK publisher.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_audio_reference.py \
  --gateway gateway/target/debug/nxrs-gateway-control --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --mediamtx /absolute/path/to/mediamtx --runtime "$matter_runtime/av-publication"
```

The generator flashes an image and sounds a 1 kHz tone at unequal intervals.
Both tracks use a common source timeline. The test sends Opus first and H.264
second in MPEG-TS. An independent PyAV receiver decodes both tracks from one
authenticated RTSP publication and checks the tone, all pulse intervals, the
decoded frame cadence and the relative cue times. This measures timing at the
decoder. Physical microphone input, client presentation and Matter audio
allocation have their own release checks.

## Microphone allocation commands

Prepare a fresh pinned SDK checkout with `--variant audio-control`, then build
the camera example with the locked command above. This qualification variant
adds a microphone format and the two allocation commands to the camera
endpoint. Use the receiver environment with the pinned Node binary on `PATH`.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_audio_allocation.py \
  --controller "$matter_runtime/node_modules/.bin/matter-server" --device "$matter_sdk/target/debug/webrtc_camera" \
  --gateway gateway/target/debug/nxrs-gateway-control --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --mediamtx /absolute/path/to/mediamtx --runtime "$matter_runtime/audio-allocation" --interface en1 --ip <fixture-lan-ip>
```

An independent controller reads `MicrophoneCapabilities`, allocates the
advertised live-view format, checks the device's stream table and releases
the ID. The paired gateway reader requires timed Opus packets and an authorized
H.264 keyframe. The fixture's configured capture format
is 16 kHz mono, 16-bit. It admits a 64 kbit/s stream. The test repeats the
request while the source is offline, after video-only replacement and after
revoking the reader while publication continues. The report contains public
format and control results.

## Matter microphone playback

Prepare and build the SDK with `--variant audio-playback`. Run the microphone
allocation command above with `--playback` and a fresh runtime directory.
The independent receiver negotiates both offer directions, decodes a known
1 kHz pulse train alongside changing images, and attempts to deallocate the
microphone during playback. A third active session loses both tracks after
reader revocation while the publisher continues.

For the browser trial, start the isolated Home Assistant and Matter Server
described above with their commissioned test enrollment. Leave the test node
stopped. The following command starts it with its saved state and a fresh
gateway, opens the normal camera dialog, and tears down its own processes.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_audio_dialog.py \
  --state "$matter_runtime/state" --device "$matter_sdk/target/debug/webrtc_camera" \
  --gateway gateway/target/debug/nxrs-gateway-control --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --mediamtx /absolute/path/to/mediamtx --runtime "$matter_runtime/audio-dialog" \
  --browser-python "$home_assistant_venv/bin/python" --storage-state "$matter_runtime/browser.json" \
  --camera camera.camera
```

The browser checks incoming H.264 and Opus, one synchronization context with
distinct tracks, 60 presented images and at least six light and tone pulses.
It measures the tone from the incoming audio track with Web Audio and the
light from the displayed video. Their observed time difference includes
browser scheduling and the analyser window. Physical microphone capture,
speaker/display timing and iPhone playback require physical qualification.

## Guarded recording privacy

Build the pinned `capture-privacy` bridge, the gateway and its SDK publisher,
and the recording variant with `tools/build-recording-media.py`. Use an
isolated interface and the pinned Matter Server. The trial generates fresh
attestation and scoped enrollment, publishes H.264 through the SDK, and
independently decodes the resulting archive.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_recording_reference.py \
  --sdk "$matter_sdk" --device "$matter_sdk/target/debug/webrtc_camera" \
  --controller "$matter_runtime/node_modules/.bin/matter-server" \
  --gateway gateway/target/debug/nxrs-gateway-control \
  --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --mediamtx "$recording_build/nxrs-mediamtx-recording" \
  --runtime "$matter_runtime/recording-privacy" --interface en1 --ip <fixture-lan-ip>
```

The same camera publication survives privacy changes and bridge restart.
The archive must stop growing at the confirmed privacy write, during lost
renewal, and after restart with the saved private setting. The gateway must
withdraw occupied status as unavailable and refuse a new observation while
privacy is enabled. After resuming, the first cached observation provides a
sequence boundary before fresh detections become visible. A second camera path
and a revoked controller must refuse access.

## Matter microphone mute and live privacy

### Microphone capture control

Prepare and build `--variant capture-privacy` and build the host gateway and
ingest binaries. Run the test from a LAN with IPv6 and free Matter port 5540.
It uses disposable enrollments and generated images and sound.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/ingest_av_reference.py \
  --sdk "$matter_sdk" --device "$matter_sdk/target/debug/webrtc_camera" \
  --controller "$matter_runtime/node_modules/.bin/matter-server" \
  --gateway gateway/target/debug/nxrs-gateway-control \
  --ingest gateway/target/debug/nxrs-camera-ingest \
  --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --ffmpeg /usr/bin/ffmpeg --mediamtx /absolute/path/to/mediamtx \
  --interface en1 --ip <fixture-lan-ip>
```

An independent Matter controller commissions the bridge, checks the 320×240
H.264 minimum in the camera profile, and writes `MicrophoneMuted` three times.
The mTLS camera fixture checks each sequenced command, keeps video flowing, and
waits 500 ms before acknowledging the start or stop of generated PCM. The
controller requires a successful write status after each acknowledgement. An
aiortc receiver keeps one Matter WebRTC session through the writes, verifies
silent and audible phases and fresh video RTP, then closes the session through
Matter. Its bundled offer repeats candidates across audio and video.
The provider admits each distinct candidate once and retains its 16-candidate
limit. After the peer closes, stopping the bridge withdraws capture when its
permit expires. An independent RTSP receiver checks the resulting silence,
advancing decoded video and exact picture annotations on the same publication.

To include the real Home Assistant camera dialog, start the isolated instance
described in [browser qualification](#home-assistant-browser-qualification)
with its Matter integration pointing to loopback port 15580. Leave that port
free for the witness to start its controller. Add
`--browser-python "$home_assistant_venv/bin/python"`,
`--storage-state "$matter_runtime/browser.json"` and
`--camera-entity camera.camera` to the capture-control command. The browser
must present 60 advancing frames while the camera receives no capture
command. The witness also checks that the dialog closes its Matter session.

Chrome sends mDNS host candidate names to conceal private addresses. The
bridge ignores these unsupported names and continues ICE using supported
candidates. The browser trial requires acknowledgement of the mDNS
messages and no candidate errors. [RFC 8839 §5.1](https://www.rfc-editor.org/rfc/rfc8839.html#section-5.1).

### WebRTC presentation

Prepare and build the pinned SDK with `--variant privacy`. Use the same binary
paths, interface and IP as the microphone allocation test. Run the following
in a fresh private runtime directory.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_privacy_reference.py \
  --controller "$matter_runtime/node_modules/.bin/matter-server" --device "$matter_sdk/target/debug/webrtc_camera" \
  --gateway gateway/target/debug/nxrs-gateway-control --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --mediamtx /absolute/path/to/mediamtx --runtime "$matter_runtime/privacy" --interface en1 --ip <fixture-lan-ip>
```

A commissioned controller mutes and unmutes a generated tone during playback.
The independent receiver requires consecutive exact-zero PCM samples and
advancing video. Live privacy ends established and pending sessions, rejects
direct offers, and allows a fresh viewer after release. The same controller
reconnects after two abrupt device-process restarts and checks settings,
allocated stream IDs, reference counts and decoded audio. A recording-privacy
write leaves the separately admitted LiveView usage available. This test
covers the Matter viewer. Physical microphone capture, recorder behavior and
the product-wide scope of privacy controls still require qualification.

## Operator-initiated commissioning

Prepare the pinned SDK with `--variant commissioning`. This variant uses the
SDK's development attestation chain and test VID/PID. To create disposable
onboarding material offline with the pinned Matter Server dependencies, run this command.

```sh
node gateway/tests/matter/provision.cjs "$matter_runtime" "$matter_runtime/new-device"
```

The output directory must be new. Keep `onboarding.json` with the operator.
It contains the QR payload and manual code. Give the gateway only the private
`factory.json` file. It contains a SPAKE2+ verifier, salt, iteration count and
discriminator. Use an absolute path on the trusted local filesystem with mode
0600. The device validates the P-256 material and binds its fingerprint to
the persistent node state. Preserve this file and the state directory together.
[Upstream provisioning tool and verifier format](https://project-chip.github.io/connectedhomeip-doc/scripts/tools/spake2p/README.html).

Pass `--factory-data /absolute/path/to/factory.json` at each startup. Use
`--initialize-state` once with a new state directory. To commission an
unowned node, explicitly pass `--open-commissioning-window` for that startup.
The local window lasts 180 seconds. An enrolled administrator can invite
another fabric through Matter's enhanced commissioning command.

Run the independent controller trial with a fresh, private runtime directory.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_commissioning_reference.py \
  --controller "$matter_runtime/node_modules/.bin/matter-server" --device "$matter_sdk/target/debug/webrtc_camera" \
  --gateway gateway/target/debug/nxrs-gateway-control --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --mediamtx /absolute/path/to/mediamtx --runtime "$matter_runtime/commissioning" --interface en1 --ip <fixture-lan-ip>
```

Two macOS runs paired with the QR payload and manual code in opposite orders.
Each decoded 30 changing video frames before and after an abrupt restart.
The pinned controller continued listing the expired device. Fresh discovery
did not find it and the original controller's valid code could not commission
it. In one run the system Bonjour browser reported removal 181.30 seconds
after process launch. To observe that event on macOS, browse the factory
discriminator's subtype with `dns-sd -B '_matterc._udp,_L<discriminator>' local.`.
The `--code manualCode` trial starts with the manually entered code.
[Measured results](evidence.json).

## Pair from a phone on the LAN

Use the packaged gateway and the same native tools and receiver environment
as the [commissioning test](#operator-initiated-commissioning). The trial
uses a new private directory, generated media and separate credentials.
Start it when the phone and hub are ready on the same IPv6-enabled LAN.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_phone_pairing.py \
  --runtime "$new_trial_directory" --sdk "$rs_matter_checkout" \
  --device "$gateway_package/nxrs-matter-camera" \
  --controller "$matter_runtime/node_modules/.bin/matter-server" \
  --gateway gateway/target/debug/nxrs-gateway-control \
  --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --mediamtx /absolute/path/to/mediamtx --interface en1 --ip <fixture-lan-ip>
```

The fixture keeps `factory/onboarding.json` private. Display its QR locally
or enter its manual code while the 180-second window is open. For Google Home,
use the account enrolled in a Google Home Developer Console Matter integration
with test VID `0xFFF1` and PID `0x8001`. In the iPhone app, open Devices → Add
→ Matter-enabled device and scan the code.
[Google test pairing](https://developers.home.google.com/matter/integration/pair) ·
[iPhone setup](https://support.google.com/googlehome/answer/13127223?hl=en-VC).

After pairing, look for the bridged occupancy sensor. Enter `occupied`,
`clear` and `unavailable` in the fixture terminal and observe the sensor or
an occupancy automation. `unavailable` pauses reports so their freshness
expires through the normal gateway path. Google documents Matter bridges and
occupancy sensors. The standard Matter Camera type is absent from its
[supported device list](https://developers.home.google.com/matter/supported-devices).
Remove the trial device in the app before entering `quit`. Each new trial
uses a new directory and pairing code.

See the [iPhone results](../../matter/README.md#iphone-compatibility-on-the-lan)
for what was verified in Google Home and Home Assistant.

### UDP report regression

Run these tests from the prepared SDK checkout using the Rust version in
`versions.json`. With large buffers enabled and UDP as the transport,
the wildcard read and subscription must return all attributes in multiple
reports. The reboot test also checks that a persisted wildcard subscription
delivers all its attributes in chunks after restart.

In the macOS LAN replay, an independent controller retrieved all 154
attributes in each of two reads with the UDP limit enforced. It received
12 datagrams across both reads. The largest was 1,191 bytes.

```sh
RUST_MIN_STACK=8388608 cargo test --locked -p rs-matter --features large-buffers \
  --test data_model_tests test_long_read
RUST_MIN_STACK=8388608 cargo test --locked -p rs-matter --features large-buffers,persistent-subscriptions \
  --test im_tests test_report_data_handler_catches_server_report
```

## Provisioned device attestation and camera endpoints

Prepare the pinned host SDK with `--variant root-power-time-zone` and the
same DNS-SD archive as the scoped LAN build. The bridge's deployment JSON
requires an absolute `attestation_directory`. Keep that directory on the
trusted host filesystem with mode 0700 and its regular files with mode 0600.

| File | Contents |
| --- | --- |
| `device.json` | `version` 1, integer `vendor_id` and `product_id`, and `vendor_name`, `product_name`, `serial_number` strings |
| `dac.der` | Device Attestation Certificate |
| `pai.der` | Product Attestation Intermediate certificate |
| `certification-declaration.der` | CSA-signed Certification Declaration |
| `dac.key` | The 32-byte P-256 private scalar for the DAC |

The default path requires an official signed declaration. It checks the
provisioned identity against the declaration and the DAC and PAI, checks the
certificates' validity windows and issuer key identifiers, and derives the
public key from the private key to check the DAC match. The commissioner
still validates the attestation chain against its trust store. The bridge
persists a fingerprint of the DAC and device identity during initialization
and requires the same identity on startup.

Run `gateway/tests/matter_attestation_reference.py` with the
[snapshot test](#matter-jpeg-snapshots) arguments plus `--sdk` pointing to
the prepared SDK. Use `--packaged-bridge` to check the package’s Time Zone,
NTP Client and Time Sync Client feature map. The test extracts the pinned
upstream public development vectors into a private runtime and enables them
with `--allow-test-attestation`.
It checks invalid input before state creation, commissioning and
identity-change rejection. It verifies the Camera's WebRTC Requestor client,
Binding server and omission of Zone Management and digital PTZ. Two
independent fabrics write their own bindings and read them back after restart.
An oversized list replacement preserves both fabrics' entries. Removing one
fabric removes its bindings and keeps those of the survivor. A test snapshot
reproduces an interruption between fabric removal and Binding cleanup. On
restart the bridge prunes the obsolete entries before accepting requests.
The surviving enrollment plays video after recovery.
Use product-issued credentials for real device qualification. Production
credential issuance and protected-key lifecycle have separate release gates.
[Results](evidence.json).

### Bridge power and timezone

Set `host_power_source` in the deployment JSON to `"ac"` or `"dc"` to
describe the gateway's single wired supply. The bridge lists its root and
aggregator as powered endpoints. The camera and occupancy endpoints are
independently powered. Starting network service requires the setting.
Offline fabric inspection still works with earlier deployment files.

The test reads the configured power source on the root before and after
restart. It sets standard time and a DST offset through Matter, checks
local time against administrator-provided UTC, rejects an invalid time
zone, and reads the settings again after restart with the same enrollment.
The TimeZone feature uses the SDK's durable tables.

### Trusted Matter time

Prepare `--variant trusted-matter-time` and run
`gateway/tests/matter_time_reference.py` with the same arguments as the
attestation test. The runner commissions an independent Matter.js clock
onto a second fabric and grants the bridge read access to its time cluster.
It verifies that the bridge uses CASE to read UTC and derives local time
from its timezone and DST settings. Restart reads a fresh sample using the
same enrollment.

Removing the source's fabric cancels a pending refresh and leaves the last
accepted clock sample in use. An interrupted-removal snapshot verifies that
startup discards a trusted-source record whose fabric is gone. The first
administrator retains playback.
[Results](evidence.json).

### Configured SNTP fallback

Prepare `--variant default-ntp` and add `--default-ntp` with a reachable
IPv6 address on UDP 123 to the trusted-clock test. The controller configures
the address through Matter. The bridge persists it, rejects malformed
addresses and hostnames, and accepts a null value to clear it. Queries use
the selected LAN interface, with a deadline and reply validation, and update
the bridge's Matter clock. The host clock is unaffected.

The witness verifies that the trusted Matter source takes precedence, that
fallback resumes after fabric removal, and that the setting survives
restart. The macOS trial used Cloudflare's published IPv6 time service.
UDP loopback tests cover reply validation and timeouts without internet
access.
[SNTPv4 protocol](https://www.rfc-editor.org/rfc/rfc5905) · [Time service](https://developers.cloudflare.com/time-services/ntp/usage/) · [Results](evidence.json).

### DNS-SD time sources

Prepare `--variant dns-sd-time`. The bridge browses for `_ntp._udp.local`
on the selected LAN before trying trusted Matter time and the configured
fallback. It uses bounded unicast responses to multicast queries from an
ephemeral port. PTR, SRV and address records can arrive separately.
Only NTP on UDP 123 is eligible.

In a disposable Linux guest, run `gateway/tests/matter_ntp_discovery.py`
with the attestation test arguments except `--interface` and `--ip`. The
runner creates two private veth links and compares distinct clock values.
It checks that the advertised clock on the other link is ignored, that
the selected link's advertised clock supersedes the configured fallback,
and that removing it restores fallback and playback on the same enrollment
after restart. Use the macOS trusted-clock test with `--default-ntp` on
a network without an advertised NTP source to check fallthrough.
[mDNS queries](https://www.rfc-editor.org/rfc/rfc6762#section-6.7) · [DNS-SD](https://www.rfc-editor.org/rfc/rfc6763) · [Results](evidence.json).

### DHCP time sources

Prepare `--variant dhcp-time`. The bridge reads the selected link's DHCP
time servers on each synchronization attempt. On Linux it reads
systemd-networkd's per-link report, verifies the interface and local address,
and tries DHCPv6 literals before DHCPv4. It ignores static or runtime
time-server entries. On macOS it reads DHCPv4 option 42 through
`ipconfig getoption`. The existing DNS-SD, trusted Matter and configured
SNTP sources follow DHCP in that order.

Install `dnsmasq-base` in the disposable Linux guest and add `--dhcp` to the
two-link witness. The DHCP server replies on the private selected veth
link with distinct clocks, so precedence is observable through Matter
across restarts. NetworkManager, DHCPv6 hostnames and macOS DHCPv6 remain open.
[DHCPv4 NTP option](https://www.rfc-editor.org/rfc/rfc2132#section-8.3) · [DHCPv6 NTP option](https://www.rfc-editor.org/rfc/rfc5908) · [Results](evidence.json).

### Matter TLS certificates

The gateway stores up to five TLS CA certificates per authenticated fabric in
the existing durable node store. Provisioning and use check DER, the CA
constraints, the signature and synchronized UTC. SHA-256 lookup is fabric
scoped, rotation preserves the CAID, and allocation commits its cursor and
certificate together. The fabric and its credential records can be retired in
one store transaction.

Run `cargo test --locked --manifest-path gateway/Cargo.toml -p nxrs-gateway-matter tls_roots_rotate_and_retire_with_their_fabric`.
The test uses generated certificates and a real Rustls handshake before and
after rotation. The old server's certificate is rejected on one fabric and
still accepted on the other. The test reopens the durable store after removal.

The client table reserves two identities per fabric. The gateway generates a
P-256 key, a PKCS #10 request and a signature over the controller's 32-byte
nonce. Renewal keeps the original key and the active certificate until a
replacement with the matching public key is committed. The TLS engine receives
a signing handle. Private DER copies in the gateway's state buffers are
zeroized on drop. The node store remains a protected local file.

Run `cargo test --locked --manifest-path gateway/Cargo.toml -p nxrs-gateway-matter tls_client_key_survives_restart_and_authenticates_after_renewal`.
The test verifies both CSR signatures and mutual TLS with a generated leaf and
intermediate before and after renewal.
[TLS Certificate Management](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/clusters/TLSCertificateManagement.xml) · [TLS Client Management](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/data_model/1.5.1/clusters/TLSClientManagement.xml).

### TLS management over Matter

Prepare `--variant tls-management` and build the camera with the same locked
SDK command. Run `gateway/tests/matter_tls_reference.py` with the same binary
and interface arguments as the [clock witness](#trusted-matter-time). The test
starts two independent administered fabrics, provisions roots, client keys and
endpoints, and verifies which records a fabric can invoke or read. An endpoint
blocks removal of its root and client certificate until the endpoint is changed
or removed. The test checks atomic fabric retirement, restart and media playback.
Repeat with `--large-root` to round-trip a certificate larger than the UDP
payload limit. Use `--max-chain` to provision two identities, each with one
3,000-byte leaf and ten 3,000-byte intermediates. Matter TCP frames stay at
or below 64,000 bytes. A combined `FindClientCertificate` response that
exceeds the message budget returns `ResourceExhausted`. Controllers can find
each ID individually or read the complete chunked certificate attribute.
The adapter includes certificate bodies only for the accessing fabric on a
Large Message attribute read. The optional Push AV Transport is absent, so
there are no application references to installed TLS endpoints.
[Host results](evidence.json) · [CCM message length](https://www.rfc-editor.org/rfc/rfc3610.html#section-2.1) · [Matter endpoint removal test](https://github.com/project-chip/connectedhomeip/blob/abcc720b48c5e59c0edcfe65c516f76ca9448aa3/src/python_testing/TC_TLSCLIENT_3_20.py).

## Two controllers on the same fabric

For **two controllers on one fabric**, prepare a clean SDK with
`--variant viewer-permissions`. Build the camera with the commissioning
variant's Cargo command and run `gateway/tests/matter_viewer_permissions.py`
with the same paths, interface and IP as the commissioning example above.
Use a fresh private runtime directory. The administrator commissions both the
camera and a separate Matter.js viewer. Each holds its own operational key.

The witness plays both WebRTC streams, withdraws the viewer's endpoint grant,
and restores it while both receivers are still connected. It checks the Matter
end reason, received RTP, decoded frames, and the surviving upstream reader.
It also withdraws access during an unanswered offer and opens a fresh session
after permission returns. The SDK fixture checks the original authenticated
subject against the endpoint and offer command on ACL changes, fabric removal
and fail-safe expiry. Each stream requires a fresh negotiation after withdrawal.
In-place SDP renegotiation is unsupported by this fixture.

## Interrupted access changes

Build the same unstripped `viewer-permissions` camera from this checkout.
On macOS, run the following command with the pinned Node on `PATH` and a
debugger permitted to attach to the new test process.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_recovery_reference.py \
  --controller "$matter_runtime/node_modules/.bin/matter-server" \
  --device "$matter_sdk/target/debug/webrtc_camera" --gateway gateway/target/debug/nxrs-gateway-control \
  --mediamtx /absolute/path/to/mediamtx --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --runtime "$matter_runtime/access-recovery" --interface en1 --ip <fixture-lan-ip>
```

The test terminates its own camera process either just before the snapshot
rename or after the directory sync, before the controller receives a reply.
It exercises a viewer grant, its withdrawal and removal of a second fabric.
For fabric removal, the debugger checks the actual key being deleted because
the SDK also writes an allocation cursor. A separate trial terminates the
process after a successful withdrawal reply.

Each restart uses the original identities in fresh processes. Controllers
read the fabric list or ACL directly, check access and decode changing video.
An interrupted rename preserves the previous access. A completed sync makes
the new access visible after restart. System power loss, interrupted initial
commissioning and consistency across multiple SDK records need separate tests.

## Two administrators and active fabric removal

With the other test processes stopped, use the receiver Python environment,
the RTSP variant of the SDK and the local IP address of the selected
interface. The witness starts fresh Matter Servers, a test device, a strict
gateway, MediaMTX and one generated-video publisher. Run from the repo root
with the pinned Node binary on `PATH`.

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_fabric_reference.py \
  --controller "$matter_runtime/node_modules/.bin/matter-server" \
  --device "$matter_sdk/target/debug/webrtc_camera" --gateway gateway/target/debug/nxrs-gateway-control \
  --mediamtx /absolute/path/to/mediamtx --publisher gateway/target/debug/nxrs-rtsp-publisher \
  --runtime "$matter_runtime/fabric-trial" --interface en1 --ip <fixture-lan-ip>
```

A opens a 180-second enhanced commissioning window for B. Each fabric must
decode moving H.264 from the shared source. A removes B while both local peers
stay open. After a two-second drain, measure four seconds of decoded frames and
RTP reception. A must keep playing, B must stop receiving packets, and the
publisher and upstream reader must retain their IDs. Restart the device with
its persisted identity, verify A can play again and commission C. Read the
device's full fabric table through A and check that only A and C are present.
Also attempt an operational read through B and record the outcome.

The SDK persists a fabric-index cursor. Ordinary recommissioning can give C a
new index. With `--exercise-index-wrap`, A repeatedly commissions and removes
fresh administrators, each with its own certificate authority and private
controller storage. The test stops when a new fabric receives B's old index
and verifies its session list is empty before opening video. Only sanitized
`result.json` is intended for sharing.

## Scope of the evidence

[Issue #388](https://github.com/yongkyuns/nxrs/issues/388) is the release
checklist. [Evidence](evidence.json) identifies the revision and scope of each
run. The merged macOS and Linux builds decoded the original ESP32 H.264 and
returned JPEG snapshots in isolated deployments. Separate macOS rehearsals
verified concurrent Nxrs and Matter playback with the existing Nxrs viewer
credential, selective revocation and gateway rollback. The earlier Home Assistant
iPhone playback demonstrated feasibility.

- **Deployment.** The live gateway still needs the scoped authorization
  cutover and verification of existing enrollments, revocation and recovery.
  Exercise restart, session limits, slow receivers and LAN changes under load,
  including interface exclusion across networks.
- **Client playback and occupancy.** Verify the release gateway with a physical
  iPhone over the LAN and remotely. Include snapshots, stream teardown and
  concurrent Nxrs Home and Matter rendering. The tested stock Home Assistant
  snapshot proxy fails the width-only request. The real detector exercised
  unavailable and recovery in the isolated bridge. A controlled clear-to-occupied
  trial on the deployed gateway is pending.
- **Performance.** The completed 24-hour Linux fixture used pre-merge code and
  generated media. The merged-build soak and sustained real-camera playback
  remain release gates.
- **Camera conformance.** Physical microphone capture, iPhone A/V timing and
  privacy, production attestation and conformance qualification remain open.
  See [camera qualification boundaries](conformance.md). Direct Matter on
  NuttX is tracked separately in phase 6.
