# Remote monitoring: implementation boundary and camera handoff

Tracks [#20](https://github.com/yongkyuns/nxrs/issues/20) and draft
[#21](https://github.com/yongkyuns/nxrs/pull/21). This is incremental M0/M1/M2 work,
not completion of those milestones or a production-ready surveillance system.

## Implemented boundaries

- `clients/ios/`: independently buildable Swift API/client and playback-state core.
  It is not yet an installable SwiftUI app or native WebRTC renderer.
- `gateway/control/`: independent Rust host service for authenticated camera lists,
  bounded short-lived playback grants and MediaMTX's internal HTTP authorization.
- `gateway/tests/`: shared contract tests, enrollment tests, and a real-process
  synthetic H.264/WHEP integration harness with a Python WebRTC receiver.
- `gateway/deploy/dependencies.json`: MediaMTX version and release-asset checksum.

See the [gateway instructions and limitations](../gateway/README.md) for executable
commands, credential generation, actual authorization semantics and remaining
production gates. Test implementation alone is not test evidence: retain the CI
run URL and commit in the PR/issue for each qualified revision. No physical
capture, home gateway, WireGuard connection or physical-iPhone result is implied.

## Coordinate before touching camera acquisition

Camera baseline inspected: main `0c70d2256e134aa6d977424a0eb26dfcfe811b83` on
2026-10-20. The user reports parallel camera work. No published in-progress camera
PR was identifiable at inspection; this does **not** establish that unpublished
work is absent. The coordination notes are on #20. Link the camera branch/PR when
published and recheck it before physical integration.

This work owns `clients/ios/`, `gateway/`, monitoring documents, and dedicated
hosted CI. It deliberately does not change `app/`, `service/`, `hal/`, `driver/`,
`platform/`, firmware workflows or root Cargo files. No competing camera provider,
driver, frame type or image transport is introduced. Do not flash hardware or
interrupt camera qualification for client/gateway tests.

| Camera effort supplies | Monitoring effort supplies |
| --- | --- |
| Capture/HAL/driver ownership and readiness | Gateway ingest adapter, not another capture owner |
| Qualified formats/resolutions and frame bounds | Encoding only when the actual source requires it |
| Frame lease/release lifetime and stop/restart behavior | Bounded sink/backpressure and reconnect policy |
| Sequence/timestamp meaning and clock domain | Stream freshness and latency instrumentation |
| Board/sensor/configuration and physical evidence | Labeled synthetic tests, then end-to-end evidence |

Before selecting MJPEG or encoded-video ingest, record the camera PR/commit;
hardware; formats actually measured; maximum/variable payload size; raw stride
and endianness; timestamp acquisition point and clock domain; sequence reset
rules; last legal read/release; readiness/wakeup; overload/drop behavior;
stop/cancel/restart; and sustained physical capture evidence. Do not add a
speculative HAL metadata ABI just to satisfy a viewer. Existing telemetry is not
an image stream; historical OV3660 bring-up is not current-path qualification.

## Product contracts

The device captures; the gateway ingests and optionally encodes; MediaMTX serves
WebRTC; a native iOS player will receive it. The Rust service owns catalog access
and scoped playback admission, never video transport. Reuse upstream media and
network components instead of implementing a media server or generic framework.

WireGuard is the initial **reachability profile**, not application authentication.
The client contains no VPN SDK, peer keys or private-IP camera identity. Stable
camera/gateway/site IDs, the [OpenAPI contract](monitoring-api.json), and the WHEP
player boundary must remain usable in a hosted deployment without a phone VPN.
Current private/hosted client cases are contract-level tests, not a working hosted
media proof. No cloud account logic belongs in camera firmware.

A media origin is enrolled explicitly; a grant cannot expand the trusted origin
set. Even two enrolled origins cannot exchange a WHEP session credential through
Location. No query credentials, redirects, HTTP downgrade or TLS bypass. Account
credentials stay on the enrolled control origin; media gets a different scoped
token. Secure app enrollment, certificate trust and Keychain storage remain open.

The server authorizes only configured viewer/site/camera scopes. Catalogs are
bounded to 64 cameras; pagination requires an explicit future contract, not
silent truncation. `availability: unknown` is intentional until real media
observations exist. Source classification stays visible; configuration alone is
not evidence of a fresh physical image.

Grants expire after at most 300 seconds with both wall and monotonic checks.
A configured global cap and per-viewer cap bound outstanding admission grants.
These are not yet active-media-session or fleet-wide quotas. Expiry/revocation
blocks new sessions but does **not** terminate established WebRTC sessions.
Durable identity/revocation and server-enforced active termination are release
blockers. The current local hard-revocation procedure removes the credential from
configuration and restarts both control and MediaMTX. A restart alone must not be
represented as persistent account revocation.

## WireGuard deployment preflight (not yet deployed)

Choose a reachable home endpoint, or a public WireGuard hub with outbound home and
phone peers for carrier-grade NAT. WireGuard does not supply automatic relay
selection. Hub hosting/bandwidth may cost money. Use per-peer keys, lost-device
revocation and narrow routes/firewall access to gateway services, not the whole
home LAN. Qualify route collisions, DNS, MTU, endpoint changes and keepalive needs.
The iPhone monitor will not manage another VPN app's private keys.

Only the VPN listener should require public exposure. Keep camera, RTSP, admin,
metrics and internal authorization private. The current service intentionally
binds only loopback; it still needs a hardened HTTPS front proxy and WireGuard
media routing before home deployment. Do not mistake the test-only aiohttp TLS
proxy for production software. Qualify both HTTPS/WHEP and ICE/media paths,
including container mappings and VPN-reachable advertised addresses. A loaded
page, API success or VPN handshake is not decoded video.

Later the gateway can authenticate/publish outward to reachable control and media
services. The phone receives the same grant shape and uses the same player.
TURN addresses media reachability, not exposure of private HTTPS signaling.
Decide continuous/on-demand publication, tenancy isolation, bandwidth and trust
boundaries explicitly. Transcoding/terminating gateways and servers can access
video; transport encryption is not camera-to-phone end-to-end encryption.

## Proposed budgets and separate evidence gates

These budgets are not measured performance promises; revisit them after camera
handoff and the pinned native WebRTC implementation.

| Measurement | Initial rule |
| --- | --- |
| Player startup watchdog | 15 s; no decoded frame means failure, never Live |
| Decoded frame gap | Mark stalled at 2 s and visibly mark/hide a frozen image |
| Client HTTP exchange | 15 s request / 20 s resource deadline, 1 MiB response cap |
| Gateway request body / handler | 16 KiB / 5 s; proxy separately bounds sockets and headers |
| Grant lifetime | At most 300 s; not a maximum established-session lifetime |
| Camera-to-display delay | Target p95 <= 1 s on declared LAN workload; cellular measured separately |
| Reconnect | Bounded retries/backoff, no credential-error retry storm |
| Sustained test | >= 60 min plus repeated switch/cancel/reconnect; no growing resources |

The pure Swift lifecycle reducer rejects retired-session callbacks and uses
monotonic event times. The player owner must serialize events and close actual
network/decoder resources, implement retries, and stop on background. Decoded
frames alone cannot detect a publisher looping old footage: capture timestamps
and end-to-end visual timing are still required. Network RTT is not glass-to-
glass latency. Synthetic Linux media, host Swift tests, iOS SDK compilation,
physical iPhone playback, cellular/WireGuard, and physical-camera capture are
separate evidence categories.

## Remaining implementation and production gates

Native Xcode/SwiftUI app, Keychain and libWebRTC WHEP negotiation/rendering;
production TLS/VPN packaging; source health/freshness observations; active-session
revocation and persistent enrollment; per-session quotas; dependency/security and
encoder-license review; actual iPhone/network transitions; and the agreed camera
ingest adapter with physical evidence. App Store, recording/retention, audio,
alerts and a full hosted fleet remain separately scoped follow-ups.

Use no shared defaults or credentials in source, URL queries, logs or artifacts.
Test fixtures are synthetic and ephemeral. The local reference trusts processes
on one host and uses plaintext RTSP only over loopback; authenticated/encrypted
physical ingest must be separately qualified. Phone-to-gateway WireGuard cannot
protect a different camera-to-gateway link. No home footage should enter CI.

The epic remains open; no full milestone is completed by these foundations alone.

## Primary implementation references

- [MediaMTX authentication](https://mediamtx.org/docs/features/authentication)
- [MediaMTX WebRTC/WHEP](https://mediamtx.org/docs/read/webrtc)
- [WireGuard endpoints and keepalive](https://www.wireguard.com/quickstart/)
- [Apple URLSession redirects](https://developer.apple.com/documentation/foundation/urlsessiontaskdelegate/urlsession(_:task:willperformhttpredirection:newrequest:completionhandler:))

The MediaMTX reference test uses its pinned release configuration, not a floating
manual as proof of interoperability. Further deployment components still need
explicit pins and evidence.
