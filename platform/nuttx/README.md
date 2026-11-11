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
cargo nxrs build --app event-demo --profile pico2-mock
~~~

The [CoreS3 navigation profile](../../docs/m5stack-cores3.md) adds raw UART/I²C
resources for Module GNSS M135; physical qualification is pending.

App entry settings come from the app's existing `Cargo.toml`; there is no
separate deployment file.

This is the Nxrs equivalent of the useful board/product-description role that
Zephyr's board DTS/overlays provide, without introducing Devicetree or another
runtime configuration framework.

The [peripheral composition design](../../docs/peripheral-composition.md)
defines the implemented versioned profile schema for validated resources,
device instances, capability roles and explicit overlays. NuttX BSP/Kconfig
remains authoritative for MCU wiring. The
[implementation plan](../../docs/peripheral-composition-plan.md) records the
migration and the hardware qualification evidence that remains pending.

## Build mechanics

`tools/build` contains the shared Rust controller. It resolves the selected
app/profile, prepares private sources and SDKs, configures NuttX, invokes Cargo,
checks the ABI and performs the final Make/link/signing steps. Upstream build
and cryptographic SDK tools remain external commands. See the concise
[build interface](../../docs/build-system.md).

The exact Apache source pins and the build-time NuttX, NuttX-apps, and Rust
library patchsets are documented in [upstream-patchsets](../../docs/upstream-patchsets.md).

The simulator recipe and NuttX Make integration for product and qualification
entries live in `tools/build/nuttx/`.

Reusable adapters and C ABI bridges live below `hal/`; product operating modes
remain in portable application code.

There is no runtime product launcher, global HAL object, checked-in deployment
graph, or app×platform wrapper-script matrix.
