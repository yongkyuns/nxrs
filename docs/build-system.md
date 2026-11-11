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

`--platform` uses the profile file that declares `default-for` for that family.
`cargo nxrs profile --platform wroom` shows the resolved WROOM default.
`--profile` selects an explicit product or qualification configuration, which
can inherit settings using `extends`. When you supply both options, the explicit
profile must belong to the same board and target family. Native builds target
the current host OS.

Start with the platform default for ordinary builds. The seven defaults live
in `platform/profiles/`, with shared WROOM settings in its `wroom/` subdirectory.
The configurations in `tests/profiles/` are explicit test inputs, such as
`pico2-mock` for a sensor-free Pico 2 build. They do not add platform defaults.
On a desktop, use `mock` for scripted providers and the navigation demo, or
`simulation` for coordinated IMU/GNSS data. A build without a profile checks
portable HAL contracts with hardware acquisition unbound.

Use `--overlay` to adjust devices, resources, or aliases on a profile with
[peripheral composition](peripheral-composition.md#bindings-and-overlays).

Platform admission does not imply every HAL is implemented or physically
qualified. CoreS3 sensor wiring still needs physical qualification. Desktop
camera replay, recording, and UDP use portable Cargo features. The Windows
profile admits detector acquisition. Web currently builds
the Emscripten thread qualification; hardware HALs are unbound. Stock
`wasm32-unknown-unknown` does not provide these std threads.

## Accepted follow-up and current behavior

The [declarative build/static-device decision](static-device-composition.md)
moves HAL-local host work into shared build preparation. IMU and GNSS now use
driver-owned catalogs, additive Cargo features, and canonical device instances.
The full pre-Cargo configuration and native-helper handoff is tracked in
[issue #246](https://github.com/yongkyuns/nxrs/issues/246).

Extend the existing pre-Cargo preparation path. Package-owned catalogs declare supported implementations and resource
constraints. Rust defines the configuration and constructor types. Shared resource
rendering supplies validated configuration and
concrete per-device declarations. There must be no chip-specific emitter,
hardcoded sibling catalog inclusion, profile-provided Rust source or redundant
`source` selector alongside ordinary feature-gated modules.

Driver availability uses optional dependencies and additive Cargo features.
Profiles choose physical instances; several instances may share one driver.
Feature activation must use valid Cargo edges before target compilation and work
for transitive/detached consumers without hardware choices in application or
service source/manifests. Cargo remains the dependency resolver.

Deleting HAL build scripts also requires replacing their `OUT_DIR` handoff,
per-package provider flags, native compilation/link outputs and freshness
tracking. Keep ABI source with its runtime owner and compile only consumed,
feature-enabled helpers with the selected target context. One global provider
flag cannot replace per-HAL selection.

The [follow-up plan](peripheral-composition-plan.md#static-device-follow-up)
requires a concrete generated-input contract for normal builds, plain Cargo
build/check/test/doc, no-profile contracts and detached packages. Generated
outputs stay outside source, carry target/configuration/schema/generator
identity, and reject stale inputs and concurrent-build contamination. The
existing native-header, helper-exclusion and incremental-evidence gates remain.

## Shared declarative sensor preparation

Each sensor catalogue imports its drivers' package-relative binding declarations.
For example, the GNSS catalogue declares:

```toml
[package.metadata.nxrs]
bindings = [{ dependency = "nxrs-ubx-driver", feature = "ubx" }]
```

Catalog version 2 declares compatible devices, bus constraints and a single
`implementation` identifier naming a checked-in type in the catalogue's
`implementations` module. Composition profiles/overlays remain version 1.
The renderer emits typed I²C, SPI, or UART bindings and one static instance per
configured endpoint. Named accessors and aliases borrow that instance. The HAL
owns acquisition and releases the instance only after delivery and cleanup retire.

`nxrs-build-support::preparation::prepare_package` accepts the consuming package
directory, resolved profile, target OS and owned output directory. The sensor
renderers use shared binding validation. The `build.rs` adapters call the
common `configure_hal()` adapter, which preserves per-package provider flags,
simulation configuration and native compilation/linking.

Profile resolution loads package declarations for explicit `[hal]` keys,
including a provider of `"none"` when its unselected physical devices must be
validated. It does not scan unrelated HAL catalogs. An isolated package without
a selected profile can compile its contract without an SDK or sibling HAL
directories. A selected complete profile may read its declared SDK metadata;
that does not add packages to the consumer's Cargo graph.

Manifest and catalog bytes participate in receipt dependencies,
the input fingerprint and Cargo rebuild tracking. The report's
`resolved-composition.binding-inputs` records their paths and hashes.
`binding-sources` retains the catalog paths relative to the SDK.
A copied consumer package must declare matching catalog contents; its
packaging-only manifest differences are permitted and its actual manifest is
also tracked. Generated files retain their timestamps when content is unchanged.
An output ownership record removes an obsolete generated Rust file when configuration is removed, while preserving unrelated outputs and native archives.

## Configuration and ownership

The application's firmware metadata table owns entry facts: binary, command,
priority and stack size. Optional `required-capabilities` metadata supplies early
admission checks; Cargo dependencies select components. App/service source and
manifests stay platform-free. Application-entry metadata under
`package.metadata.nxrs` accepts `firmware`, `required-capabilities`, and the boolean
`qualification-only` fixture marker. Build planning and deployment preflight both
validate this parent table and reject unknown fields, so misspelled metadata cannot
silently select firmware defaults or skip capability admission. Other tools' metadata stays
independent. Declarative profiles under `platform/profiles` select hardware
resources, capabilities, execution/model budgets and OS build settings. HAL
implementations stay in their owning HAL crates.

The [peripheral composition design](peripheral-composition.md) defines the
implemented versioned profile schema for device instances, resource references,
roles and ordered overlays. Its [implementation plan](peripheral-composition-plan.md)
records resolver/build-script agreement, input fingerprints, Cargo dependency
handling and detached-consumer checks. Use `cargo nxrs profile` to inspect a
resolved graph and repeat `--overlay` on profile inspection or build commands
to apply command-line overlays. Named profiles may also declare ordered
overlays. The build frontend and direct Cargo consumers both use the versioned resolver.

Currently, each HAL owns its local `build.rs`; it depends on `platform/build-support`,
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
`dependency_resolution` is `pending`, with null `model_features` and `driver_features`.
Resolving a NuttX custom target waits for its actual compiler and target spec.
Completed builds record the Cargo-selected packages and the model and driver
feature requests added by the frontend.
Dry runs do not claim compiler, SDK, binary-target or physical-resource qualification.

The IMU catalogue's `bmi270` and `lsm6dso` features and the GNSS catalogue's
`ubx` feature enable optional NuttX drivers. `cargo nxrs build` requests the
features for the configured devices in each consumed catalogue. For example,
`cargo nxrs build --app event-demo --platform cores3` selects BMI270 and UBX.
For a direct Cargo build with a physical profile, enable its catalogue features
explicitly. Mixed IMU profiles can select both drivers, and host driver tests
compile the implementations through dev-dependencies.

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
Cargo. A Make target exports its C compiler, archiver and flags into
`native-context.json`, with the target and configured-header digest.
The controller precompiles the consumed NuttX resource and HAL ABI archives.
HAL helpers follow the resolved profile binding and the required features
selected by Cargo. Package adapters validate and link the archives listed for
the build. Standalone Cargo, non-path dependencies and builds with Cargo-only
compiler environment overrides compile shims through their package adapters.
SDK preparation keeps its own build path. NuttX owns OS drivers and registration.

The controller checks current compiler dependencies before reusing an archive,
including headers that newly take precedence in the include search path.
`provider-selection.json` records the prepared archive paths and hashes.
Concurrent NuttX builds use separate output directories.

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
