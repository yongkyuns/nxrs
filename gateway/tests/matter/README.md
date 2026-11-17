# Matter camera feasibility fixture

This opt-in fixture tests the protocol choice for [#388](https://github.com/yongkyuns/nxrs/issues/388).
It is separate from the live gateway, camera firmware and production Cargo graph.
The issue is the canonical architecture and release checklist.

`../matter_reference.py` commissions the rs-matter camera example through the
independent Home Assistant Matter Server, then uses aiortc to decode H.264. It
requires 60 frames with advancing timestamps and changing decoded images, and an
acknowledged Matter session teardown. Reported FPS is **decoded fixture FPS**,
not iPhone rendering or physical camera throughput.
Candidates are removed from both directions of SDP so playback must use the
Matter ICE-candidate commands; inline candidates cannot conceal broken trickle.

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

The patch changes Apache-2.0 upstream sources and retains their original notices.
It is a qualification patch, not a vendored production Matter runtime. Two
focused SDK regressions accompany the protocol changes.

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
git -C "$matter_sdk" apply "$matter_fixture/rs-matter-camera.patch"
cp "$matter_fixture/Cargo.lock" "$matter_sdk/Cargo.lock"
cargo +1.98.1 build --locked --manifest-path "$matter_sdk/Cargo.toml" \
  -p rs-matter-examples --bin webrtc_camera --features webrtc,astro-dnssd
cargo +1.98.1 test --locked --manifest-path "$matter_sdk/Cargo.toml" \
  -p rs-matter-dm --lib
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
TMPDIR="$matter_runtime/device-tmp" WEBRTC_CAMERA_H264="$matter_runtime/fixture.h264" \
  WEBRTC_CAMERA_FPS=30 RUST_LOG=warn "$matter_sdk/target/debug/webrtc_camera" \
  > "$matter_runtime/device.log" 2>&1
```

Commission only this fixture into an empty isolated controller using its LAN IP:

```sh
"$matter_runtime/venv/bin/python" gateway/tests/matter_reference.py \
  --test-commission-ip <fixture-lan-ip> --flow inventory
"$matter_runtime/venv/bin/python" gateway/tests/matter_reference.py --flow receiver-offer
"$matter_runtime/venv/bin/python" gateway/tests/matter_reference.py --flow camera-offer
```

Restart the fixture with the same private state to check retained commissioning.
Run alternating playback flows at least six times to exceed the four-session
table cumulatively. Stop only these two fixture processes when finished.

## Scope of the evidence

`evidence.json` records host results. Neither a teardown acknowledgement nor
repeated sessions establishes joined worker cleanup. The upstream example still
uses placeholder capability declarations, unbounded queues and detached tasks.
Production needs truthful capabilities, bounded session ownership, revocation,
persisted identity and failure recovery under #388's release criteria.

Home Assistant UI, a physical iPhone, real camera media, bridge conformance,
multi-admin authorization, Linux, remote access and the 24-hour soak remain
separate acceptance gates. This fixture establishes no NuttX support and adds
no mandatory CI job or production dependency.
