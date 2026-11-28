# Test and fixture index

Use the [performance guide](../website/guide/performance.md) to choose the question and [RTOS comparison results](../docs/rtos-comparison.md) to read the findings. This index selects the fixture to run. A build or host check does not establish physical-board timing.

## Current measurement and qualification paths

| I want to… | Fixture | Scope |
| --- | --- | --- |
| Compare std, NuttX POSIX and Zephyr queues | [MPSC comparison](mpsc-comparison/README.md) | Same Rust workload; separate images on each board |
| Evaluate messaging, resources, progress and recovery together | [RTOS characterization](rtos-characterization/README.md) | Fixed campaign and [physical results](rtos-characterization/PHYSICAL-RESULTS.md) |
| Measure std workers, inboxes and multi-queue selection | [Ordinary Rust RAM](ram-footprint-std/README.md) | Owner/queue scaling and lifecycle, with host and target observers |
| Find hidden allocation or retained memory | [Allocation probes](../docs/memory-qualification.md) | Rust allocator boundary; separate [NuttX heap/stack checks](../docs/nuttx-memory-qualification.md) |
| Measure wall-clock steps, adjustment and monotonic time | [Clock control qualification](nuttx-std/clock-control/README.md) | MPS2-AN521 QEMU with an independent timer; separate API and behavior gates |
| Qualify priority progress, overload and joined shutdown | [Owner qualification](owner-qualification/README.md) | Finite owner controls; read its input and target boundaries |
| Check native C/Rust service lifecycle | [Service qualification](service-qualification/README.md) | Lean LED fixture, delivery, restart and faults |

## Diagnostic and historical comparisons

Use these to reproduce a specific experiment, keeping its configuration separate from current measurements.

| Suite | Question |
| --- | --- |
| [Event services](event-services-comparison/README.md) | Native NuttX C/Rust, Zephyr C and Embassy execution/storage comparison |
| [Service footprint](service-footprint/README.md) | Before/after startup, output, worker and queue costs |
| [Arithmetic parity](arithmetic-parity/README.md) | Compiler correctness and arithmetic performance controls |
| [Thread-Metric foundation](../docs/rtos-benchmarking.md) | Native primitives and limits of comparison with the external report |
| [Historical Zephyr](zephyr-comparison/README.md) / [Embassy](embassy-comparison/README.md) | Older producer/worker/reply workload, separate from event services |

## Run a fixture's host checks

Run a fixture's Python tests by replacing the suite path in this example:

```sh
python3 -m unittest discover -s tests/event-services-comparison -p 'test_*.py'
```

Optional Linux host checks may require GCC, Rust and mounted `/dev/mqueue`; see
the fixture guide. Shared `rtos_harness` ownership: `images.py` handles ELF and
ESP-IDF aliases; `zephyr.py` handles Zephyr pins and target guards; `device.py`
handles serial, restore and markers; `matrix.py` handles case order and session
restore. These are workload-neutral utilities, not another measured suite.
