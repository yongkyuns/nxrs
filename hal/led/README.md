# Typed LED capability: first device-access implementation

**Status: proposed implementation; no production board is enabled by this change.**
This is the first executable slice of the device-access design in
[PR #10](https://github.com/yongkyuns/EmbeddedRust/pull/10), not a migration of the
existing camera/storage providers or evidence that their std file paths are qualified.

```text
portable caller -> nxrs-led -> selected Rust provider
                                  | File / OpenOptions (ownership + open/close)
                                  | two private controls -> NuttX ioctl -> USERLED
```

The single `nxrs-led` crate owns `Led`, `LedSet`, and shared `DeviceError`.
`NXRS_PLATFORM` selects one private provider; unset builds select `none`.
`mock` owns independent, allocation-free in-memory state. `nuttx` owns a
standard `File`; only
`ULEDIOC_SUPPORTED` and `ULEDIOC_SETLED` need C in `src/platform/nuttx/ffi/`.
There is no shared device object, generic ioctl API, POSIX wrapper, additional
runtime, or third-party dependency. C contains neither policy nor descriptor
ownership. Rust validates supported indices and maps errors.

A portable caller imports only the facade and trait:

```rust,ignore
use led::Led;
let mut leds = led::open()?;
leds.set(0, true)?;
leds.set(0, false)?;
```

The mask denotes supported logical indices, not current output state. Invalid
indices (32 or greater) produce `InvalidData`; absent indices produce
`Unsupported` without calling the driver. The provider caches the supported mask
at acquisition. Mutable access serializes calls through that Rust object, but is
not a claim of system-wide exclusive hardware ownership. A second NuttX opener
may still control the same LEDs. No polling thread or service is needed for these
synchronous controls, and no hard latency bound is claimed.

## Target integration gate

The `nuttx` provider compiles only for a NuttX target. The host
unit-test exception supplies two fake controls; it is not a native provider.

Before adding this provider to a production product platform:

1. Configure/register the board's NuttX USERLED device and select the provider in
   that product platform, not in application/service manifests.
2. Add `hal/led/src/platform/nuttx/ffi/nxrs_userled.c` to that firmware's **target** C sources,
   compiled with the exact configured NuttX headers/toolchain. Link it in the same
   image as the Rust binary; do not compile it as HOSTSRC. The selected platform
   must include the helper in target C sources.
3. The default provider binding is `/dev/userleds`. A target integration may set
   the build-time `NXRS_USERLED_PATH` environment variable when compiling the
   provider. This is a provisional provider-local binding, not a new platform
   schema; no board profile currently forwards or enables it.
4. Qualify File open/drop, native control signatures, positive-errno translation,
   supported masks (including holes/high bit), on/off, failed query cleanup, and
   repeated lifecycle against the pinned target. Confirm the required driver is
   registered; `/dev/led0` and `/dev/userleds` are not interchangeable aliases.

`File` opens an existing device write-only, without create/truncate. Only one Rust
owner exists; a failed capability query drops the same File. The two C helpers
return 0 or captured positive errno and never retain an fd/pointer. Native request
constants and `userled_s` layout stay entirely in C. A compile-time width check
rejects drift in the native mask type instead of truncating silently.

The chosen lifetime contract uses standard File drop: close errors are not
reported, and dropping does **not** promise to switch outputs off. Call `set`
explicitly where output state matters. Do not replace camera/storage's stronger
fallible close or rollback contracts with this simpler LED lifetime.

## Tests and evidence limits

```sh
NXRS_PLATFORM=mock cargo test --locked -p nxrs-led
cargo test -p nxrs-led --lib -- --test-threads=1
python3 tests/host/test-userled-ffi.py
CC=clang python3 tests/host/test-userled-ffi.py
NXRS_PLATFORM=mock cargo check -p nxrs-led --target thumbv6m-none-eabi
python3 tools/check-architecture.py
```

The hosted workflow runs these checks and verifies the emitted mock provider
cfg. Rust provider tests use a real host File with
fake control functions; Linux additionally checks descriptor cleanup. C tests
compile the actual helper against deliberately fake headers to check request,
pointer/payload, success/stale errno, failure output, and no-retry behavior.
Neither is a NuttX target ABI test. No NuttX execution, physical LED qualification,
latency, binary-size delta, or allocation measurement is claimed by these tests.

For footprint qualification, compare the same target/config/toolchain with a
std baseline, LED Rust path, and final helper/driver enabled. Record text/rodata,
data/bss, stack and steady-state allocations separately. The two small C helpers
and absence of new third-party dependencies do not establish a zero-overhead result.

## Sources and related contracts

The native commands/layout were reviewed at the repository's pinned NuttX
[USERLED header](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/include/nuttx/leds/userled.h).
See Rust [File](https://doc.rust-lang.org/std/fs/struct.File.html) and
[AsRawFd](https://doc.rust-lang.org/std/os/fd/trait.AsRawFd.html), the existing
[HAL architecture](../../docs/hal-platform-architecture.md),
[std qualification](../../docs/nuttx-std.md), and
[descriptor ownership](../../docs/nuttx-ownership.md).
