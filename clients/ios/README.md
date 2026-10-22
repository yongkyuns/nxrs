# Nxrs Home for iPhone

A native home monitoring client with a camera-first dashboard, rooms and
favorites, full-screen live video, digital zoom, snapshots, short video clips and
a private local Recordings view. The original home/lens identity, light and dark
themes, guided setup and native Home / Recordings / Settings navigation are described in the
[product brief](../../docs/ios-home-experience.md).

The authenticated receive-only player and Keychain enrollment from
[PR #21](https://github.com/yongkyuns/nxrs/pull/21) remain the foundation. Existing
enrollments work without re-pairing. Check commit-specific validation evidence:
simulator screenshots and macOS media tests do not qualify a new iPhone build.

## Open and build

Open `clients/ios/NxrsMonitor.xcodeproj` in Xcode, select the shared `NxrsMonitor`
scheme, and use an iPhone simulator or a physical iPhone with iOS 16 or later.
For a real phone choose your development signing team and a bundle identifier
available to that team. There are no developer certificates or signing keys in
the repository. This is a development app; shipping SDK notices, distribution
review and physical-device acceptance are still release gates. Editable logo
geometry and the app-icon renderer live in `Design/`.

```sh
# Dependency-free contract/WHEP/lifecycle tests
cd clients/ios
swift test --jobs 2 -Xswiftc -warnings-as-errors

# Apple-only native tests and receiver
cd Native
swift test --jobs 2 -Xswiftc -warnings-as-errors

# From clients/ios, compile the whole app without a signing identity
xcodebuild -project NxrsMonitor.xcodeproj -scheme NxrsMonitor \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build

# Run setup, camera navigation, saved recording and accessibility UI tests
xcodebuild -project NxrsMonitor.xcodeproj -scheme NxrsMonitor \
  -destination 'platform=iOS Simulator,name=YOUR_AVAILABLE_IPHONE' \
  CODE_SIGNING_ALLOWED=NO test
```

`Package.swift` remains the dependency-free portable core. `Native/Package.swift`
contains the separately checksum-pinned Apple WebRTC binary and native tests.
The Xcode project consumes both local packages. See [THIRD_PARTY.md](THIRD_PARTY.md)
for the community binary provenance and outstanding release-review gates.

## Connect to a gateway

The app takes an explicitly trusted HTTPS control gateway, an optional separate
HTTPS media origin, and a viewer credential from gateway enrollment. Do not use a
publisher or administrator credential. Both HTTPS endpoints need certificates
trusted by iOS; no certificate-validation bypass or broad ATS exception exists.
The gateway's loopback reference test is not directly reachable from a phone:
configure a trusted HTTPS front proxy and reachable WebRTC media addresses first.
See the [gateway boundary](../../gateway/README.md).

The record is stored together in Keychain with
`kSecAttrAccessibleWhenUnlockedThisDeviceOnly`, not UserDefaults or iCloud sync.
Changing gateway trust requires new explicit enrollment rather than forwarding an
old account credential to a new origin. Forget gateway deletes the local record;
server-side revocation is a separate operation. The receive-only application
requests no camera or microphone capture permission. Local-network permission
explains access to the enrolled gateway; there is no Bonjour discovery yet.

Select a camera to view. Synthetic sources are visibly labeled. Live is set only
after advancing decoded frames, never from an API/ICE success. A missing-frame gap
marks the image stale; persistent stalls trigger a bounded reconnect. Stop,
camera replacement and backgrounding close the local peer immediately and attempt
a bounded WHEP DELETE. The renderer is recreated for a new peer so an old camera's
image cannot remain under another camera's name. Full-screen and landscape viewing
use the same native video track.

## Save and review footage

Snapshot saves the latest current decoded frame as a JPEG. Record clip saves
video only, stopping after at most 60 seconds or when viewing ends, the camera
changes, video stalls or the app leaves the foreground. Encoding runs on a
separate bounded queue; it cannot build up an unbounded backlog of video frames.
Clips are capped at 15 frames/second and 32 MB. A failed or empty recording is
never shown as a saved clip.

Recordings offers camera-name search, snapshots/clips/favorites filters, playback,
explicit system sharing and confirmed deletion. Files are device-local, protected
while locked and excluded from cloud backup. The 512 MB / 1,000 item limit never
silently deletes saved footage; incomplete saves are recovered on the next launch.
Forgetting a home removes its viewer key and leaves saved footage accessible.

Camera names, rooms, favorites and playback/appearance preferences stay on this
iPhone. Pause stops viewing here; it does not power off the camera. Audio, talk,
pan/tilt, motion notifications and continuous recording history require additional
camera/gateway capabilities and are not offered as working controls.

## Import an invitation

Setup can import a private JSON file or intentionally paste its JSON text. Version
1 has `version`, `gateway` (HTTPS), `viewerCredential`, optional `mediaOrigin`
(HTTPS), and optional `homeName` fields. Treat the entire file as a secret and
share it through a trusted private channel. Do not commit invitations or put
viewer keys into URLs. Import fills the form; Connect securely validates the
catalog before replacing an existing Keychain record. No credential is saved to
preferences, and certificate verification remains mandatory.

## Initial WHEP/network profile

The first native player completes local ICE gathering before POSTing the offer.
It implements authenticated POST/answer and DELETE with same-origin Location
validation, bounded SDP, timeouts and partial-success cleanup. It does **not** yet
implement trickle PATCH, ICE restart, Link-header TURN discovery or arbitrary
hosted/P2P traversal. Those are explicit follow-ups inside the protocol adapter,
not VPN logic embedded in the UI. The initial server must advertise reachable
LAN/WireGuard candidates; no third-party STUN service is contacted implicitly.

Each reconnect obtains a fresh grant after ICE gathering. Transient failures get
at most three automatic retries (2/4/8-second backoff); access/trust/catalog/limit
errors require user intervention. A network transition that breaks media is
recovered via peer failure/stall detection, not by managing the WireGuard app.
Foregrounding resumes an interrupted live view; an explicitly paused view stays
paused. Background streaming is not claimed.

### Whole-attempt startup deadline

`PlaybackStartup` supplies one **15-second monotonic budget** from before the
native offer/ICE work through grant retrieval, WHEP POST, answer application and
the first observed decoded frame. The clock is not reset at each stage. The app
and the hosted native reference probe use this same owner. The existing native
peer conforms to its small `PlaybackStartupPeer` boundary; no capture or VPN
machinery is added.

On failure/timeout, local decoding is stopped and the app's failure status is
updated before waiting for remote cleanup. Pending operations are canceled and
joined, so late results cannot start a later phase. A known WHEP resource is
retired exactly once, including a successful POST arriving after cancellation.
DELETE remains a best-effort operation with its own bounded HTTP transport; its
time is **not** included in the 15-second first-frame budget. Unknown resources
from a lost POST response still rely on server cleanup. Retries/backoff and remote
cleanup mean a complete retry sequence can take longer than 15 seconds.

The startup timer is canceled once video arrives: it does not end a healthy live
view after 15 seconds and does not implement server-side authorization expiry.
Each retry owns a fresh peer/grant/startup instance. Stop/background/camera changes
cancel the old task and retain the existing generation guard against stale UI
updates. Runtime scheduling and cancellation-cooperative SDK/HTTP dependencies
are required; this is not a hard-real-time guarantee or protection against a
blocked main actor. Post-start frame-stall monitoring remains separate.

Decoded delivery is not proof of camera capture freshness or glass-to-glass
latency. A publisher looping old footage is a remaining source-health problem.
Likewise, the current gateway's token expiry/revocation does not terminate an
already-established stream; see its documented production blockers.

## Validation categories

Core tests cover authorization, endpoint policy, resource bounds, WHEP response/
cleanup/error handling, cancellation and late-session callbacks. Startup tests
hold each negotiation phase, verify cumulative budget exhaustion, canceled/late
results, cleanup and that the timer cannot later terminate successful playback.
Apple-native unit tests cover receive-only SDP, bounded one-shot SDK callbacks,
native/planar-frame JPEG conversion, adapted-frame crop orientation, prompt
encoder-failure handling and encoding/decoding actual H.264 clips. The capture
cases also run inside the iOS UI-test runner to exercise iOS runtime behavior.
The native reference probe uses the same `PlaybackStartup`, `NativeVideoPeer`,
`GatewayClient` and `WHEPClient` as the application, reads an ephemeral synthetic
MediaMTX stream and requires advancing decoded frames, a snapshot, a decodable
saved clip and successful DELETE. UI tests use explicitly labeled Debug-only
fixtures and cover enrollment validation, personalization, navigation, landscape,
large text, favorites, sharing and deletion. Release builds ignore preview flags.

`ci/native_reference.py` refuses personal/self-hosted machines. It creates an
ephemeral CA and signed server leaf in a private temporary directory. The probe's
**test-only `FixtureHTTPS` transport** scopes that CA to each URLSession using
`SecTrustSetAnchorCertificates` and `SecTrustEvaluateWithError`, retaining the
server's SSL/hostname policies. Nothing is added to a system or user keychain;
no privileged trust-settings commands, TLS bypass, or trust exceptions are used.
The transport is compiled only into the probe executable, not the native library
or iPhone app. The application's `BoundedHTTPTransport` and trust policy are
unchanged. Thus native media interoperability uses the production API/WHEP/peer
code, but its positive HTTPS case uses fixture-scoped trust rather than the
application's default system trust path. This is not device enrollment evidence.

Before playback, real HTTPS checks require rejection by the unchanged production
transport, successful scoped-CA HTTPS, wrong-CA and wrong-hostname rejection,
redirect refusal, off-origin refusal, and declared/streamed response-size bounds.
A separate Security-framework evaluation verifies leaf expiry using a future
evaluation date; no system clock is changed. Production transport must still
reject the CA after these checks. The harness verifies a redirect trap was never
reached. These controls run in each of two independent receiver processes.

Credentials, private keys and raw logs are temporary and never uploaded. All
child processes and logs receive cleanup attempts even if another cleanup fails;
overall PASS is emitted only after the temporary directory is removed. Portable
harness tests cover private certificate generation, refusal of personal machines,
publisher readiness and cleanup failure paths. The test proxy is not a production
server. Encoder credentials occur in test-process arguments, so the harness
requires an isolated trusted runner.

An unsigned iPhone SDK build is not an install/run test. Native macOS decoding
is stronger than compilation but still does not establish iPhone VideoToolbox,
Metal rendering, local-network prompts, Keychain lifecycle, cellular transitions,
WireGuard routing, a long soak or actual camera-driver behavior. Those gates
remain open, with no firmware/HAL/driver changes made by this client slice.

## Detection status and boxes

The camera view subscribes to the gateway's authenticated detection feed and shows
Person detected, No person detected or Detection unavailable. Loss of video,
metadata expiry, stop, backgrounding and camera switching invalidate the indicator.
[The versioned contract](../../docs/camera-detection-delivery.md) carries bounded
source-frame boxes and retained event IDs. Annotated H.264 carries each camera
frame's identity and observations. The native decoder associates these with the
actual decoded image buffer; orange boxes follow rotation, aspect fit and zoom.
Missing, ambiguous or expired metadata clears boxes. Fresh in-band metadata also
drives status; SSE supplies history, authorization and legacy-video fallback.
Enable the ingest metadata option only with compatible camera firmware; legacy
video remains supported.
