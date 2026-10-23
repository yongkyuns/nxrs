# Android dependencies

The client pins `io.github.webrtc-sdk:android:150.7871.04` from Maven Central,
a community binary distribution of WebRTC, not an official Google Android SDK
release. Gradle verifies the exact AAR SHA-256:

`1872dda4e3bf7d6ea6f7c6e11861332081c57081a1e62fc82fab72415c69c84c`

The vendor release is https://github.com/webrtc-sdk/android/releases/tag/v150.7871.04.
Its changelog records the update addressing compromised eMudhra certificates.
The vendor's `Licenses/WEBRTC.md` from that exact tag is bundled unmodified under
`app/src/main/assets/licenses/WebRTC.txt`, alongside the Apache 2.0 text and the
application's dependency attribution notice. About exposes these texts offline.
The wrapper repository's MIT license does not supersede the underlying WebRTC
BSD license, patent grant or third-party terms.

The monitor uses the SDK's Java MediaCodec adapters for H264. It does not embed
FFmpeg, a separate software H264 package or a detector engine. OS hardware
codecs are preferred, with OS MediaCodec software fallback. AndroidX/Compose,
Kotlin/kotlinx, OkHttp and Okio are Apache 2.0 dependencies; resolved artifacts and
metadata are checksum-pinned in `gradle/verification-metadata.xml`.

SDK upgrades require independent pixel-identity, codec token/drop/crop, callback
lifetime, TLS and dual-stream throughput qualification. Preserve notices for the
exact binary and review distribution requirements before publishing a signed
product. The private emulator integration tests do not certify every physical
Android codec or establish legal/distribution clearance.
