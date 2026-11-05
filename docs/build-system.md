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

Each HAL owns its local `build.rs`; it depends on `platform/build-support`,
which contains profile validation and native build utilities. The CLI depends on
the same support package. A HAL never builds or depends on the deployment CLI.
The detector's shared host SDK adapter is an ordinary detector-owned build-support
package. The CLI and HAL build scripts share one Rust profile resolver. Model dependency
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
The controller queries Cargo's production graph using that complete build command,
including the real target/compiler and any Cargo configuration. Cargo resolves
optional and target dependencies, aliases and workspace inheritance. A profile's
detector model is added through the app's enabled direct detector dependency; the
model is not forwarded through services or selected in an app manifest. An
external transitive-only detector cannot be given a feature by Cargo and fails
with a direct-dependency diagnostic.

Dry runs reject backend-incompatible switches before any build starts. Their
`dependency_resolution` is explicitly `pending` and `model_features` is null;
resolving a NuttX custom target waits for its actual compiler and target spec.
Completed builds record the Cargo-selected packages and verified model features.
Dry runs do not claim compiler, SDK, binary-target or physical-resource qualification.

IMU's BMI270 dependencies and GNSS's UBX dependencies are normal target
dependencies only on NuttX. Host driver unit tests use dev-dependencies. Thus
portable contract and host/WASM mock **library** builds omit those driver graphs;
unit tests intentionally compile them. NuttX consumers include those ordinary
NuttX driver dependencies even when a profile selects a mock. Profile admission
is Rust module wiring, not an extra Cargo dependency resolver or provider feature.

The isolation audit uses full Cargo package metadata and observed compiler
artifacts. It checks target packages separately from host build tools, retains
registry packages, rejects unknown artifact identities, and records the observed
sets. `provider-selection.json` describes profile bindings, including available
bindings that an application may not consume; it is not a compiled-package list.

## Incremental builds and evidence

Normal builds preserve Cargo output. NuttX also retains its configured native
trees and private SDK, and Web retains its private SDK, when a completed build's
inputs and retained bytes still match. The identity includes profile/Kconfig,
entry settings, user features, pinned upstream/patch inputs, selected SDK and
compiler, and the build controller. Cargo and Make still run on every invocation;
they decide which compilation units are fresh. Generated files retain their
modification time when their content is unchanged.

Use `--clean` to discard owned compilation state and build again. Incompatible
configuration or SDK inputs also invalidate the retained configuration. A build
removes its completed-cache marker before work starts and writes it only after
all required checks succeed, so an interrupted build cannot qualify the next
reuse. A changed retained native header or private SDK file invalidates that
completed state.

Success evidence has a separate lifecycle. Each invocation removes prior image,
ABI, source-selection and provenance results and regenerates them from its Cargo
messages and actual artifacts. If Cargo reuses the Rust executable, its previous
linker receipt is accepted only with the completed cache and the exact executable
digest; `link-reuse.json` identifies that reuse. The native image is inspected
again. A failed rebuild leaves no current success bundle. Web recreates `site/`
and chooses its JS/WASM pair from the selected binary's Cargo artifact, so old
outputs from another binary cannot become the new site.

`platform/build/tests/nuttx_helper_ownership.rs` executes real Cargo and the host
C compiler to check unchanged freshness, same-path header/source changes and
exclusion of an unconsumed helper. `native_header_freshness.rs` retains the TLS
and detector checks, including changed symlinked architecture headers. These are
build ownership tests; target libc ABI and physical device behavior retain their
separate qualification gates.

## Native helper ownership

The NuttX controller configures Make and generates target headers before invoking
Cargo. A target in the ordinary application Makefile exports its selected C
compiler, archiver and flags into `native-context.json`, including the target and
configured-header digest. Each consumed HAL's local build script then compiles
only that package's private ABI sources into its own archive. The native build
continues to own OS drivers, registration and final image startup.

Helpers for optional functionality follow the owning package's Cargo feature
through `package.metadata.nxrs.platform.<os>.required-features`; the detector's
PICO compute context is one example. This gate does not select an OS or admit a
capability. Header, source and native-context changes are Cargo build inputs.
Profile-wide helper enumeration has been removed. Explicit `--target-c-source`
inputs are reserved for qualification/application integration code, not production
HAL helpers. Link maps and symbol checks still establish what the image retains.

## External applications and package boundaries

An external product has its own workspace, lockfile and handwritten `main()`.
Pass its manifest to the same frontend and select the NXRS profile/SDK root
explicitly with `--workspace-root`. For direct Cargo builds with a profile,
`NXRS_SDK_ROOT` supplies that root; in-checkout builds can discover it. The SDK
root provides profiles and pinned native inputs and need not be the application's
Cargo workspace. No source path outside an ordinary dependency package supplies
its build script or production Rust modules.

Shared acquisition/session code is the normal `nxrs-acquisition` dependency.
Linux Wi-Fi and BLE share the `nxrs-linux-gio` package and one private GIO native
archive; radio schemas and device policy stay in their HALs. Unbound and mock
builds do not compile the GLib helper. `tests/host/test_external_consumers.py`
copies only declared normal/build dependency packages to a detached location,
generates a new lockfile, and exercises renamed dependencies, real replay,
recording and UDP, mock sensor joins, and joint Linux radio linking. It also
rejects profile use without an explicit SDK root in that detached location.
This establishes external application/package composition, not a crates.io
publication contract or new device qualification.

Physical multi-device ownership is a separate HAL task. The current IMU lease
is global; two physical devices require explicit provider resources and
per-device ownership while preserving cleanup/quarantine. They do not require
a generalized build-time resource graph. This work does not change the parallel
channel/progress qualification in #252.
