# Nxrs Home

The iPhone client is a private window into home: open it, recognize the camera,
see whether video is arriving, and save a moment without learning the gateway's
implementation. Existing Keychain enrollment and the authenticated playback
path remain the foundation.

## Product direction

- **Home:** a camera-first dashboard with a selected live view, room filters,
  favorites, camera naming, and useful loading, paused, empty and offline states.
- **Live view:** native video, full-screen and digital zoom, snapshot and bounded
  local video recording, reconnect, pause, and an accessible connection summary.
- **Library:** private photos and clips, dates/camera labels, filtering, favorites,
  playback, explicit sharing and deletion. No invented motion events or cloud history.
- **Settings:** home identity, appearance, playback preferences, gateway status,
  enrollment replacement, privacy and capability explanations.
- **Setup:** a welcoming guided flow, a private invitation-file import, and manual
  HTTPS enrollment. Existing enrollments continue to work without re-pairing.

The brand uses a custom home-and-lens mark, deep forest, warm ivory, and a restrained
lime accent. Native navigation, semantic type, 44-point controls, clear contrast,
dark mode and Reduce Motion behavior take priority over decoration. The brand
geometry remains editable source; no third-party logo or stock camera image is used.

## Feature boundaries

Only decoded frame arrivals can produce Live. A catalog's availability is a gateway
observation, not proof of source freshness. Synthetic sources remain labeled.
Snapshots and clips are user-initiated, device-local and excluded from cloud backup.
Clips stop after at most 60 seconds; the library has an explicit storage bound and
does not silently delete the user's captures. Audio, talk, PTZ, motion detection,
continuous history and remote camera power/privacy controls are not advertised when
the current camera/gateway cannot perform them. Pause stops viewing on this phone;
it does not claim to disable the camera or other viewers.

## Design and verification sources

Task-local pinned skills (not runtime dependencies):

- [SwiftUI Expert](https://github.com/AvdLee/SwiftUI-Agent-Skill/tree/204dba7c67256a8b707ee3fbc5759a824191974f/skills/swiftui-expert-skill)
- [Mobile iOS Design](https://github.com/wshobson/agents/tree/0874a27b17a22e2b7c2e2bcd725768e0803cad06/plugins/ui-design/skills/mobile-ios-design)
- [Apple Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines/designing-for-ios)

Validate core persistence/import/quota tests, native capture/lifecycle behavior,
whole-app builds and simulator navigation. Visually inspect light, dark, large
type and landscape layouts. Preview fixtures must stay visibly identified and
must not count as physical-camera qualification. Keep real capture data, private
enrollment and simulator/device artifacts out of Git.

## Development acceptance

The baseline keeps iOS 16 support. Native Tab APIs are availability-gated, while
standard system navigation and sharing adapt to the installed iOS version.
At accessibility text sizes, camera rows and headings stack rather than squeezing
names between icons. There are no custom animations that override Reduce Motion.

The native reference receiver now saves a real decoded JPEG and a bounded clip,
then decodes that clip before it can report success. Core tests exercise storage
quota, transactional metadata, crash recovery and invitation redaction. UI tests
exercise setup validation, renaming, full-screen portrait/landscape, light/dark
appearance, largest Dynamic Type, local favorites, sharing and confirmed deletion.

Before shipping, repeat those workflows on the physical iPhone with the actual
camera, including pause/background, recording interruption and cellular/VPN
transitions. Distribution also needs the third-party SDK review documented in
`clients/ios/THIRD_PARTY.md`. Backend work for motion events, audio/talk and
continuous history is separate from this client release.

Development checks on 2026-10-21 passed: 52 core tests, 9 native tests, 4 simulator
UI tests, architecture audit plus 23 regressions, 8 reference-harness tests and
3 editor regressions. Debug simulator and Release iPhone SDK builds passed with
Swift warnings treated as errors. Light/dark, landscape and largest-text screens
were visually reviewed. A separate private macOS harness used the running real
ESP32 stream with the same native player/capture code and verified a decoded JPEG,
a playable H.264 clip and WHEP cleanup. That harness used scoped test-certificate
trust; it does not establish iPhone rendering or the app's system-trust path.
The redesigned build's final physical-iPhone acceptance remains pending.
