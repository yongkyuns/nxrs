# Nxrs Monitor native client core

First implementation slice of [#20](https://github.com/yongkyuns/nxrs/issues/20).
This is an independently buildable Swift package for the future SwiftUI app,
**not yet an installable iPhone application or a WebRTC player**.

```sh
cd clients/ios
swift test --jobs 2
```

The package declares Swift tools 5.9, iOS 16 and macOS 13 minimums. The initial
local test evidence uses Swift 6.2.1 on Linux. macOS CI must qualify Foundation
on Apple platforms; neither host test establishes physical-iPhone playback.
There are no third-party Swift dependencies in this slice.

Implemented:

- Typed, authenticated camera list and playback-grant requests matching
  [the shared OpenAPI contract](../../docs/monitoring-api.json).
- Bounded foreground HTTPS transport (1 MiB response cap, including chunked
  responses), cancellation, no redirects/cookies/cache, and default platform
  TLS verification. No certificate bypass or HTTP fallback.
- Explicit enrollment-time media origins. Account credentials stay on the
  API origin; a distinct media credential is supplied in the playback grant.
  Relative WHEP session locations cannot change origin. No credentials in URLs.
- Opaque camera/site/gateway IDs, synthetic-source labeling, redacted credentials,
  grant expiry/camera validation, and explicit HTTP failure categories. No
  automatic retries, including no accidental repeated grant creation.
- A pure playback lifecycle reducer with session isolation, startup timeout,
  monotonic frame-stall detection, and late-callback rejection. Its owner must
  serialize calls and perform networking/resource teardown; the reducer does
  not close sockets or enforce server-side authorization.

`online` in the catalog is not `live` in the player. `live` means a decoder
reported incoming frames; it alone does not prove camera-to-display age. A
synthetic source must stay visibly labeled. Measuring actual capture freshness
requires the camera/gateway integration described in the handoff document.

Tests use the same JSON fixtures as the gateway contract tests. The grant token
is deliberately fake, its timestamp is fixed in the past, and tests inject a
clock. These fixtures cannot be used as deployment credentials.

Still required: the Xcode/SwiftUI application, Keychain enrollment, native
libWebRTC dependency pin/rendering, WHEP negotiation/ICE/PATCH/DELETE, decoder
wiring, cleanup/retry owner, and physical iPhone/WireGuard/cellular evidence.
The HTTPS transport's URLProtocol tests are not certificate/real-network tests.

See [architecture and coordination](../../docs/remote-monitoring.md).
