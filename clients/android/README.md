# Nxrs Home for Android

Native Kotlin/Compose client for Android 9 (API 28) and later. It uses the same
HTTPS catalog/grant, WHEP and detection contracts as the iOS client. No camera or
microphone permission is required: this application receives video.

Two visible cameras can play independently. Single/grid and full-screen viewing
reuse camera sessions; pause/retry affects one camera. Backgrounding retires both
peers and clears detections. Matched boxes expire without new network input.
Snapshots and silent clips (60 s / 32 MiB maximum) stay in a private, bounded
library with review, share and deletion. See
[client architecture](../../docs/client-camera-architecture.md) for ownership,
identity, resource limits and qualification requirements.

## Build and install

Install JDK 17 and Android SDK platform/build tools 35. Set `JAVA_HOME` and
`ANDROID_HOME`; keep machine paths in an untracked `local.properties` if using
Android Studio. The repository includes the Gradle 8.11.1 wrapper with its official
distribution checksum, dependency lockfiles and strict artifact/metadata checksums.

```sh
./gradlew :core:test :app:assembleDebug :app:lintDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Open the app and enter the home HTTPS address, viewer key, and a separate HTTPS
video origin if the invitation specifies one. Connect the owner's private
network/VPN first when required. Certificates must match the hostname and chain
to a system root or a root deliberately installed by the user. No insecure HTTP,
certificate-accept-all option, redirect fallback or client-side VPN is provided.
The invitation is encrypted with an unlocked-device Android Keystore key, excluded
from backup/transfer, and hidden from screen capture while enrollment is visible.

`assembleRelease` produces an **unsigned** APK. Product signing and distribution
are deliberately outside the development build; never commit a signing key or
viewer credential. The APK bundles the vendor's WebRTC and Apache license texts,
available from About. See [THIRD_PARTY.md](THIRD_PARTY.md).

## Qualification

The core JUnit tests consume the existing shared Swift/Kotlin fixture corpus.
Native instrumentation tests require a test-owned emulator with its screen awake.
`RenderLifetimeTest` and the Keystore corruption control run independently. The
multi-camera test skips when its private fixture is absent; the reference runner
requires its explicit PASS marker so a skip cannot masquerade as qualification.

Build the target and instrumentation APKs, the gateway workspace and the actual
Rust camera fixture. Install FFmpeg and the pinned MediaMTX from
`../ios/ci/dependencies.json`. Start an isolated API 35 emulator whose AVD name begins with
`nxrs-multicamera-`, then run from the
repository root:

```sh
clients/android/gradlew -p clients/android :app:assembleDebug :app:assembleDebugAndroidTest
cargo build --manifest-path gateway/Cargo.toml --locked
python3 gateway/tests/build_camera_source.py --output /tmp/nxrs-client-camera
ANDROID_SERIAL=emulator-5554 python3 clients/tests/multicamera_reference.py \
  --gateway "$PWD/gateway/target/debug/nxrs-gateway-control" \
  --ingest "$PWD/gateway/target/debug/nxrs-camera-ingest" \
  --mediamtx /path/to/pinned/mediamtx \
  --camera-source /tmp/nxrs-client-camera/nxrs_camera_source \
  --runner "$PWD/clients/tests/run_android_reference.py"
```

The runner scopes its own CA to a loopback-only test transport, copies its private
configuration through ADB stdin, reverses only its two allocated TLS ports, and
removes the configuration/forwards afterward. It does not install a CA on Android.
`NXRS_ADB` optionally selects a stable platform-tools binary; `ADB_SERVER_SOCKET`
can isolate the test ADB server. `NXRS_CLIENT_REPORT` writes sanitized measured
FPS/CPU counters. Keep generated credentials, private logs and footage out of Git.
The entire AVD is disposable; shut it down after qualification.

A software-rendered emulator is useful for functional tests but is not a phone
performance claim. The default functional case admits emulator overload (minimum 4 FPS per stream).
Use `--case throughput` for the stricter 14 FPS decoded/rendered control.
Record its GPU backend, codec input/output, rendered FPS and
process CPU separately. Actual Android hardware, thermal/battery behavior,
private-network roaming and vendor codec coverage require physical-device tests.
