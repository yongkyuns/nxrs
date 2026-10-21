# NuttX device access from Rust

**Status:** normative device-access design for NuttX HAL providers.

This document defines how a Rust HAL in Nxrs talks to a NuttX driver. It is
intentionally narrow: it does not introduce a second OS abstraction layer,
generic POSIX wrapper, device registry, or platform-wide HAL object.

The governing rule is:

> A hardware capability is implemented in its single Rust HAL crate. Use
> qualified Rust `std` APIs directly for ordinary file/socket operations. Keep
> the small amount of NuttX-specific ABI glue required for device controls
> private to that crate's selected provider module.

Portable applications and services see only typed capability APIs. They never
see device paths, file descriptors, `ioctl` request numbers, NuttX C structs,
or NuttX-specific error values.

## 1. Architecture

A typical physical NuttX capability has this shape:

~~~text
portable app/service
        |
        | typed Rust API
        v
hal/<capability>/
  src/api.rs              provider-independent contract
  src/lib.rs              crate root and profile-selected wiring
  src/providers/nuttx/    private NuttX implementation in Rust
  ffi/nuttx/              optional, minimal target-header glue only
        |
        +---- std::fs / std::io / std::net
        |         |
        |         v
        |      NuttX libc/VFS/socket interface
        |
        +---- private typed FFI, only when std cannot express the operation
                  |
                  v
               ioctl / target ABI operation
                  |
                  v
             NuttX VFS driver
                  |
                  v
          lower-half / hardware
~~~

The Rust NuttX provider is the implementation. The optional C file is not
another adapter architecture. It is only an ABI boundary for operations whose
constants, structures, or calling convention are defined by target NuttX
headers.

Example with the qualified USERLED implementation:

~~~text
application/service
  -> led::open() / led::Led       # Rust crate namespace; package nxrs-led
       -> private NuttX module    # hal/led/src/providers/nuttx.rs
            -> OpenOptions("/dev/userleds")
            -> set(index, on)
                 -> private NuttX USERLED ioctl
                      -> hal/led/ffi/nuttx/nxrs_userled.c
~~~

This is an implemented path, not a claim that every HAL or radio has a NuttX
provider. The selected `NXRS_PLATFORM` must bind a capability to an existing
implementation; an unbound capability does not acquire a mock implicitly.

## 2. NuttX device nodes are normal descriptor-backed resources

NuttX drivers commonly register character, block, or other device endpoints in
the VFS, often under `/dev`. Opening such a path yields a normal NuttX file
descriptor whose operations are dispatched through the registered driver's file
operations.

Nxrs does not need to reproduce that VFS in Rust.

When the selected NuttX Rust `std` implementation has been qualified for the
required operation, the provider should use ordinary Rust APIs directly:

- `std::fs::File` / `OpenOptions` for descriptor-backed device nodes;
- `std::io::Read` and `Write` for driver `read`/`write` operations;
- `std::net` for qualified socket operations;
- Rust ownership for the lifetime of the opened resource.

Conceptually:

~~~text
OpenOptions::open("/dev/imu0")
        |
        v
Rust std
        |
        v
NuttX open()
        |
        v
VFS lookup
        |
        v
driver open()

File::read(...)
        |
        v
Rust std -> NuttX read() -> driver read()
~~~

This is the preferred path because it has no Nxrs-specific translation layer.

## 3. Typed Rust APIs, not OS APIs

The public HAL contract describes the hardware capability, not how NuttX
implements it.

Good:

~~~rust,ignore
pub trait Led {
    fn set(&mut self, index: u8, on: bool) -> Result<(), DeviceError>;
}

pub trait Imu {
    fn set_rate(&mut self, hz: u32) -> Result<(), DeviceError>;
    fn read_sample(&mut self) -> Result<Option<ImuSample>, DeviceError>;
}
~~~

Not part of a portable HAL API:

~~~rust,ignore
fn ioctl(&mut self, request: u32, argument: usize) -> ...;
fn raw_fd(&self) -> ...;
fn open_device(path: &str) -> ...;
~~~

An `ioctl` request is an implementation detail of one NuttX provider. A native
provider, browser provider, replay provider, or mock should be free to implement
the same typed Rust operation without pretending to have an `ioctl`.

## 4. How device-specific ioctl operations are implemented

Rust `std` intentionally does not provide a general device-specific `ioctl`
API. That does not justify a general-purpose Nxrs POSIX layer.

When a NuttX driver exposes a capability through `ioctl`, the corresponding
method in the private provider module translates the typed operation internally.

Preferred pattern:

~~~rust,ignore
impl Led for NuttxLed {
    fn set(&mut self, index: u8, on: bool) -> Result<(), DeviceError> {
        // Private implementation detail.
        ffi::userled_set(self.file.as_raw_fd(), index, on)
            .map_err(map_device_error)
    }
}
~~~

If the request constant and argument ABI are defined by NuttX headers, keep the
native layout on the NuttX side:

~~~c
int nxrs_userled_set(int fd, uint8_t index, bool on)
{
  struct userled_s request =
    {
      .ul_led = index,
      .ul_on  = on
    };

  return ioctl(fd, ULEDIOC_SETLED,
               (unsigned long)(uintptr_t)&request);
}
~~~

The helper should be compiled with the exact selected NuttX headers.

This boundary has three purposes only:

1. obtain request values from the real NuttX header configuration;
2. construct or consume native C structures with the target ABI;
3. enter the NuttX operation that Rust `std` does not expose.

It should not implement device policy.

## 5. Keep device behavior in Rust

The dedicated Rust HAL provider owns the meaningful device implementation:

- resource acquisition and Rust lifetime;
- capability configuration and validation;
- protocol parsing and serialization;
- conversion from raw device values to typed Nxrs data;
- bounded buffers owned by the capability;
- state and lifecycle;
- provider-level error mapping;
- readiness integration required by the service execution model.

The native helper must remain mechanical. Do not move the following into C merely
because the NuttX driver is written in C:

- UBX/NMEA parsing;
- IMU sample conversion or calibration;
- retry/state machines;
- packet framing owned by the capability;
- buffering policy;
- service event generation;
- application policy.

If reusable wire-protocol logic is independent of NuttX, it belongs in the Rust
HAL or a repository-owned Rust `driver/` module rather than in the native ABI
shim.

## 6. Boilerplate rule

A NuttX provider should normally consist of:

~~~text
hal/<capability>/nuttx/
  src/lib.rs       # real implementation
  ffi/...          # absent unless a target-specific operation requires it
~~~

Do not add wrappers for operations already available through a qualified Rust
standard-library path.

In particular, do not create capability-local C wrappers merely to rename:

~~~text
open()
read()
write()
socket()
send()
recv()
close()
~~~

when the provider can use the corresponding qualified Rust `std` facility
directly.

Legacy descriptor support and core-only POSIX wrappers now live under
`tests/nuttx/support` and `tests/nuttx/c`. Production HALs must not depend on
those fixtures or recreate a shared POSIX wrapper package. Camera-local
`close.c` exists only to return an observable close error; ordinary file
ownership and all lifecycle decisions remain in Rust.

The goal is for a new NuttX hardware capability to require mostly Rust code and,
in the common case, either no C glue or a few tiny target-header helpers.

## 7. Device paths and platform configuration

A portable service must not know that a GNSS receiver is `/dev/ttyS1`, that an
IMU is `/dev/imu0`, or which bus/address/IRQ backs either device.

Those facts belong below the portable boundary:

~~~text
product platform / NuttX board configuration
        |
        v
selected capability provider
        |
        v
provider-local configuration
        |
        v
NuttX device node
~~~

The exact mechanism can vary by capability. Do not create one giant portable
configuration enum solely to represent provider-specific resource descriptions.

## 8. Relationship to NuttX upper/lower-half drivers

Nxrs should reuse NuttX's driver model rather than mirror it.

NuttX remains responsible for low-level hardware integration such as:

- board and bus setup;
- IRQ/DMA interaction;
- lower-half driver operation;
- kernel-side synchronization and buffering;
- registration of the VFS device;
- implementation of `open/read/write/ioctl/poll` semantics.

The Nxrs Rust HAL begins at the user-facing driver contract exposed by NuttX.

~~~text
Rust capability semantics
        |
        v
NuttX provider in hal/<capability>/nuttx
        |
        v
descriptor operations + minimal private controls
        |
        v
NuttX upper-half / file_operations
        |
        v
NuttX lower-half
        |
        v
hardware
~~~

Nxrs therefore does not need another C "upper half" above the NuttX upper half.

## 9. Readiness and event delivery

This device-access design does not prescribe a second execution framework.

A HAL/provider may need blocking I/O, descriptor readiness, an IRQ-originated
notification, or a dedicated acquisition loop. Whatever mechanism is chosen,
the NuttX-specific wait/readiness details remain inside the capability/provider
boundary and integrate with the service communication model documented
elsewhere.

Do not expose raw descriptor readiness as application policy solely because the
underlying NuttX device uses a file descriptor.

## 10. ABI qualification

Using Rust `std` does not imply that every libc/NuttX interface is automatically
qualified on every Nxrs target.

Each production path must be qualified against the selected Rust/NuttX
configuration. In particular:

- use direct `std` functionality where that exact path has compatible target
  ABI and behavior;
- keep C-native structures behind target-header glue when the Rust binding/layout
  is not established;
- do not manually duplicate NuttX structure layouts or request-number macros in
  portable Rust;
- qualify resource lifetime, error behavior, and any required readiness path.

This is a reason to keep the native boundary small, not a reason to wrap all
ordinary I/O.

## 11. Current repository migration direction

The desired direction is already demonstrated by the NuttX transport provider,
which uses `std::net::UdpSocket` directly and contains no socket wrapper layer.

Some older camera/storage compatibility providers still route ordinary
`open/read/write/close` operations through C bridges. Those paths were useful
for earlier `no_std` and ABI qualification work, but they are not the template
for new physical HALs.

As each corresponding Rust `std` path is qualified, simplify those providers:

~~~text
before
Rust HAL -> C open/read/write/close wrapper -> NuttX

target
Rust HAL -> Rust std -> NuttX

and only where required
Rust HAL typed method -> tiny private ioctl/ABI helper -> NuttX
~~~

Existing special close/error qualification must be preserved where its stronger
lifecycle semantics are part of a capability's requirements; migration must not
silently weaken an already tested failure contract.

## 12. Design invariants

The following are architectural invariants:

- Applications and services consume typed Rust capability APIs.
- A concrete hardware resource is implemented in its dedicated Rust HAL provider.
- NuttX device paths, descriptors and request numbers stay below that boundary.
- Qualified `std` APIs are used directly instead of being rewrapped.
- Device-specific native ABI glue is private and minimal.
- C glue contains no protocol or product logic.
- No generic Nxrs POSIX/device framework is introduced.
- Unselected capability providers and their exclusive glue are absent from the
  production dependency graph.
- Adding a new physical device should primarily add Rust code to its capability,
  not a parallel C adapter stack.

See [HAL capability architecture](hal-platform-architecture.md),
[architecture](architecture.md), [NuttX std qualification](nuttx-std.md), and
[NuttX descriptor ownership](nuttx-ownership.md).
