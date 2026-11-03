# Detector SMP qualification

`esp32s3-camera-stream-smp` is an explicit, unsigned experiment. It enables two
NuttX CPUs and pins synchronous PICO numerical execution to core 1 through the
profile's `compute-core` resource. Capture and detection keep their existing
Rust workers. No extra application thread, graph, weight copy or arena is added;
the detector API, model, thresholds and 750 ms observation budget are unchanged.

The NuttX factory validates placement before enabling SIMD. Rust serializes
numerical execution and restores SIMD state before restoring affinity, including
unwind. Placement/restoration failures retire the kernel. The SDK remains
synchronous and single-threaded. App and service code has no CPU/OS knowledge.

Qualification found a pinned NuttX scheduler bug: changing affinity with an
empty ready queue updated the mask without migrating the running task. Patch
0105 corrects that condition; the host test executes the actual pinned function
and reproduces the original failure. Rust still verifies the actual CPU.

Build the existing stream fixture with `--platform esp32s3-camera-stream-smp
--probe`; use `esp32s3-camera-stream --probe` for the single-core control.
SMP accounting uses the common 1 ms scheduler clock. Independent CCOUNT and
overflow correction are disabled. Fixture CPU observations use `perf_convert`
with the caller's current active slice, including attributed interrupts; CPU
busy averages both cores. Wall
latency, CPU time, observation cadence and decoded video FPS are separate metrics.

On the Freenove ESP32-S3 N16R8/OV3660, eight calls alternating the two committed
96×96 witnesses preserve all numerical output fingerprints, scores and box
counts. This is a regression/performance probe, not an accuracy corpus.
At 320×240, quality 81, the final 60-second comparison of source `48f187d0` measured:

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
discards. Sustained video, thermal, power and maximum throughput remain unqualified.

The signed product remains single-core. TLS entropy and Wi-Fi shutdown/OTA are
still unqualified on SMP and their admission gates remain closed. Physical
trials restore and verify the original full flash and observe MCUboot startup;
TLS recovery separately requires the saved gateway address to be reachable.
