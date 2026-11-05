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
includes them; see the remaining qualification notes below.

The isolation audit uses full Cargo package metadata and observed compiler
artifacts. It checks target packages separately from host build tools, retains
registry packages, rejects unknown artifact identities, and records the observed
sets. `provider-selection.json` describes profile bindings, including available
bindings that an application may not consume; it is not a compiled-package list.

## Exact application dependency selection

At actual build time the controller asks Cargo for its resolved graph using the
application manifest, enabled/default features, and **the exact target**. The
walk starts at the selected application's Cargo package and follows normal
dependency edges only. Unrelated workspace packages, dev-only dependencies,
inactive optional dependencies and build/proc-macro dependencies do not
activate runtime HAL functionality.

The selected graph answers two questions:

1. If the application consumes `nxrs-detector`, enable the model feature
   specified by the hardware/product profile. The model is not an app-owned
   option. Optional, renamed, workspace-inherited and transitive dependencies
   follow Cargo, rather than a second TOML traversal.
2. Stage a NuttX HAL's private C ABI sources only if its Rust HAL package is
   actually consumed **and** the profile binds that HAL to NuttX. The files
   are still compiled by the existing native image build, after target ABI
   selection. OS drivers and startup stay in NuttX.

For Xtensa, metadata uses the **generated, qualified NuttX JSON target spec**
and matching SDK compiler. Therefore selection occurs after generating that
spec, not during an SDK-free configuration dry run. The dry run reports
`model_features: null` and `model_resolution: pending-exact-target-cargo`
rather than falsely claiming the target's selected graph. Actual build
evidence replaces that pending field with the selected feature list. The
CLI diagnostic `profile --model-features` requires `--cargo-target` for
NuttX and supports ordinary `--features` and `--no-default-features`.

Tests in `platform/build/tests/cargo_selection.rs` exercise real Cargo feature,
workspace alias, inactive target and transitive service selection, and compile
an independent external camera app with its own Cargo workspace and lockfile.
The ordinary portable HAL isolation audit still checks actual compiler
artifacts (not just the metadata graph).

## Remaining independent qualification and optimization

**Build correctness and the HAL interface do not require another dependency
resolver, capability layer, service registry, generated application graph, or
new runtime framework.**

- For a **standalone package distribution** beyond an external Cargo app using
  checkout-local path dependencies, package the shared build script/source
  inputs with their owner and slim the non-publishable host build dependency
  where the consumer test proves it necessary. Current path dependencies
  still require the NXRS SDK checkout.
- For **repeated NuttX/Web firmware builds**, the default controller still
  regenerates native SDK trees and build outputs. The explicit shared Cargo
  target directory and NuttX std sysroot inputs permit some controlled reuse,
  but fully automatic persistent incremental SDK/native/Cargo reuse requires
  input-keyed caches, same-path header/config invalidation, SDK/compiler
  identity verification, and artifact-matched linker evidence. A cache without
  those conditions can give stale firmware and must not replace a clean
  qualification run.
- NuttX mock/unbound builds still resolve target-scoped BMI270/UBX driver
  dependencies. Host/core/WASM library graphs are isolated. Fully excluding
  drivers from mock NuttX builds requires explicit optional dependency
  selection before Cargo resolution, not a cfg emitted by a build script.
- Physical multi-device ownership is a separate HAL runtime concern: the IMU
  lease remains global. Supporting two physical instances requires explicit
  resource handles and per-device ownership, not a build-time resource graph.

Do not claim a firmware-size reduction without link maps or physical-device
qualification without target evidence. This work leaves the parallel message
and priority-progress qualification in #252 unchanged.
