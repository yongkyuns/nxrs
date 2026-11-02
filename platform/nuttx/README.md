# NuttX platform integration

Nxrs separates **product platforms** from reusable NuttX build mechanics.

## Product platforms

`platforms/` contains checked-in product/qualification descriptions. One
selected platform owns:

- the NuttX board;
- Rust target/toolchain facts;
- Kconfig deltas and required/forbidden settings;
- concrete HAL provider bindings.

Applications and services do not know the selected board or provider identity.
Developers select a platform through Cargo:

~~~sh
cargo firmware --app event-demo --platform pico2-mock
~~~

The [CoreS3 navigation profile](../../docs/m5stack-cores3.md) adds raw UART/I²C
resources for Module GNSS M135; physical qualification is pending.

App entry settings come from the app's existing `Cargo.toml`; there is no
separate deployment file.

This is the Nxrs equivalent of the useful board/product-description role that
Zephyr's board DTS/overlays provide, without introducing Devicetree or another
runtime configuration framework.

## Build mechanics

`platform/firmware` contains the host-side Cargo frontend. It translates the
selected app + platform into explicit arguments for
`tools/build-nuttx-std-app.sh`.

The common shell backend is not the developer-facing build system. It owns the
steps Cargo cannot perform itself: NuttX/Kconfig configuration, prepared std
integration, ABI checks, NuttX Make/final linking and image generation.
The exact Apache source pins and the build-time NuttX, NuttX-apps, and Rust
library patchsets are documented in [upstream-patchsets](../../docs/upstream-patchsets.md).

`profiles/` contains older focused NuttX configuration recipes used by the
core-only simulator path. `qualification/` supplies NuttX application
registration/archive-linking rules for target tests. `std-app/` is generic
ordinary-Rust-main integration.

Reusable adapters and C ABI bridges live below `hal/`; product operating modes
remain in portable application code.

There is no runtime product launcher, global HAL object, checked-in deployment
graph, or app×platform wrapper-script matrix.
