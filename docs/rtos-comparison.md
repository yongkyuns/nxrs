# Message delivery on NuttX and Zephyr

This study asks whether Rust std channels impose a practical message-delivery penalty on NXRS. A fresh WROOM comparison finds similar stream latency to Zephyr and higher sustained throughput in the tested case. That result applies to the specified images and workload; it is not a general Rust-versus-C or kernel ranking.

<a id="workload-and-fairness"></a>
<a id="which-implementations-are-compared"></a>

## Test setup

| Item | Configuration |
| --- | --- |
| Hardware | One physical ESP32-S3 WROOM; one 240 MHz core; radio and PSRAM disabled |
| Implementations | Patched NuttX `std::sync::mpsc::sync_channel` and `std::thread`; Zephyr native `k_msgq` and threads |
| Shared work | The same Rust workload, payload validation, producer ordering, admission accounting and joined cleanup |
| Versions | NuttX `2b5c0de5e933`; Zephyr 4.5.0 `41d10b597304`; Espressif Rust `abf50ae2e`; GCC 14.2.0 |
| Build and scheduling | Rust optimization level 2, no LTO; FIFO workers; 100 Hz tick; no time slicing |
| Measurement | Nominal 16 MHz SYSTIMER; three rotated passes, fresh boot per case; 24 captures on November 17, 2026 |

Both arms ran four-producer streams with 16- and 64-byte messages and eight slots, a separate sustained 16-byte case, and a one-producer paced case. Streams use 128 warm-up transfers and 2,048 measured offers; sustained throughput uses 131,072 deliveries without per-message timing; paced delivery uses 256 offers at a nominal 100/s. No sensor I/O or useful application processing runs in the receiver.

**Delivery p99** spans message offer through receiver timestamp, including admission, residence and scheduling. It is not the execution time of one queue operation. **Sustained rate** counts valid deliveries over that case's active interval. Distributions are evaluated separately for each run and never pooled. Three repetitions show repeatability, not a confidence interval or a worst-case bound.

### Placement and comparison limits

The review found that the old direct-call audit missed Xtensa literal-loaded calls. NuttX's pending-signal helper `nxsig_addset` remained in flash; the qualification linker now places it in IRAM. This closes a placement gap, but does not establish that pending signals occurred or slowed the measured runs.

The extended ELF inspection found no further flash boundary among the reviewed queue, copy, wakeup, timer and switch paths before cold exclusions. Both arms use the same ROM copy/compare routines. The [placement review](../tests/rtos-characterization/WROOM-REVIEW.md#placement-what-was-corrected-and-what-was-reviewed) identifies dynamic callback targets, excluded channel variants and the remaining limitations. Full automatic placement qualification stays false: frame aliases and arbitrary indirect control flow are not proved. The kernels also use different native tick/dispatch implementations. These are configured-workload results, not isolated scheduler costs.

Executable internal-memory sections total 62,840 B for NuttX and 55,240 B for Zephyr. Equal residency of corresponding hot operations matters; padding both images to equal IRAM bytes would not make execution fairer. These diagnostic-image totals are separate from the RAM study below.

<a id="message-delivery-on-the-two-boards"></a>

## Results

<figure class="architecture-art">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/nxrs-message-comparison-dark.svg">
    <img src="assets/nxrs-message-comparison.svg" alt="WROOM stream delivery p99, maximum of three per-run results. For 16-byte messages: NuttX std 50.375 microseconds and Zephyr 52.000. For 64-byte messages: NuttX std 85.438 and Zephyr 85.313." width="800" height="480" loading="lazy">
  </picture>
  <figcaption>Same board, four producers, eight slots. Bars show the largest per-run p99, not a pooled percentile or worst-case bound. <a href="../tests/rtos-characterization/wroom-review-2026-11-17.json">Numeric evidence</a>; <a href="assets/nxrs-message-comparison.svg">open SVG</a>.</figcaption>
</figure>

| Measurement | NuttX std | Zephyr native |
| --- | ---: | ---: |
| 16-byte stream delivery p99 | 50.375 µs | 51.938–52.000 µs |
| 64-byte stream delivery p99 | 85.375–85.438 µs | 85.313 µs |
| 16-byte sustained deliveries/s | 268,505 | 234,835 |
| One-producer paced delivery p99 | 7.375 µs | 8.188 µs |

Ranges are the minimum and maximum per-run values. All admitted messages arrived correctly and every capture passed replay against its frozen build. Both firmware flashing and restoration of the original image were verified.

<a id="speed-responsiveness-is-not-queue-operation-time"></a>

## Interpretation

**WROOM does not show a general std delivery penalty.** The 16-byte stream p99 favors std by about 1.6 µs; the 64-byte tails differ by at most 0.125 µs. Those small differences do not establish a universal winner. The much larger Pico differences in the older campaign cannot be explained from this board's result.

**Payload and queue residence affect both paths.** Increasing payload from 16 to 64 bytes raises both stream tails by roughly 34–35 µs. This is consistent with additional payload movement and residence under saturation. Subtracting percentiles cannot isolate copy cost, and there is no measured per-function attribution here.

**Throughput and response time answer different questions.** Std sustains 14.3% more deliveries/s in the untimestamped case, while stream p99 remains close. Queue admission, wakeup and scheduling interact with the full workload; the sustained advantage is not a 14.3% improvement in application response time. At the paced rate, both keep up and std's observed p99 is lower by 0.813 µs.

For a service with millisecond handlers, these transport times may be secondary. For a deadline of tens of microseconds, they matter. Budget event-to-output response under actual I/O and handler load; increasing queue capacity can increase residence time rather than fix insufficient processing capacity.

<a id="cpu-efficiency-and-interrupt-response"></a>

## What the older comparisons establish

The [earlier campaign](../tests/rtos-characterization/PHYSICAL-RESULTS.md#messaging-and-progress) covered 16 cases, three transports and three passes per board. NuttX POSIX queues showed higher latency than std, even though both used the same Rust workload and std threads. Descriptor lookup, message management and priority-ordered queue operations are plausible sources of that extra cost; individual operations were not timed. Pooled message reuse means this is not necessarily a heap allocation on every send.

Pico's earlier 16-byte p99 was 81 µs for std versus 65 µs for Zephyr. Its indirect placement paths remain unqualified, and its compiler/architecture policy differs from WROOM. The gap remains an observation about those images, with no proven dominant cause.

Older CPU and alarm-response results use separate diagnostic images. NuttX may charge IRQ work to a participant; Zephyr stops that charge before ISR dispatch. They cannot support a precise cross-kernel CPU or energy ranking. The alarm observer also combines interrupt exclusion and dispatch. This review did not rerun or promote either measurement.

<a id="development-choice"></a>
<a id="focused-follow-up"></a>
<a id="conclusions"></a>

## Conclusions and focused next tests

Retain std for the tested WROOM service design: its delivery performance is competitive, and the older native POSIX alternative did not improve it. Before changing the channel implementation, establish a product-level deficit under real load.

To investigate Pico's remaining gap, first inspect its exact indirect hot paths, then reuse ready-operation and verified-blocked-handoff cases at fixed payload/capacity. A ready-only difference implicates ring/copy/generated code; a blocked-only difference implicates parking/switching. For interrupt response, use matched exclusion/dispatch observers. Neither question requires expanding the workload matrix before resolving its specific measurement gap.

<a id="does-the-application-fit-in-ram"></a>
<a id="ram-count-provisioned-capacity"></a>
<a id="what-is-required-chosen-or-added-by-the-test"></a>
<a id="what-the-ram-ratio-establishes"></a>
<a id="conclusions-a-developer-can-use"></a>
<a id="io-and-cpu-work-need-different-treatment"></a>
<a id="image-size-distinguish-contents-from-address-span"></a>
<a id="what-about-flash-size-and-other-rtoses"></a>
<a id="lean-nuttx-application-check"></a>
<a id="apply-the-findings-to-a-product"></a>

## RAM and other studies

The fresh [native RAM control](../tests/rtos-characterization/WROOM-REVIEW.md#ram-separate-requests-usable-stacks-and-queue-costs) provides at least 4 KiB usable stack per worker and identical payload capacity. NuttX C uses 223,748 B versus Zephyr C's 204,076 B, 9.6% more. POSIX queue machinery is the largest positive difference; equal payload and stack budgets do not explain it. This is a separate native-API fixture, not ordinary Rust std overhead or the footprint of the timing images.

See the [memory report](rust-std-footprint.md) for ordinary std workers/inboxes, the [historical native-service study](history/native-rtos-study.md) for Embassy's different execution contract, and [Thread-Metric](rtos-benchmarking.md) for the PX5 foundation. There is no matched physical NXRS-versus-PX5/openvela result here.

## Evidence and reproduction

The [WROOM review](../tests/rtos-characterization/WROOM-REVIEW.md) records setup, placement review, RAM attribution and reproduction. Its [numeric evidence](../tests/rtos-characterization/wroom-review-2026-11-17.json) binds results to source/build hashes. Use the [MPSC procedure](../tests/mpsc-comparison/README.md) for build, capture and replay. The [compiler assessment](../tests/arithmetic-parity/RESULTS.md) is a separate arithmetic study.
