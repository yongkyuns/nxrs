# Build and platform selection

`cargo nxrs` is the host-only Rust build controller in `platform/build`. It owns
profile resolution, SDK preparation, Cargo invocation, NuttX configuration and
linking, artifact checks, signing and offline factory packaging. Its dependencies
are build dependencies; they do not enter firmware.

```sh
cargo nxrs platforms
cargo nxrs apps
cargo nxrs build --app home-cam --platform wroom
cargo nxrs build --app event-demo --platform cores3
cargo nxrs build --app std-demo --platform macos
cargo nxrs build --app event-demo --profile pico2-mock --dry-run
cargo nxrs build --help
```

The seven platform families are:

| Platform | Hardware / environment | Default profile |
| --- | --- | --- |
| `wroom` | ESP32-S3 WROOM / Freenove N16R8, NuttX | `esp32s3-camera-stream-startup` |
| `cores3` | M5Stack CoreS3, NuttX | `m5stack-cores3-gnss` |
| `pico2w` | Raspberry Pi Pico 2 W / RP2350, NuttX | `pico2w-radio` |
| `linux` | Native Linux | `linux` |
| `macos` | Native macOS | `macos` |
| `windows` | Native Windows | `windows` |
| `web` | Browser, `wasm32-unknown-emscripten` | `web` |

`--profile` selects an explicit product or qualification configuration. With
`--platform`, it must belong to that family. Qualification profiles such as
`pico2-mock` and `mps2-an521-mock` are configurations, not additional public
platform families. Native builds target the current host OS.

Platform admission does not imply every HAL is implemented or physically
qualified. CoreS3 sensor wiring still needs physical qualification. Windows has
file-camera, storage, IP transport and detector bindings. Web currently builds
the Emscripten thread qualification; hardware HALs are unbound. Stock
`wasm32-unknown-unknown` does not provide these std threads.

## Configuration and ownership

The application's firmware metadata table owns entry facts: binary, command,
priority and stack size. Optional `required-capabilities` metadata supplies early
admission checks; Cargo dependencies select components. App/service source and
manifests stay platform-free. Declarative
profiles under `platform/profiles` and `platform/nuttx/platforms` select hardware
resources, capabilities, execution/model budgets and OS build settings. HAL
implementations stay in their owning HAL crates.

The CLI and HAL build scripts share one Rust profile resolver. Model dependency
features follow the profile automatically. There is no shell environment-file
interpreter or Python platform resolver. Existing qualification entry scripts
are compatibility adapters; numerical reference/oracle tests remain independent.

Upstream NuttX Make/Kconfig/configure scripts, compilers, CMake and the pinned
`imgtool`/Espressif SDK remain external tools. Python remains a prerequisite for
those vendor tools and independent qualification tests. Compiler installation
and experimental compiler/OS research harnesses remain separate setup/test work.

## Inputs and outputs

Prepare the pinned sources explicitly; Cargo build scripts do not fetch:

```sh
cargo nxrs fetch tls
cargo nxrs fetch detector
```

NuttX needs its existing qualified toolchains. Rust discovers the pinned Xtensa
bundle beneath `target/qemu-tools` (or `NXRS_QEMU_TOOLS_DIR`) directly. Explicit
native-board setups can supply their compiler paths and SDK as before. A private
SDK copy receives checked patches; the installed SDK is not modified. The final
build checks the actual std source, device libc ABI, ELF, symbols and Kconfig.

Signed profiles retain the existing required inputs:
`NXRS_FIRMWARE_SIGNING_KEY`, `NXRS_FIRMWARE_VERIFY_KEY`,
`NXRS_FIRMWARE_VERSION` and `NXRS_FIRMWARE_SECURITY_COUNTER`. Packaging verifies
against the independently supplied public root before publishing an image.
Build/package commands do not flash, provision or program eFuses.

Default output is `target/build/<binary>/<profile>/`; `--out` overrides it.
An ownership marker prevents reuse of unrelated output directories. Empty
folders and regular log-only folders can be adopted for qualification drivers.
Rebuilds clear old success evidence. Provider/profile hashes, SDK patch records,
ABI/source checks and final image hashes accompany NuttX artifacts.

Use `--manifest-path` and optional `--bin` for qualification binaries. The Web
builder accepts an optional `--html` template containing one `__NXRS_SCRIPT__`
marker and publishes its verified JS/WASM pair under `site/`.

Relative build paths (`--manifest-path`, `--out`, `--html` and
`--target-c-source`) are relative to the selected NXRS workspace root. The
standalone controller accepts `--workspace-root <path>` before its subcommand;
this also applies to `profile`. `--help` and `build --help` need no workspace.
Application discovery reports malformed app manifests instead of hiding them.

## Cargo component selection

Components are ordinary Cargo packages. Additive Cargo features may enable
optional algorithms, formats, integrations and driver code. App/service code
remains platform-free; OS selection stays at HAL module/factory boundaries.
`probe` remains an explicit diagnostic opt-in. The detector's model features
still follow the selected product profile.

`build` accepts repeated `--features` (or the compatible `--feature` spelling)
with comma- or space-separated values, plus `--no-default-features`. The same
feature arguments reach the native, NuttX and Web Cargo invocations. Cargo owns
the dependency and feature semantics, including dependencies' own defaults.
Dry runs reject backend-incompatible switches before any build starts; they do
not claim compiler, SDK, binary-target or physical-resource qualification.

IMU's BMI270 dependencies and GNSS's UBX dependencies are normal target
dependencies only on NuttX. Host driver unit tests use dev-dependencies. Thus
portable contract and host/WASM mock **library** builds omit those driver graphs;
unit tests intentionally compile them. A mock library targeting NuttX still
includes them and is part of the remaining selection work below.

The isolation audit uses full Cargo package metadata and observed compiler
artifacts. It checks target packages separately from host build tools, retains
registry packages, rejects unknown artifact identities, and records the observed
sets. `provider-selection.json` describes profile bindings, including available
bindings that an application may not consume; it is not a compiled-package list.

## Remaining-work case after the Rust controller migration

The baseline is [#254](https://github.com/yongkyuns/nxrs/pull/254), merged as
`36627ce`. It replaced the product shell/Python orchestration with one Rust
controller, introduced real TOML parsing, and retained Cargo, NuttX Make/Kconfig,
native ABI checks and independent qualification programs. Keep that boundary:
the next work should remove duplicate dependency interpretation and improve
reuse, without adding a capability solver or generated application graph.

This follow-up implements ordinary feature controls, target-scoped sensor
dependencies, complete isolation evidence, explicit-root/help/preflight fixes,
and native-header input tracking. It does not enable persistent firmware build
caches or independently package the HALs. The following order separates those
remaining changes and makes their acceptance measurable.

### 1. Finish exact dependency selection

- Replace `profile.rs::uses_detector`, which currently follows local manifest
  paths, skips optional dependencies, and traverses inactive target tables.
  Preserve automatic profile/model selection with ordinary Cargo inputs; use
  Cargo's selected dependency information wherever inspection remains necessary.
  Do not add another dependency resolver or assume `metadata.packages` is the
  application's compiled closure.
- Carry the actual manifest, enabled features/defaults, target and compiler
  through that decision. A host `cargo metadata --filter-platform` cannot query
  the custom Xtensa NuttX target without its qualified compiler/target spec.
  Preserve SDK-free configuration dry runs or explicitly report that dependency
  resolution remains pending; do not substitute a host graph for a target graph.
- Prove model activation for an enabled optional detector dependency, exclusion
  for an inactive target dependency, and correct package aliases/workspace
  inheritance. Verify the resulting feature arguments with Cargo itself.
- If contract/mock builds on NuttX must also omit sensor drivers, select their
  optional dependencies before Cargo resolution. `build.rs`-emitted cfg cannot
  activate or remove dependencies. Upfront cfg in rustflags is a different
  mechanism, but the controller must deliberately propagate it; the NuttX
  backend currently constructs its own encoded rustflags.

### 2. Make development rebuilds incremental

- Preserve Cargo output and an isolated configured NuttX tree across compatible
  builds; continue invoking Cargo and Make. Keep an explicit clean qualification
  path. Current NuttX/Web builds still reset their generated build directories.
- Track all native ABI/config inputs before enabling reuse. TLS and detector
  adapters now watch the configured NuttX header tree, including symlinked
  architecture headers. Their Cargo regression checks unchanged freshness,
  same-path header edits, and rejection after required headers become invalid.
- Verify cached SDK/compiler identity and patch hashes. Associate cached linker
  evidence with the exact artifact digest, or regenerate inspection evidence.
  Clear current success records separately from reusable compilation state.
- Acceptance: an unchanged second build reuses work; Rust and native-header
  edits rebuild their actual dependents; configuration/SDK changes cannot reuse
  incompatible objects; each evidence bundle describes the image it accompanies.

### 3. Let each HAL own its NuttX C-helper compilation

`native_helpers()` currently selects C sources from every NuttX-bound HAL in the
profile. Move private helper compilation into the owning crate after the
controller prepares the actual NuttX headers, SDK context and compiler/ABI
settings. Leave OS drivers and image startup in the native build. Acceptance:
an app without a HAL dependency does not compile its private C sources; an app
using it passes the existing real-target ABI/link checks. Link maps must support
any claim about retained image bytes; source exclusion alone is not that proof.

### 4. Qualify independent application/package reuse

Start with one application outside `app/`, with its own workspace and lockfile,
normal Cargo dependencies and handwritten `main()`. Distinguish its workspace
from the NXRS SDK/profile root. Test renamed/inherited dependencies and Cargo
binary discovery rather than adding app-specific build wrappers.

HALs still depend on checkout-relative build scripts/shared source and the
non-publishable full `nxrs-build` package. If independent package consumption
requires it, move build scripts/sources inside their packages and extract only
the small reusable build support. Do not split the runtime into more framework
layers. Tighten NXRS-owned metadata field validation as part of the external
consumer test, including mistyped entry fields and unknown resource settings.

Physical multi-device ownership is a separate HAL task. The current IMU lease
is global; two physical devices require explicit provider resources and
per-device ownership while preserving cleanup/quarantine. They do not require
a generalized build-time resource graph. This work does not change the parallel
channel/progress qualification in #252.
