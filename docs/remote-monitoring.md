# Remote monitoring: architecture, coordination and qualification

Tracks [#20](https://github.com/yongkyuns/nxrs/issues/20) and draft
[#21](https://github.com/yongkyuns/nxrs/pull/21). No full epic milestone is complete
merely because these components build. The PR records exact tested commits/runs.

## Implemented component boundaries

| Component | Responsibility |
| --- | --- |
| `clients/ios/` | SwiftUI/Xcode app, Keychain enrollment, dependency-free API/WHEP core |
| `clients/ios/Native/` | Checksum-pinned libWebRTC receiver, Metal view and native tests/probe |
| `gateway/control/` | Independent Rust host service for camera catalog and scoped playback admission |
| MediaMTX | Media distribution and its internal authorization callback; no video through the Rust API |
| Optional gateway encoder | Encode only when the actual camera output requires it |
| Camera firmware | Capture/HAL/driver ownership, unchanged by monitoring work so far |

See [iOS build/connect instructions](../clients/ios/README.md),
[gateway instructions and limitations](../gateway/README.md),
[OpenAPI contract](monitoring-api.json) and
[binary provenance/release gates](../clients/ios/THIRD_PARTY.md).

## Camera coordination and handoff

Main inspected on 2026-10-20: `0c70d2256e134aa6d977424a0eb26dfcfe811b83`.
The user reports parallel camera work; no identifiable published in-progress
camera PR was found at inspection. Unpublished/local work may exist. Link that
branch/PR on #20 and recheck before integration. Do not infer its absence from
search, introduce a competing provider/frame ABI, flash devices, or interrupt a
camera qualification run.

Monitoring owns `clients/ios/`, `gateway/`, these documents and dedicated hosted
CI. It does not change `app/`, `service/`, `hal/`, `driver/`, `platform/`, existing
firmware workflows or root Cargo files. No new runtime/encoder dependencies enter
MCU builds. Camera work supplies capture and buffer lifetime; monitoring consumes
its output through an agreed gateway adapter, never a second acquisition owner.

Required handoff: camera PR/commit; board/sensor/configuration; formats actually
measured, resolutions and maximum variable payload sizes; raw stride/endianness;
sequence reset semantics; timestamp acquisition point and clock domain; frame
lease/release contract and last legal read; readiness/wakeup, timeout, overload/
drop, cancel/stop/restart behavior; and sustained physical capture evidence.
Existing telemetry summaries are not an image stream. Historical OV3660 bring-up
is not qualification of the current HAL/application path. Choose ingest only
after this handoff rather than presuming JPEG/H.264 support.

## Product-compatible control and trust

WireGuard is the initial reachability profile, not application authentication.
Camera/gateway/site IDs are stable opaque identifiers, not IP addresses, stream
paths or VPN keys. The iPhone has no VPN SDK and makes ordinary HTTPS/WHEP calls.
The same grant/player contract can be used by a future hosted endpoint. Cloud
account logic does not belong in camera firmware.

Account, publisher and media credentials are distinct. A playback grant permits
only one authorized camera/action. The control service filters catalog access by
viewer/site/camera. A configured camera is not an observed live publisher:
`availability` stays `unknown` until real observations exist. Synthetic source
classification remains visible throughout tests and in the application.

Trusted media origins are enrolled explicitly alongside the credential in
Keychain. A playback response cannot expand the trusted set; WHEP Location cannot
send a media token to another origin, even another enrolled origin. No query
credentials, redirect following, HTTP fallback, TLS bypass or broad ATS exception.
Keychain uses WhenUnlockedThisDeviceOnly and no synchronization. Forgetting the
local record is not server-side revocation. Physical-device Keychain/onboarding,
ownership transfer and lost-phone flows still need qualification.

The current gateway stores bounded opaque grants with at most 300-second wall/
monotonic expiry and per-viewer/global limits. These are admission-grant limits,
not active-media/fleet quotas. Expiry or current in-memory revocation blocks new
admission but does **not** terminate an established stream. Durable revocation
and prompt active-session termination are production blockers. Present manual
hard revocation removes credentials from configuration and restarts both control
and MediaMTX; restart alone is not persistent revocation.

## Initial native playback profile

The native peer receives video only, without capturing microphone/camera input.
It gathers ICE completely before WHEP POST, obtains a fresh grant after gathering,
applies the answer and renders through the native Metal view. WHEP POST/DELETE,
bounded SDP, same-origin resource validation and partial-success cleanup exist.
Trickle PATCH, ICE restart and TURN Link discovery are **not yet implemented**;
initial LAN/VPN deployments must provide reachable advertised media candidates.
Reconnect creates a new peer/grant/session. The protocol adapter, not the UI or
camera firmware, is the extension point for additional network profiles.

Live requires advancing decoder deliveries. Missing frames visibly mark the
image stale; sustained stalls/peer failure trigger at most three retries with
2/4/8-second backoff. Authorization/trust/catalog/limit failures stop automatic
retry. Switching camera or backgrounding immediately closes the local peer and
attempts bounded DELETE; obsolete session work cannot replace the current peer.
Foregrounding reconnects the selected camera. No background streaming is claimed.

The no-frame watchdog is 15 seconds after answer application; negotiation itself
has bounded individual SDK/HTTP operations, not yet one 15-second end-to-end
watchdog. Rapid-switch, background/lock, network transitions, actual Metal output
and long-session resource counts require further device/runtime tests. Decoder
activity does not detect a publisher looping stale footage; true capture
freshness and glass-to-glass delay require camera timestamps/visual timing.

## WireGuard deployment preflight (not deployed yet)

Choose a reachable home endpoint, or a public hub with outbound home/phone peers
for carrier-grade NAT. Plain WireGuard does not discover relays. Use per-peer
keys, lost-peer revocation, narrow routes/firewalls and reachable DNS/certificates;
qualify subnet collisions, MTU, keepalive and endpoint changes. Hub hosting and
bandwidth may cost money. The monitor does not control another VPN app's keys.

Only the VPN listener should need public exposure. Keep camera/RTSP/admin/metrics
and the internal authorization listener private. The current Rust service is
loopback-only and trusts local processes. It needs a hardened HTTPS front proxy,
socket/header limits and qualified media routing before home deployment. The
Python TLS proxies in tests are fixtures, not production software. Verify both
HTTPS and ICE/media reachability, including container/VPN address advertisement;
a successful API call or VPN handshake is not a decoded-video result.

For hosted operation, a gateway can authenticate/publish outward to reachable
control/media services and return the same playback descriptor to the phone.
TURN addresses media connectivity, not exposure of private HTTPS signaling.
Decide continuous/on-demand publication, tenancy, bandwidth and trust explicitly.
Terminating/transcoding servers can see video; this is not camera-to-phone E2EE.
Phone-to-gateway WireGuard does not encrypt a separate camera-to-gateway link.

## Evidence and remaining gates

Keep core mocks, native macOS tests/decoding, unsigned Simulator/iPhone SDK builds,
physical-iPhone playback, cellular/WireGuard, and real camera capture separate.
A test harness existing is not evidence that it passed. Commit-specific results
belong in #21/#20. The native reference runs only on disposable hosted Macs and
temporarily trusts/removes its own generated certificate; it never disables TLS
verification or modifies a personal Mac. No credentials, raw logs, camera footage
or binary SDK are uploaded as fixtures/artifacts.

Current bounds: client responses 1 MiB; SDP 256 KiB; HTTP request/resource 15/20 s;
gateway bodies 16 KiB and handlers 5 s; catalog <=64 cameras. Socket/header limits
are a deployment obligation. Stale indication is 2 seconds plus polling latency.
Target p95 LAN camera-to-display delay <=1 s is an unmeasured design goal, not a
result; cellular must be measured separately. Require >=60 minutes of sustained
viewing plus repeated switch/cancel/reconnect, with no growing resources.

Still required: real iPhone/UI/privacy/Keychain tests; hardened HTTPS/WireGuard
packaging and cellular access; source freshness; durable identity/active-session
revocation; active quotas; comprehensive soak/recovery; dependency security and
encoder/SDK licensing/privacy review; and coordinated physical-camera ingest.
App Store/TestFlight, icons, recording/retention, audio, alerts and hosted fleet
operation remain separately scoped work. No production certification is claimed.

## Primary references

- [MediaMTX WHEP](https://mediamtx.org/docs/read/webrtc)
- [MediaMTX authentication](https://mediamtx.org/docs/features/authentication)
- [WireGuard endpoint configuration](https://www.wireguard.com/quickstart/)
- [WebRTC Objective-C peer API](https://webrtc.googlesource.com/src/+/refs/heads/main/sdk/objc/api/peerconnection/RTCPeerConnection.h)

Pinned-release tests, not rolling references, establish interoperability.
