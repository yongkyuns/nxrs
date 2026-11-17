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

This proves process recovery and modeled interrupted staging, not sudden power
loss or atomicity across several SDK blob mutations. Semantic SDK-state/version
validation, commissioning failure paths and multi-admin removal remain open.

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
requires 60 presented frames with increasing media time and changing sampled
pixels, records video dimensions and presentation/decoder counters, and closes
the dialog. Repeat to exercise session reuse. Browser peer closure is distinct
from proving that all SDK tasks joined. Headless presentation FPS is diagnostic;
it is not the physical iPhone performance acceptance measurement.

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
sharing. Physical camera input, per-fabric access and iPhone rendering need
separate qualification.

## Scope of the evidence

`evidence.json` records host results. Neither a teardown acknowledgement nor
repeated sessions establishes joined worker cleanup. The upstream example still
uses placeholder capability declarations, unbounded queues and detached tasks.
Production needs truthful capabilities, bounded session ownership, revocation,
complete persistent identity and failure recovery under #388's release criteria.

See [conformance.md](conformance.md) for mandatory audio/snapshot/root behavior
and the unresolved endpoint topology. The video-only fixture is not a conforming
Matter Camera product.

A physical iPhone, real camera media, bridge conformance,
multi-admin authorization, Linux, remote access and the 24-hour soak remain
separate acceptance gates. This fixture establishes no NuttX support and adds
no mandatory integration CI job or production SDK dependency. The Rust store and occupancy tests run in the existing gateway workspace check using Rust 1.90.
