# Native multi-camera qualification — 2026-10-23

The iOS hosted integration and Android native integration passed against three
independent real Rust stream-service owners, Rust gateway/ingest and MediaMTX
1.21.2. Images/annotations are deliberately synthetic; this is not an ML accuracy
measurement or a claim about physical camera optics.

Android runs on an owned API 35 ARM64 emulator (Android 15 build
`AE3A.240806.043/12960925`) at 1080×2340 with the host GPU backend. Host: Apple
ARM64 / macOS 27; Xcode 27.1 (27A9275); FFmpeg 9.0.2. iOS uses a disposable iPhone
18 Pro simulator, runtime iOS 27.0, built with the iOS 27.1 SDK. No machine-wide
trust or production enrollment is changed. The fixture root is trusted only by
the Android test transport and the disposable iOS simulator.

`android-throughput.json` contains the final measured interval. The strict case
requires **both decoded and actually rendered FPS ≥14 per camera**, independently
of the more tolerant functional CI case. Pixel readback runs before/after that
interval for image identity; it is removed from the throughput interval because
it is a diagnostic synchronization operation absent in production. Native draw
and swap wall times, codec input/output, decoded delivery, rendered swaps and
process CPU are separate counters. Android process CPU does not include emulator
GPU/host work; these numbers are not battery or thermal qualification.

The near-17 FPS source has deterministic textured borders with exact red/blue
centers. Red implies a person box and blue implies clear, checking metadata
against actual displayed pixels. Its 1 ms polling budget can deliver the detailed
JPEGs through the real server's 4 KiB-per-poll write limit. The legacy flat-color
fixture remains separate: very small H264 frames batch in the publisher, and the
bounded viewer intentionally coalesces those bursts. It is unsuitable for a
steady-rendering throughput claim.

The Android suite exercised:

- Two real MediaCodec decoders and surfaces, with zero pixel/annotation mismatches.
- Independent pause/resume, full-screen peer reuse and a source failure/recovery
  while the other stream keeps advancing.
- Native JPEG snapshot and H264/MP4 recording, independently checked with
  Android's media extractor and camera-specific library records.
- Background release, healthy foreground SSE and video, and home replacement
  that joins obsolete callbacks before reusing the decoder budget.
- Frozen-image overlay expiry without further frames/network input, obsolete
  source-epoch rejection, late-frame fencing and no idle GPU redraw loop.
- Device-local Keystore ciphertext authentication, and rejection of the
  uninstalled fixture root by the default production transport.

The iOS suite exercised two independent source epochs, camera-specific capture,
full-screen reuse, third-camera admission and background/foreground retirement
through production app owners. Core/native suites and builds are summarized in
`validation.json`; `provenance.json` identifies the SDK, source components and APKs.
The gateway additionally passed its full Rust suite, Clippy and real active-viewer
revocation, unrelated-viewer isolation, outage, orphan/restart and corrupt-state
controls after replacing the arbitrary admission grace with a snapshot-start
retirement barrier.

No physical Android device was available. The iPhone 14 Pro was unavailable for
this run. A successful iPhone SDK release build and these simulator/emulator
results do not establish physical-device FPS, thermal behavior, vendor codec
coverage or private-network roaming. The committed runners allow those controls
to be extended without changing application protocols or adding platform flags.

`ci-followup.json` records the later control/surface fixes and their exact source
and APK hashes. That run uses the dense 720×1280 / 160-dpi layout and the
`swiftshader_indirect`/swangle emulator backend. Real Android input clicks
Watch/Pause, while switching pixel observation retains the same native surfaces.
Both streams passed the strict decoded/rendered throughput control with zero
pixel/annotation mismatches. The revised iOS fixture passed with cold boot outside
the media-runner deadline; a real process-tree failure control verifies timeout
cleanup even when a child ignores TERM. Original measurements above retain their
original provenance.
