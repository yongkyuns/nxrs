# Native client dependencies

The `Native` Swift package embeds the WebRTC M155 XCFramework distributed by
[stasel/WebRTC release 155.0.0](https://github.com/stasel/WebRTC/releases/tag/155.0.0).
It is a community-built binary, **not an official Google or Apple binary SDK**.
The package pins the release URL and SHA-256
`cb9adacf2d6bbf598d1d9e3e6c16d8fb97ca987b5b697746891828f9dc068ffe`,
as specified by that release's Package.swift. SwiftPM verifies it at download.
The dependency-free core package does not download or link this framework.

[Upstream WebRTC](https://webrtc.googlesource.com/src/) is BSD-licensed and includes
additional third-party notices and a patent grant. Preserve upstream LICENSE,
PATENTS and third-party notices for the exact shipped build; the wrapper
repository's license alone is not a substitute. The framework's binary origin,
linked codecs, privacy manifest/signature, distribution notices and App Store
requirements remain release-review gates. No legal/distribution clearance is
claimed by a successful test build. A reproducible source build can replace this
binary without changing the app/API contracts.

MediaMTX 1.21.2 in the reference tests is MIT-licensed. The 1.21.2-nxrs.1
rebuild uses the shared [gateway recipe](../../gateway/deploy/dependencies.json)
and [native advisory procedure](../../gateway/deploy/media-engine.md). FFmpeg is a test-host dependency; its exact reported build is not
a pinned encoder distribution for a product. No camera footage or binary SDK is
committed to this repository.
