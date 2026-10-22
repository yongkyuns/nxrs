# Nxrs Home

The iPhone client is for private home monitoring: check live cameras, see
whether video is arriving, and save footage for review without learning the
gateway's implementation. Existing Keychain enrollment and the authenticated
playback path remain the foundation.

## Product direction

- **Home:** a camera-first dashboard with a selected live view, room filters,
  favorites, camera naming, and useful loading, paused, empty and offline states.
- **Live view:** native video, full-screen and digital zoom, snapshot and bounded
  local video recording, reconnect, pause, and an accessible connection summary.
- **Activity:** optional gateway-owned motion clips, grouped by day and camera,
  authenticated playback, explicit Keep and recent monitoring status.
- **Recordings:** private snapshots and clips, dates/camera labels, filtering, favorites,
  playback, explicit sharing and deletion. No invented motion events or cloud history.
- **Settings:** home identity, appearance, playback preferences, gateway status,
  enrollment replacement, privacy and capability explanations.
- **Setup:** a welcoming guided flow, a private invitation-file import, and manual
  HTTPS enrollment. Existing enrollments continue to work without re-pairing.

Product language centers on checking in on home, live cameras and saved recordings.
Recordings contains manually captured clips and snapshots. Activity is separate
automatic motion history, available only when the gateway recorder is enabled.

The brand uses a custom home-and-lens mark, deep forest, warm ivory, and a restrained
lime accent. Native navigation, semantic type, 44-point controls, clear contrast,
dark mode and Reduce Motion behavior take priority over decoration. The brand
geometry remains editable source; no third-party logo or stock camera image is used.

## Feature boundaries

Only decoded frame arrivals can produce Live. A catalog's availability is a gateway
observation, not proof of source freshness. Synthetic sources remain labeled.
Snapshots and clips are user-initiated, device-local and excluded from cloud backup.
Manual clips stop after at most 60 seconds; the library has an explicit storage bound and
does not silently delete the user's captures. Optional gateway motion clips have
their own retention/quota; kept clips are exempt from automatic deletion. Audio, talk, PTZ,
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
`clients/ios/THIRD_PARTY.md`. Audio/talk, push notifications and continuous history
remain separate capabilities.

## Activity extension qualification

The gateway activity component and iOS timeline have their own tests and
[qualification boundary](../gateway/activity/README.md). The UI uses the existing
identity, palette and adaptive layouts. Its Debug-only sample activity is labeled
and cannot serve as physical-camera evidence. The prior physical acceptance below
predates Activity: it does not qualify the new timeline, motion accuracy or the
planned 72-hour run. Record actual new device/endurance results separately after
they finish. Existing camera firmware, USB supervision and native playback remain
independent of the gateway motion observer.

Development checks on 2026-10-21 passed: 52 core tests, 11 native macOS tests,
12 iOS simulator tests (6 UI workflows and 6 native capture tests),
architecture audit plus 23 regressions, 8 reference-harness tests and
3 editor regressions. Debug simulator and Release iPhone SDK builds passed with
Swift warnings treated as errors. Light/dark, landscape and largest-text screens
were visually reviewed. A separate private macOS harness used the running real
ESP32 stream with the same native player/capture code and verified a decoded JPEG,
a playable H.264 clip and WHEP cleanup. That harness used scoped test-certificate
trust; it does not establish iPhone rendering or the app's system-trust path.
Physical iPhone 14 Pro acceptance passed with the actual ESP32 stream: live video,
protected and backup-excluded JPEG saving, clip creation, and pause/reconnect.
The normal signed app was restored using its existing app identity and enrollment.
The user also confirmed return to Live over cellular with Tailscale, after
disconnecting USB and disabling Wi-Fi, and playback of a saved clip in Recordings.
This qualifies the tested development setup; the distribution SDK review above
remains a separate release gate.
Additional capture regressions verify adapted WebRTC crop coordinates and prompt
encoder-failure reporting; the same capture cases run on both macOS and iOS.
Camera-management acceptance also verifies favorites, room filtering and moving
the last camera out of the actively filtered room.
Saved-recording detail also receives a largest-text visual check: its date wraps,
actions stack, and the content scrolls rather than truncating controls. Detail
photos fit their frame so portrait images remain fully visible. CI retains the
fixture-only UI result bundle for three days to diagnose runtime differences.
