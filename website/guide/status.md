# Status and limitations

NXRS is an experimental development environment, not a finished SDK or an independently qualified RTOS. NuttX supplies the operating system. This repository explores the Rust integration, component boundaries, configuration and application patterns built above it.

## What is present

The repository contains ordinary Rust application entry points, reusable services, hardware capability interfaces, platform profiles, maintained Rust/NuttX integration, and tests at several levels. The [navigation example](examples.md#navigation-and-service-composition) is a small way to exercise the owner/event model. The [camera workflow](examples.md#experimental-iot-camera) exercises a larger application with capture, detection, transport and client-facing state.

Existence of an implementation, a passing host test, a linked firmware image and a successful physical-device run are different claims. The reference documents state which evidence is available for their configurations.

## What is not implied

- **General board or peripheral support.** A NuttX-supported board is not automatically an NXRS-qualified product. Rust toolchain, ABI, selected providers, drivers and resource budgets still need validation.
- **Stable packaging or universal standard-library support.** The integration currently includes maintained patches. Available OS facilities depend on the selected target and configuration. Library portability must be checked, not inferred from use of `std` alone.
- **Negligible cost or a production guarantee.** Thread stacks, allocations, queues, code size, recovery, timing and security need application-specific measurements and review.

NXRS is independent of the NuttX and Rust projects. It does not claim their endorsement or replace their release and support policies.

## Reading the evidence

Use [Testing and debugging](testing.md) to distinguish pure/host tests, platform integration, emulation and physical measurements. Use [Performance and memory](performance.md) to understand what a number includes and excludes.

The [Rust std integration reference](../../docs/nuttx-std.md), [event-path measurements](../../docs/std-channel-performance.md), [footprint analysis](../../docs/rust-std-footprint.md) and [RTOS comparisons](../../docs/rtos-comparison.md) describe specific configurations. A microbenchmark using a native RTOS adapter does not measure the complete `std`-based application path.

For the camera example, consult both [delivery and ownership](../../docs/camera-detection-delivery.md) and the model-specific [qualification report](../../docs/camera-detection-qualification.md). Historical detector results do not establish dependable intrusion detection, field reliability or production security. [Security design and release requirements](../../docs/security.md) records separate obligations.

## Work that remains

The ongoing questions are whether the components stay reusable as products differ, whether their costs remain acceptable on constrained targets, and whether the toolchain and platform integration remain maintainable. Additional examples, on-board measurements and regression tests are part of answering those questions.

The [repository issues](https://github.com/yongkyuns/nxrs/issues) contain the active work. This page deliberately does not turn a historical test report into a release checklist marked complete.
