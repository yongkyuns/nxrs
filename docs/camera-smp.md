# Detector SMP qualification

`esp32s3-camera-stream-smp` is an explicit, unsigned experiment. It enables two
NuttX CPUs and pins synchronous PICO numerical execution to core 1 through the
profile's `compute-core` resource. Capture and detection keep their existing
Rust workers. No extra thread, graph, weight copy or activation arena is added;
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
with the caller's current active slice; CPU busy averages both cores. Wall
latency, CPU time, observation cadence and decoded video FPS are separate metrics.

On the Freenove ESP32-S3 N16R8/OV3660, eight calls alternating the two committed
96×96 witnesses preserve all numerical output fingerprints, scores and box
counts. This is a regression/performance probe, not an accuracy corpus.
At 320×240, quality 81, a 60-second live comparison measured:

| Measurement | Single core | SMP, one numerical worker |
| --- | ---: | ---: |
| Mean JPEG-to-detections wall time | 555.4 ms | 488.3 ms |
| Decoded video FPS | 17.78 | 17.12 |
| Completed detections | 79 | 79 |
| Detection failures | 0 | 0 |
| Driver frame discards | 0 | 16 |

Two extra-helper/Conv-partition placements regressed live latency to 587–590 ms;
that implementation was removed. The selected topology improves detector latency
about 12% in this probe, with lower measured video FPS and some driver discards.
These short runs do not establish sustained thermal, power or maximum throughput.

The signed product remains single-core. TLS entropy and Wi-Fi shutdown/OTA are
still unqualified on SMP and their admission gates remain closed. Physical
trials restore and verify the original full flash and observe MCUboot startup;
TLS recovery separately requires the saved gateway address to be reachable.
