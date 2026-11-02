# Detector SMP qualification

`esp32s3-camera-stream-smp` is an explicit, unsigned streaming experiment;
the signed product default remains single-core. It selects two CPUs, caller
core 1 and helper core 0, with a 32 KiB helper stack at priority 90. The detector
API, model, weights, activation arena and 750 ms observation budget are unchanged.

Rust owns one persistent helper and a bounded synchronous handoff. ESP-DL supplies
disjoint Conv output-row partitions; the caller performs graph/bias updates and
waits for both numerical halves before argument/tensor storage can retire. Errors
are sticky and partial results are rejected. Rust restores caller affinity and
SIMD state; the helper waits with its newly enabled SIMD context released.

Build with the existing stream fixture and `--platform esp32s3-camera-stream-smp
--probe`. Compare the same frozen images with `esp32s3-camera-stream --probe`:
all output fingerprints, scores and boxes must match. Report kernel/pipeline
wall time separately from decoded video FPS, heap and stack use. Fixture CPU
utilization averages both cores; detector thread CPU time excludes the helper.
SMP uses the shared 1 ms scheduler clock for CPU accounting. Independent Xtensa
CCOUNT registers and global overflow correction are explicitly disabled; these
coarse CPU measurements are distinct from pipeline wall-clock timing.

Physical SMP results are pending. TLS entropy and Wi-Fi shutdown/OTA maintenance
remain explicitly unqualified on SMP; their admission gates are preserved.
