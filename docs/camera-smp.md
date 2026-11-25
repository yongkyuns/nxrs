# Detector SMP qualification

Signed WROOM builds and the unsigned `esp32s3-camera-stream` profile share two
NuttX CPUs and a 500 ms observation interval. The profile's `compute-core`
resource pins synchronous PICO numerical execution to core 1. Capture and
detection use their existing Rust workers.

The NuttX factory validates placement before enabling SIMD. Rust serializes
numerical execution and restores SIMD state before restoring affinity, including
unwind. Placement/restoration failures retire the kernel. The SDK remains
synchronous and single-threaded. App and service code has no CPU/OS knowledge.

Qualification found a pinned NuttX scheduler bug: changing affinity with an
empty ready queue updated the mask without migrating the running task. Patch
0105 corrects that condition; the host test executes the actual pinned function
and reproduces the original failure. Rust still verifies the actual CPU.

Build the existing stream fixture with `--profile esp32s3-camera-stream
--probe`; use `--profile esp32s3-camera-stream-single-core --probe` for the
single-core control.
SMP accounting uses the common 1 ms scheduler clock. Independent CCOUNT and
overflow correction are disabled. Fixture CPU observations use `perf_convert`
with the caller's current active slice, including attributed interrupts; CPU
busy averages both cores. Wall
latency, CPU time, observation cadence and decoded video FPS are separate metrics.

On the Freenove ESP32-S3 N16R8/OV3660, eight calls alternating the two committed
96×96 witnesses preserve all numerical output fingerprints, scores and box
counts. This is a regression/performance probe, not an accuracy corpus.
The earlier 60-second comparison of source `48f187d0` used a 750 ms interval
at 320×240, quality 81. Use `--sample-ms 750` in the fixture to reproduce
that interval.

| Measurement | Single core | SMP, one numerical worker |
| --- | ---: | ---: |
| Mean JPEG-to-detections wall time, startup included | 589.0 ms | 460.4 ms |
| Mean wall time, last 40 recorded calls | 584.2 ms | 493.0 ms |
| Mean caller CPU time, startup included | 287.2 ms | 396.1 ms |
| Decoded video FPS | 17.79 | 17.79 |
| Completed detections | 79 | 79 |
| Detection failures | 0 | 0 |
| Driver frame discards | 0 | 0 |

The probe image grows from 1,704,372 to 1,705,020 bytes. SMP adds 2,304 bytes of
linked IRAM, 8,528 bytes of linked DRAM and 3,332 bytes of median kernel heap.
User heap stays about 2.26 MiB; both runs use 53,632 bytes of the 128 KiB detector
stack. These are probe/profile costs, not signed production image measurements.

Two extra-helper/Conv-partition placements regressed live latency to 587–590 ms;
that implementation was removed. Whole-run means include unequal viewer startup
periods: SMP initially captured without delivering to a viewer for longer. The
last 40 recorded calls occur after both viewers are active; their lower SMP
mean is descriptive evidence from one short run with unmatched live inputs,
not a controlled speedup. Earlier controls showed video variability and some
discards.

## 500 ms sampling

Two 60-second runs per setting on the same SMP image averaged 1.91 detections/s
at 500 ms and 1.30 at 750 ms, with about 17.8 decoded FPS in both cases.
A ten-minute run at 500 ms completed 1,176 detections (1.96/s) with 514 ms p95
inference latency, zero inference failures and zero camera-driver drops.
After a forced Wi-Fi reconnect at five minutes, decoded video returned to
about 17.8 FPS and median heap use stayed level. The longest gap between decoded
frames was 5.21 seconds. The 500 ms runs used the fixture's interval override.
Thermal and power behavior remain unqualified.

## Signed firmware

On ESP32-S3 with SMP and PSRAM enabled, NuttX atomic APIs, compiler atomic
libcalls and the Rust bounded-channel gate use an internal-RAM spinlock. The
kernel includes the upstream heap-lock fix for interrupted mutex waits. RF
entropy requires active Wi-Fi. Signed updates wait for station shutdown before
writing flash. Each flash operation waits for both CPUs to restore cache and
interrupts.
