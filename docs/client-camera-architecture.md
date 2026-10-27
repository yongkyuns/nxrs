# Native camera clients

The gateway protocol is shared. UI, network lifetime, credential storage,
decoding, rendering and local capture remain native Swift/SwiftUI and
Kotlin/Compose. A Rust client core or cross-platform UI is not required to
implement the existing HTTPS/WHEP/SSE contracts.

## Ownership

`MonitorModel` (iOS) and the process-wide `HomeController` (Android) own one enrollment, the camera
catalog and viewing admission. Each `CameraSession` is bound to one camera and
one immutable enrollment. It owns its peer, WHEP lease, source namespace,
detection subscription, retries, freshness and capture jobs. Switching selection
must not reuse a peer or detection state from another camera.

The default budget is **two active camera sessions**. The ordered request is the
visible selection followed by other visible tiles. Full screen requests only its
selected camera and reuses that session. Explicit pauses survive foregrounding.
The dashboard has one automatic layout: one camera fills the available width;
multiple cameras use compact previews, stacked on phones and arranged in columns
when width permits. There is no single/grid mode or mode state in admission.
Large accessibility text uses one column. Catalog and filter changes update the
same camera collection, preserving camera identity and resource ownership.
A failed camera stays blocked until Retry; it does not prevent a neighbor from
playing. Catalog identity/capability changes and home replacement retire the old
sessions, even when the new home reuses the same camera IDs.
Removing a selected camera chooses an available replacement; removing the
full-screen camera closes its presentation. Unchanged cameras keep their sessions.
With automatic playback disabled, new cameras remain paused until explicitly
resumed. Selection for display does not itself override that preference.
Watch/Pause updates selection and admission in one action. Selecting and then
toggling independently can start a camera and immediately pause it again.

Cancellation immediately clears presentation and closes the local peer. The
admission slot remains occupied until startup, WHEP DELETE, native callbacks,
renderers, snapshots and clip finalization have joined. Retiring old enrollments
reserve slots as well. Starting a replacement before that barrier would exceed
the decoder/memory budget. A failed native retirement keeps a sticky reserved slot
until process restart. Activity replacement never creates a second admission owner.
One monotonic 15-second startup budget spans offer,
grant, WHEP, answer and first decoded image; retries are bounded.

Swift's main actor and Android's main dispatcher serialize session/admission
mutations. Native decoder/render/capture callbacks have their own explicit
ownership and close barriers. Backgrounding requests no sessions. Offscreen
renderers release their surfaces instead of drawing underneath full screen.

## Protocol and trust

The clients consume the existing camera catalog, short-lived playback grants,
WHEP and detection protocol described in `docs/monitoring-api.json` and
`docs/detection-protocol.schema.json`. Camera IDs are stable keys, never stream URLs.
Enrollment owns the complete HTTPS origin allowlist; a server response cannot
expand it. Redirects, wrong-camera grants, expired grants and cross-origin WHEP
Locations are rejected. Even an invalid, oversized or interrupted WHEP 201 body
must retire its trusted allocated Location. No bearer key, SDP or raw server
error is logged or persisted outside the appropriate credential/transport owner.

iOS uses a device-only, unlocked Keychain item. Android uses Android Keystore
AES-GCM with a nonexportable, unlocked-device key, authenticated ciphertext in
`noBackupFilesDir`, and no backup/device-transfer admission. Android honors
system roots and roots explicitly installed by the user; hostname verification
and the enrollment origin allowlist still apply. Test trust is injected only in
the test's loopback transport or a disposable iOS simulator.

The gateway retires closed media ownership using the **start** of the media
session-list request as its barrier. The pinned MediaMTX registers a session
before its authorization callback. Thus an absent ID in a later snapshot proves
retirement, including failed negotiations; an older snapshot retains racing
admissions. This avoids stale entries exhausting the four-session viewer limit
without weakening revocation, orphan retirement or the limit itself. Requalify
this ordering before changing the pinned media server.

Both clients run the same golden protocol and session-admission fixtures from
`clients/ios/Tests/NxrsMonitorCoreTests/Fixtures`. This is one test corpus, not an
Android runtime dependency on the iOS application. Wire schemas and fixtures are
the shared contract; SDK objects do not cross into it.

## Pixels and detections

SSE provides detection status/history independently of video freshness. Its
bounded, latest-event subscription cannot block capture or accumulate work.
SSE/arrival time never positions a live box. Boxes require the exact source
identity carried by the H264 access unit and matched to the actual decoded
image. Missing, stale, cropped, ambiguous and obsolete-source mappings omit
annotations. A frozen image clears its boxes after 1.5 seconds without new input.
An expired inference is unavailable rather than a new clear observation.

On iOS, the existing decoder buffer identity store and Metal renderer retain
that mapping. On Android, the Java MediaCodec adapter parses bounded SEI and
assigns a unique, microsecond-aligned private input token. WebRTC's original Java
timestamp is only whole milliseconds derived from RTP and can alias during a
burst. The adapter matches the actual codec output token, restores SDK timing,
and attaches metadata to the owned image buffer. Codec drops discard obsolete
entries; duplicate/unmapped output invalidates the owner. Pending entries and
source epochs are bounded. Dimensions must match before annotations are admitted.

Android uses MediaCodec byte-buffer output. The pinned SDK's SurfaceTexture path
failed an independent red/person, blue/clear image correspondence test, so it is
not used for annotations. Hardware decoding is preferred, with the OS software
codec as fallback. A video-only monitor consumes the matched decoder output
directly: the SDK's later A/V playout stage otherwise coalesced valid frames.
The SDK still owns transport, keyframe recovery and decoder setup/teardown.

Each Android surface retains one pending frame and one immutable I420 image
already copied out of the codec, at most 1920×1080 pixels. It draws YUV directly
without an intermediate RGB texture. Video and boxes are drawn in the same EGL swap with the
same rotation/letterboxing. The SDK returns the codec output buffer after its immutable YUV copy;
no idle display pins a codec buffer. Expiry redraws only when an annotation must
clear. Surface destruction joins GL release before returning the native window.
Peer close joins every owned sink before admitting another peer.

## Saved footage

Local capture is camera-specific. Snapshots consume a bounded owned image;
recording retains at most one pending frame and drops overload. Clips are silent,
limited to 60 seconds and 32 MiB, and finalized before their session slot retires.
The private library reserves capacity before recording (512 MiB / 1000 items),
persists its index atomically, and never deletes saved originals to make space.
Only an explicit share action grants another app access to the selected file.
The Android library supports review, share and deletion; iOS retains its existing
library, favorites and sharing behavior.

## Qualification

`clients/tests/multicamera_reference.py` starts three independent real Rust
stream-service sources and gateway/ingest publications, with alternating
red/person and blue/clear image/annotation witnesses. The near-17 FPS case uses deterministic textured borders, an exact center-color
witness and a 1 ms owner poll budget. Tiny flat H264 images instead batch at the
publisher and exercise burst coalescing; the legacy flat-color identity fixture
remains available. Sources use a named near-17
FPS fixture cadence; this is not an ML accuracy benchmark. The native runners
exercise two cameras, pause/retry, full-screen reuse, JPEG/clip capture, source
replacement and background cleanup. Android additionally checks exact rendered
pixels, frozen expiry without new input and source-fault isolation. Every runner
must emit its success marker; a skipped integration test cannot pass the fixture.

Native CI uses the gateway/ingest release binaries with the same source cadence,
freshness deadlines and assertions. Debug-build CPU overhead belongs in separate
build-cost measurements rather than the native client's throughput budget.

The ingest watchdog measures encoded output with advancing encoder frame counts
and publication with strictly advancing SDK publisher packet counters. Encoder
frames, muxed timestamps, repeated packet counts, unknown progress fields and
partial lines do not renew the publication deadline. These progress counters
measure health; decoded-image identity still comes from the access unit.

The native client fixture selects its encoder and optional isolation launcher
explicitly. Its independent system FFmpeg generates synthetic source images.
The SDK publisher must be built before camera acquisition, and the fixture's
TLS issuer requires its pinned CPython environment. Android CI supplies the
minimized media runtime, its linked release SDK binaries and the existing
encoder sandbox. These resources do not change the pixel or lifetime assertions.

Record codec-input FPS, codec-output FPS, delivered FPS, actual rendered FPS and
process CPU separately. Synchronous pixel readback is enabled for the identity
witness and removed for production throughput measurement. The diagnostic
observer switches on the existing renderer, preserving its native surface and
peer. Surface replacement and frozen-image expiry have separate lifetime controls.
The named functional
case requires at least 4 decoded/rendered FPS per stream under emulator load; the
throughput case requires at least 14 and does not certify a physical device.
Emulator GPU work may execute outside the Android
process, so process CPU is not total host or physical-device cost. Simulator and
emulator results establish integration behavior, not physical camera throughput,
Android battery/thermal behavior or certification for every vendor codec.
Requalify the pinned SDK binary, pixel identity, lifetime barriers and physical
throughput before changing codec output mode or widening the viewing budget.
