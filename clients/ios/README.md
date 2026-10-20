# Nxrs Monitor for iPhone

Tracks [#20](https://github.com/yongkyuns/nxrs/issues/20) in draft
[PR #21](https://github.com/yongkyuns/nxrs/pull/21). The native SwiftUI app, Keychain
enrollment, Metal view and receive-only libWebRTC player are now implemented.
**Physical-iPhone/cellular/WireGuard playback and camera integration remain
unqualified.** Check the PR's commit-specific CI evidence, not this file, for
which builds and runtime tests have actually passed.

## Open and build

Open `clients/ios/NxrsMonitor.xcodeproj` in Xcode, select the shared `NxrsMonitor`
scheme, and use an iPhone simulator or a physical iPhone with iOS 16 or later.
For a real phone choose your development signing team and a bundle identifier
available to that team. There are no developer certificates or signing keys in
the repository. This is not an App Store/TestFlight release; app icons and
shipping SDK notices/distribution review remain unfinished.

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
Foregrounding reconnects the selected camera. Background streaming is not claimed.
The no-frame watchdog runs after answer application; whole-negotiation startup
is additionally bounded by individual SDK/HTTP deadlines, not yet a single
15-second end-to-end watchdog.

Decoded delivery is not proof of camera capture freshness or glass-to-glass
latency. A publisher looping old footage is a remaining source-health problem.
Likewise, the current gateway's token expiry/revocation does not terminate an
already-established stream; see its documented production blockers.

## Validation categories

Core tests cover authorization, endpoint policy, resource bounds, WHEP response/
cleanup/error handling, cancellation and late-session callbacks. Apple-native
unit tests cover receive-only SDP and bounded one-shot SDK callbacks. The native
reference probe uses the same `NativeVideoPeer`, `GatewayClient` and `WHEPClient`
as the application, reads an ephemeral synthetic MediaMTX stream and requires
advancing decoded frames plus successful DELETE.

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
