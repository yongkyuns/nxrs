# Pico 2 W and ESP32-S3 WROOM characterization

The physical runs covered 151 Pico captures and 304 WROOM evidence entries.
Each block replayed against its own frozen artifacts. The IRQ and native RAM
corrections used separate cohorts, so these results summarize the measured
census across cohorts. Both boards' complete original flash verified after
restoration.

| Board and block | Replayed evidence |
| --- | --- |
| Pico primary and owner | 144 messaging captures and seven owner cases |
| WROOM primary, CPU and IRQ | 144 primary captures, 36 CPU captures and 36 IRQ captures |
| WROOM owner and ordinary RAM | Seven owner cases and 78 ordinary Rust RAM captures |
| WROOM native RAM | Three matrices of four fresh image boots and 45 profiles each |

## Messaging and progress

Primary builds ran 16 cases over three fresh-boot passes with rotated transport
order. Each transport delivered every admitted message. Rejections occurred
in the intentional overload cases. The four-producer stream uses an eight-slot
queue. The table gives per-run p99 and sustained throughput from the nominal
hardware counters, with the same displayed values across all three passes.

| Metric | NuttX std | NuttX POSIX | Zephyr native |
| --- | ---: | ---: | ---: |
| Pico 16-byte stream p99 | 81 µs | 130 µs | 65 µs |
| WROOM 16-byte stream p99 | 50.38 µs | 85.50 µs | 51.94 µs |
| WROOM 64-byte stream p99 | 85.44 µs | 128.38 µs | 85.31 µs |
| WROOM 16-byte sustained deliveries/s | 268,502 | 163,329 | 234,835 |
| WROOM 16-byte stream participant CPU/delivery | 6.59 µs | 9.49 µs | 6.84 µs |
| WROOM maximum loaded alarm-to-probe latency | 8.44 µs | 13.50 µs | 4.56 µs |

CPU and IRQ figures use separate diagnostic images. NuttX can charge attributed
interrupt work to participant threads. Zephyr stops charging before ISR
dispatch. The timer observer recorded zero missed or trailing periods under
load. Its timing includes dispatch up to the probe's first counter read.
Placement, external clock calibration, sensor latency, energy and worst-case
bounds remain unqualified. All 14 owner runs passed the finite progress,
shutdown and recovery controls with synthetic Wi-Fi input.

## RAM

For 20 ordinary Rust workers, the observed heap increase over each image's
workload baseline was 90,448 B for threads alone, 124,064 B for services with
std inboxes and 152,232 B for services with Crossbeam selection. These cases
retained 112 B above baseline after workers joined. Queue-only controls
returned to baseline. Linked resident RAM was 57,688 B for the baseline,
57,704 B for threads, 57,720 B for std inboxes and 62,040 B for Crossbeam.
Rust allocation observations are included in the native heap accounting.

| Native RAM image | Accounted RAM | `work-long` deadline misses by pass |
| --- | ---: | ---: |
| NuttX C | 218,940 B | 0, 1, 0 |
| NuttX Rust | 218,940 B | 1, 1, 7 |
| Zephyr C | 204,060 B | 0, 6, 6 |
| Embassy Rust | 61,184 B | 1,286, 1,286, 1,285 |

All four images delivered 3,900 of 3,900 `work-long` offers per pass. NuttX
accounting combines resident sections with observed heap high water. Zephyr
and Embassy count resident sections, including reserved stacks. The stack
observations qualify the exercised workload and settings.

## Supplemental board checks

These November 10, 2026 checks used separate firmware and are outside the
campaign census above.

- **Pico 2 W timer to Rust worker.** A flat, single-core NuttX fixture used the
  RP2350 hardware timer to wake a blocked Rust thread. All 600 alarms reached
  the worker. In each of the three loaded batches of 100, the ISR interrupted
  the lower-priority task on every alarm. Each loaded batch had a 6 µs median
  from alarm deadline to the worker's first counter read. The three observed
  maxima were 32, 26 and 22 µs. Timing uses the nominal on-chip counter.
  The fixture image SHA-256 is
  `953b4039f94f57b5ec1de9b32f77d0d736c49c10f816f780aaca7eb1d79e12eb`.
  Physical IMU IRQ-to-sample timing still needs the
  [wired BMI270 fixture](../nuttx-std/imu-qualification/README.md) and the
  external timing reference in the
  [S8 plan](../../docs/peripheral-composition-plan.md#s8--qualify-protected-execution-and-physical-deadlines).
- **WROOM camera read recovery.** The signed camera diagnostic recovered in
  the same boot and Wi-Fi session after an injected read error on the mutual
  TLS path. The reconnecting RTSP viewer observed a 7.83 s gap between decoded
  frames, and the gateway detection viewer received fresh PICO metadata 4.87 s
  after the fault. See the [camera trial](https://github.com/yongkyuns/nxrs/pull/323)
  and the separate [watchdog and update evidence](../../docs/firmware-updates.md).

## Campaign build provenance

The starting fixture revision was `797c00ca`. The physical reruns used the
Pico flash-resident reset helpers and owned Zephyr USB transfers, the NuttX
loader or shell boot framing, the WROOM IRQ dispatch registration fix, and
complete RAM shell exit framing. The WROOM ordinary RAM build receipt names
source `ab7fdc45`. Messaging uses Espressif Rust `abf50ae2e` on WROOM and Rust
`52618eb33` on Pico, with Zephyr pinned to `41d10b597304`. The matched NuttX
transport pairs passed the kernel-input controls in each cohort.

The NuttX Rust native RAM image used the six-patch Xtensa LLVM evaluation
backend with GNU assembly. Embassy used the seventh alignment proposal with
native object emission. In the separate paired NuttX `work-long` control, the
stock standalone backend rejected 20 of 3,900 offers on both boots. The patched
backend delivered all 3,900 on both boots from the same optimized Rust bitcode.
The integrated `rustc` and its final firmware loop placement need their own
qualification. See the [compiler evaluation guide](../../upstream/rust-llvm/BUILDING.md).
