# Rust std on NuttX

NXRS lets firmware use an ordinary Rust `main()`, threads, channels, files and sockets on NuttX. The build frontend prepares a pinned, patched standard library and native configuration for the selected target. Applications use Rust APIs; target compatibility work stays in SDK/build support and platform implementations.

This integration is broader than compiling Rust syntax for an MCU. Runtime initialization, native ABI layouts, scheduling, thread cleanup and resource lifetime must all agree with NuttX. The references below separate how that integration works from the original bring-up runs.

## Choose your next step

| I need to… | Read |
| --- | --- |
| Build and run my first application | [Building for NuttX](../website/guide/firmware.md) |
| Choose an ordinary Rust API | [Standard-library APIs](../website/guide/standard-library.md) |
| Understand preparation, linking and build receipts | [Build system](build-system.md) |
| Understand device-specific operations | [NuttX device access](nuttx-device-access.md) |
| Assess threads, channels and memory | [Bounded channels](std-channel-performance.md), [memory and image size](rust-std-footprint.md) |
| Assess wall-clock steps, adjustment and monotonic time | [Clock control qualification](../tests/nuttx-std/clock-control/README.md) |
| Qualify repaired software adjustment and concurrent sleeps | [Public time adjustment](../tests/nuttx-std/time-adjustment/README.md) |
| Reproduce the original integration probes | [Historical qualification](history/nuttx-std-integration.md#reproduce) |

<a id="scope-and-linking"></a>

## What the integration owns

| Boundary | Responsibility |
| --- | --- |
| Application | Ordinary Cargo dependencies, Rust entry point, state and lifecycle |
| Standard library | Rust's public thread, synchronization, I/O and runtime APIs |
| Prepared SDK | Pinned Rust/libc compatibility fixes and selected NuttX implementations |
| NuttX | Scheduling, native synchronization, VFS, drivers and board support |
| Build frontend | Resolve the profile, prepare private source copies, compile/link and retain artifact identity |

Rust `std` is an available programming environment, not a platform name. Profiles bind actual operating systems and product resources. OS-specific std internals do not require application branches for Linux, macOS or NuttX.

The frontend applies patches to private prepared sources, leaving installed host toolchains and checked-out dependencies intact. The [patchset reference](upstream-patchsets.md) and [std patch inventory](../upstream/rust-std/README.md) identify the maintained inputs. A successful build using those inputs is not evidence of stock upstream support.

<a id="three-scoped-nuttx-sdk-fixes"></a>
<a id="parker-mutex-initialization"></a>
<a id="historical-startup-pollfd-failure-and-current-abi-fix"></a>
<a id="startup-sig-ign-mismatch"></a>
<a id="startup-sig_ign-mismatch"></a>

## Why target compatibility matters

The original bring-up found differences in synchronization initialization, `pollfd` layout and startup signal constants. Matching a function's name was insufficient: Rust needed the target's exact layout and native meaning. The [original fixes](history/nuttx-std-integration.md#three-scoped-nuttx-sdk-fixes) explain those failures; the maintained patch inventory is the source for today's series.

Thread lifecycle matters too. Flat NuttX built-ins can enter a shared Rust image from different native task groups. Cached Rust thread-local keys and NuttX destructor registration must agree across that boundary. [NuttX memory qualification](nuttx-memory-qualification.md#flat-build-pthread-key-patchset) describes the scoped fix and repeated-launch checks. Session restart inside an application and relaunching its `main()` are different tests.

<a id="abi-gate-before-execution"></a>
<a id="current-extended-checks"></a>

## What validation establishes

| Level | Useful evidence | Boundary |
| --- | --- | --- |
| Native ABI and final link | Target layouts, symbols and prepared inputs agree | Cannot establish device behavior |
| Host behavior | Channel ownership, errors, cancellation and lifecycle logic | Host synchronization is not NuttX scheduling |
| NuttX/QEMU execution | Runtime startup, thread progress, cleanup and selected I/O/fault paths | Emulator timing is not MCU timing |
| Physical WROOM and Pico checks | Recorded messaging, memory, timer and recovery observations | Applies to those images and exercised cases |

Use [QEMU integration](nuttx-qemu.md), [native heap/stack qualification](nuttx-memory-qualification.md) and the [physical campaign](../tests/rtos-characterization/PHYSICAL-RESULTS.md) for their respective results. A physical channel run does not qualify every libc API, peripheral or protection mode.

<a id="reproduce"></a>

## Build and test the path you use

Use the normal [firmware workflow](../website/guide/firmware.md), with a selected app and profile. Keep the resolved configuration, prepared SDK receipt and final image identity with the result. [Testing and debugging](../website/guide/testing.md) selects host, emulator and physical checks; the [fixture index](../tests/README.md) selects measurement workloads.

Run behavior checks after changing runtime or native interfaces. Important failures include partial startup, timeouts, disconnection, thread-local destructor cleanup and joined teardown. The original integration probe includes deliberate-failure cases so a success marker alone cannot pass it.

<a id="remaining-boundaries"></a>

## Product qualification

Test the APIs and resource paths the product actually uses. Correct channel delivery does not establish calibrated deadlines, bounded interrupt response, SMP timing, physical I/O correctness or safe stack margins. Those require the corresponding target configuration and workload.

The [evaluation method](rtos-characterization.md) connects resource, response, overload, recovery and portability requirements without turning every application into a new RTOS benchmark.

<a id="full-esp32-s3-application-integration"></a>
<a id="current-exact-head-result"></a>
<a id="extended-result"></a>
<a id="first-scoped-result-historical"></a>

## Original integration evidence

The [historical M1 report](history/nuttx-std-integration.md) preserves the first ESP32-S3 application integration, ARM/RV32 build and execution boundaries, browser controls, exact revisions and artifact hashes. Its “current” and “pending” statements refer to that stage of development. Later physical evidence is linked above rather than being attributed to those earlier images.
