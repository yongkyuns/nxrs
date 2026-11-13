# Drivers and HALs

Hardware access is split between NuttX board support, Rust resource adapters, device drivers, and capability interfaces. This chapter helps you choose the right integration point, add a peripheral, and understand which behavior still needs a board test.

Start with a working [NuttX build](firmware.md) and the [architecture overview](overview.md). You need the board schematic and the selected BSP's resource configuration before changing physical wiring.

## NuttX owns board support

The NuttX BSP configures pins and controllers, initializes devices, and registers OS resources. Platform profiles select the configuration used by a firmware build. Start by checking that the BSP exposes the resource the application needs.

There are two common arrangements. NuttX can own the chip driver and expose a device interface, or it can expose a raw bus that a Rust chip driver uses. Choose one owner for the chip protocol. A profile declaration alone cannot create a controller or driver the BSP has not initialized.

## Use std for standard operations

Files, byte streams, sockets, time, and synchronization can use `std` directly. A serial stream can use an owned file handle and `Read`/`Write`; IP communication can use `std::net`.

Device configuration, ioctls, cancellable readiness, and radio-control operations may need a Rust adapter over native APIs. Keep those bindings small. Validation, ownership, retries, and cleanup stay in Rust.

| Operation | Appropriate boundary | What stays below the application |
| --- | --- | --- |
| Read or write an ordinary file | `std::fs` and `std::io` | Filesystem mount and native device setup |
| Exchange IP packets | `std::net` or an intentional packet-output contract | Interface and radio configuration |
| Configure a peripheral bus transaction | Resource adapter using a Rust bus trait | Native request layout and bus descriptor |
| Read or configure a particular chip | Chip driver | Register protocol and device-specific conversion |
| Acquire a managed subsystem | Capability HAL | Concrete resource selection, callbacks and acquisition lifetime |
| Decide what a device result means to the product | Application or reusable service | No board selection is needed here |

The [interfaces guide](interfaces.md) explains feature selection. The [Rust API reference](../api.md) provides public capability and chip-driver documentation, with explicit source links for the NuttX-only bus and GPIO resource types.

## Resource adapters and chip drivers

Resource adapters expose bus or pin operations such as I²C transactions and GPIO interrupt inputs. A chip driver implements the device's register protocol using those resource interfaces. The driver should not need to know which application is using it or how the product names its logical devices.

Use existing Rust bus traits where they express the operation. Keep controller initialization and electrical configuration with the BSP rather than duplicating them in each driver.

The current adapters are concrete integrations. For example, the [I²C adapter](../../driver/i2c/) owns NuttX bus transactions, and the [GPIO adapter](../../driver/gpio/) currently supports rising-edge interrupt registration and input reads. Chip drivers consume the relevant Rust traits. A host test can inject a scripted bus to exercise the protocol without introducing a pretend host hardware backend.

## Capability HALs

A capability HAL presents a subsystem interface, such as storage, transport, a camera, or a data-acquisition device. It owns its public Rust API, errors, resource lifetime, and OS-specific implementation selection.

The application can acquire a capability through its documented constructor or select a named device through the portable catalogue. Services consume that interface rather than constructing board-specific drivers. A synchronous driver call and a streaming acquisition session are different APIs because their lifetimes differ.

For example, a data logger can separate input acquisition from formatting, recording, and transmission. The record format and processing code can remain ordinary Rust when a supported hardware binding changes. This reuse still depends on the new device meeting the required rate, units, capacity and timing contract.

### Device identity and acquisition

For capabilities using the static device catalogue, a named accessor identifies a canonical configured device. Several aliases can refer to that same device; an alias is not an additional physical instance. Sharing the reference does not create another active acquisition. The device's acquisition API enforces its ownership contract.

Other capabilities have operation-specific factories. Do not assume every HAL has a universal `open()` method or identical exclusivity semantics. Read the owning API: a synchronous output, a packet sink and an acquisition session have different resource needs. The [static-device reference](../../docs/static-device-composition.md) describes the catalogue's implemented scope and ownership rules.

### Interrupts, readiness and shutdown

A hardware interrupt is a notification that a provider may have work. Bounded notification happens in interrupt context; parsing, bus access and ordinary Rust channel submission happen in task context. A consumer receives typed results after the provider has interpreted readiness and completed the operation.

Keep callbacks, buffers and descriptors alive until the producer has stopped. For an explicit session, request cancellation, wake blocked waits, and join before considering the resource available for another acquisition. Report a cleanup failure without releasing a resource that might still be in use. [Event communication](../../docs/concurrency-event-communication.md) specifies these lifecycle requirements.

## Source layout

| Location | Responsibility |
| --- | --- |
| `driver/` | Resource adapters and device protocols |
| `driver/peripheral/` | Shared synchronous device contracts |
| `hal/<capability>/` | Public capability API and implementations |
| `hal/<capability>/src/platform/` | OS-specific implementation selection |
| Provider-local `ffi/` directories | Native ABI bindings |
| `platform/src/` or a product-owned composition package | Named devices and typed constructors |
| `platform/<family>/profile.toml` and `platform/nuttx/` | Product and target configuration |
| `service/` | Reusable application behavior |

Portable applications and services use capability contracts. OS/provider selection stays at module and factory boundaries. Cargo selects optional driver and resource code; profile records describe concrete instances and their resources. The catalogue's `resource-features` mapping selects the bus adapters needed by consumed capabilities. An unused capability's profile records do not activate its HAL, drivers, or native helpers. See [resource dependency selection](../../platform/README.md#select-resource-dependencies) and [Profiles and overlays](profiles.md).

## Add a peripheral

First determine which of these cases applies:

| Situation | Work needed |
| --- | --- |
| Supported device on an already registered bus | Add the device and alias to a profile or overlay |
| Supported device on a new bus or pin arrangement | Add the BSP resource mapping and supported native configuration, then the device |
| New chip using an existing bus contract | Implement and test the chip driver, declare its binding, register the optional dependency, then compose an instance |
| Native NuttX device driver with a new control operation | Enable/register the native device and implement the capability's minimal ABI binding |
| New board or controller support | Bring up the NuttX BSP before adding NXRS resource declarations |

For a chip driven by NXRS over a raw bus, NuttX initializes the controller and registers the bus endpoint. The Rust driver owns the chip protocol. If the HAL uses a native NuttX device driver, enable that driver and register the device in board bring-up, then check that its file operations or ioctls match the HAL provider. If the device uses a wired interrupt, qualify the pin, trigger and readiness path on the board as well. See [Prepare the NuttX BSP](profiles.md#prepare-the-nuttx-bsp).

With a supported driver and bus, add a device to the profile or overlay. If the bus is new to the profile, add a resource that selects its registered BSP binding and supplies bus and pin settings. The device names that resource, its driver-compatible string, and settings such as an address. An alias gives the app a stable device name. The [CoreS3 example](examples.md#add-a-second-sensor-on-cores3) shows the files and commands.

For a new driver in an existing composed capability family, implement the peripheral contract and declare the supported combinations in the driver's `bindings.toml`. Register a renamed optional NuttX path dependency in the selected composition crate, either in the SDK or the product workspace. Build support enables its Cargo feature and generates the typed wiring for the selected device. See the [driver registration example](../../platform/README.md).

A new capability also needs its own HAL contract and provider. If it will participate in the device catalogue, add the corresponding composition support; a binding declaration by itself cannot introduce a new subsystem. The [composition reference](../../docs/peripheral-composition.md) identifies the currently implemented families.

## Verify the boundary you changed

A useful sequence is to test the chip protocol with a scripted bus, validate profile resolution, build the target and check its native ABI, then exercise real acquisition on the board. Test failure as well as a successful sample: missing device, invalid configuration, saturated output, stop during a wait, and repeated acquisition.

DMA buffers, callbacks, file descriptors, and device leases must remain valid until the associated work has stopped. Include those lifetimes in tests when changing an implementation. Host tests of an adapter do not establish physical transfer timing or electrical correctness.

Continue with [Profiles and overlays](profiles.md) to configure a device, or [Testing and debugging](testing.md) to choose the validation path. The [HAL architecture](../../docs/hal-platform-architecture.md) and [NuttX device access](../../docs/nuttx-device-access.md) references contain the implementation rules.
