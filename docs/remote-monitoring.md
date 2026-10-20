# Remote monitoring: implementation boundary and camera handoff

Tracks [#20](https://github.com/yongkyuns/nxrs/issues/20). This is a first M0/M2
foundation, not completion of either milestone. The initial delivery is a native
Swift client core, an OpenAPI contract, shared fixtures, and focused tests. No
physical capture, gateway deployment, VPN connection, or decoded video is claimed.

## Coordinate before touching camera acquisition

Baseline inspected: main `0c70d2256e134aa6d977424a0eb26dfcfe811b83` on 2026-10-20.
The user reports parallel camera work. At inspection no published in-progress
camera PR was discoverable; this does **not** establish that local/unpublished
work is absent. The coordination note is on #20. Link the camera branch/PR there
when published and recheck it before physical integration.

This change owns `clients/ios/`, the new `gateway/` boundary, these monitoring
documents, and its dedicated hosted CI. It deliberately does not change `app/`,
`service/`, `hal/`, `driver/`, `platform/`, firmware workflows or root Cargo files.
No competing camera provider, driver, frame type, or image transport is introduced.
Do not flash hardware or disrupt another camera qualification run for client tests.

| Camera effort supplies | Monitoring effort supplies |
| --- | --- |
| Capture/HAL/driver ownership and readiness | Gateway ingest adapter, not a second capture owner |
| Actual formats/resolutions and frame-size bounds | Encoding only when the qualified source requires it |
| Frame lease/release lifetime and stop/restart semantics | Bounded sink/backpressure and explicit reconnect policy |
| Sequence/timestamp meaning and clock domain | Stream freshness and latency instrumentation |
| Board/sensor/configuration and physical evidence | Labeled synthetic client/transport tests, then end-to-end evidence |

Before choosing MJPEG or encoded-video ingest, record: camera PR/commit; hardware;
formats actually measured (not merely enum values); maximum/variable payload size;
stride/endianness for raw data; timestamp acquisition point and clock domain;
sequence reset rules; frame ownership and last legal read; readiness/wakeup;
overload/drop policy; stop/cancel/release behavior; sustained capture evidence.
Do not add a speculative metadata ABI to the camera HAL just to satisfy a viewer.
The existing telemetry summary is not an image stream. Historical OV3660 bring-up
is not qualification of the current application/provider path.

## Product boundaries

The device captures; a home gateway ingests and optionally encodes; MediaMTX serves
WebRTC; the native iOS player receives it. A small Rust control service returns
an authorized catalog and scoped playback grants. The client never needs camera
hardware types or MediaMTX administrative credentials. Reuse existing media,
encoding and networking implementations rather than creating a general framework.

WireGuard is the initial reachability profile, **not** application authentication.
The monitor uses HTTPS and WHEP with an ordinary endpoint policy; it contains no
VPN peer keys, VPN SDK, or private-IP camera identity. The same models/client are
fixture-tested with a private gateway and hosted gateway. This is a contract-level
proof only, not the epic's required working non-VPN media deployment.

The [OpenAPI document](monitoring-api.json) is the shared boundary. IDs are stable,
opaque ASCII identifiers; the server maps them to internal stream paths. Site and
gateway scope permit multiple homes/cameras without implementing SaaS here. The
initial list is bounded to 64 visible cameras; pagination is a later explicit
contract change, not silent truncation. Optional response fields can be additive;
new required fields or changed enum meanings require versioning/review.

A media origin is enrolled explicitly and cannot be silently expanded by a grant.
Even two enrolled origins cannot exchange a WHEP session credential via Location.
Gateway base paths are preserved. Query credentials, redirects, HTTP downgrade,
and certificate bypass are rejected. Enrollment, including certificate trust and
origin changes, remains unfinished; users must not type arbitrary untrusted URLs
and forward an existing account credential to them.

Grants use a separate media token and identify exactly one camera. The client
checks version, camera, endpoint, token shape and expiry after the response. The
server MUST enforce viewer/site/camera policy, audience, a maximum 300-second
issue lifetime, admission limits, and revocation. The client checks expiry with
wall time (a wrong phone clock can cause rejection) and uses monotonic time only
for playback freshness. Grant renewal must be coordinated with the WHEP adapter.
An expired grant does not prove an existing media session has ended: define and
test server-driven termination/revalidation before release. The initial client
code is not an authorization server or cryptographic token verifier.

## WireGuard deployment preflight (planned, not deployed)

Choose either a reachable home WireGuard endpoint or a public WireGuard hub with
outbound home/phone peers for carrier-grade NAT. Plain WireGuard does not provide
automatic relay discovery. Hub hosting/bandwidth may cost money. Keep per-peer
keys, revoke lost devices, and limit routes/firewall rules to the intended gateway
services rather than exposing the entire home LAN. Check route collisions, DNS,
MTU, endpoint changes and keepalive needs. Do not embed VPN private keys in this app.

Only the VPN listener should need public exposure in the initial profile. Keep
camera, RTSP, admin, metrics and control internals private. HTTPS still requires
valid trust; a VPN is not permission to disable TLS checks. Qualify both HTTPS
session setup and ICE/media address/port reachability, including container mapping.
A loaded page, successful API call or VPN handshake is not a decoded-video test.

Later a gateway can authenticate/publish outward to reachable control/media
services. The phone receives the same grant shape and uses the same player,
without a separately configured VPN. TURN addresses media reachability, not
exposure of private HTTPS signaling. Explicitly decide continuous/on-demand
publication, tenancy isolation, bandwidth and trust boundaries. A transcoding or
terminating gateway/cloud can access video; do not claim camera-to-phone E2EE.

## Initial validation budgets and evidence

These are proposed test budgets, not measured performance promises. Revisit after
camera handoff and pinned gateway/libWebRTC deployment:

| Measurement | Initial budget / rule |
| --- | --- |
| Player startup watchdog | 15 s; no decoded frame means failure, never Live |
| Last decoded frame gap | Mark stalled at 2 s; visibly retain or hide the stale image |
| HTTP control exchange | 15 s request / 20 s resource deadline; 1 MiB response cap |
| Grant lifetime | Server maximum 300 s; no silent permanent tokens |
| Camera-to-display latency | Target p95 <= 1 s on a declared LAN test workload; cellular measured separately |
| Reconnect behavior | Bounded retries with capped backoff; no credential-error retry storm |
| Sustained test | >= 60 min plus repeated connect/switch/cancel cycles; no growing queues/resources |

The pure lifecycle reducer rejects callbacks from retired sessions and uses
monotonic event times. Its owner must close the actual network/decoder resources,
serialize events, implement retries, and call stop on background. A decoded-frame
callback alone cannot detect an encoder looping a stale image: capture timestamps
and end-to-end visual timing are still needed. Never substitute network RTT for
camera-to-display delay or synthetic-source success for hardware qualification.

Local evidence for this slice: Swift 6.2.1/Linux unit tests and JSON-schema/shared
fixture tests. Hosted macOS tests are separate evidence; physical iPhone SDK,
TLS/network behavior, media decoding, Wi-Fi/cellular transitions, WireGuard,
source staleness, access revocation and hardware capture remain acceptance gates.

## Threat boundaries and next slices

No credentials in source, URL queries, error bodies, logs or CI artifacts. Use
Keychain for the future app and server-side secret handling for deployments. The
fixtures have fake, expired tokens. Distinguish synthetic and physical sources in
the catalog/UI. Do not upload home footage as a test fixture.

Authenticate/encrypt camera ingest before production exposure or name the lack
as a production blocker: phone-to-gateway WireGuard does not protect this link.
Retain separate viewer/publisher/admin permissions and minimal health information.
Pin/review MediaMTX, libWebRTC, encoder build/licenses and upgrades before release.
No gateway or WebRTC dependency version is qualified by this dependency-free core.

Next: (1) Rust control service plus pinned authenticated MediaMTX reference-source
integration, including admission and active-session revocation tests; (2) Xcode/
SwiftUI app, Keychain and native libWebRTC WHEP player; (3) physical-iPhone LAN and
WireGuard cellular tests; (4) agreed camera adapter and physical-source evidence.
The epic stays open and its milestone checkboxes remain unchanged by this slice.

## Primary implementation references

- [MediaMTX authentication](https://mediamtx.org/docs/features/authentication)
- [MediaMTX WebRTC/WHEP reading](https://mediamtx.org/docs/read/webrtc)
- [WireGuard endpoint and keepalive configuration](https://www.wireguard.com/quickstart/)
- [Apple URLSession redirect handling](https://developer.apple.com/documentation/foundation/urlsessiontaskdelegate/urlsession(_:task:willperformhttpredirection:newrequest:completionhandler:))

These rolling references were consulted on 2026-10-20; deployment versions still
need explicit pinning and interoperability evidence in the next slice.
