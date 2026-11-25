# PICO detection and video performance on ESP32-S3

This study measured the cost of JPEG-to-person detection and its effect on concurrent camera streaming. It evaluated image preparation and memory configuration while keeping the PICO model and detection policy unchanged. The results describe the October 22, 2026 single-core firmware; later [SMP measurements](camera-smp.md) use a different execution configuration.

## Test setup

| Item | Tested configuration |
| --- | --- |
| Device | Freenove ESP32-S3 N16R8 with OV3660 camera; flat NuttX; one 240 MHz core |
| Model | PICO_S8_V1, 224×224 RGB int8; ESP-DL v3.3.0 at `998b76af271101b1a09b005958ed9e97ec8925a0`; unchanged weights |
| Controlled input | Ten frozen 320×240, quality-81 JPEGs from the COCO/VWW holdout: five evenly spaced samples per class |
| Original configuration | 40 MHz PSRAM; 32 KiB data cache, 32-byte lines; model parameters read from flash |
| Selected configuration | 80 MHz PSRAM; 64 KiB data cache, 64-byte lines; RAM-resident parameters; shared Rust resize optimization; 40 MHz flash retained |
| Streaming configuration | Selected memory settings; 750 ms requested detection interval; 128 KiB requested detector-worker stack |
| Firmware identity | Production image SHA-256 starts `5f9a59be4099`; full hash, configuration and source hashes in the [build record](../tests/detection/results/2026-10-22-pico/production-provenance.json) |

### Procedure and measurement boundaries

| Test | Procedure | Measurement |
| --- | --- | --- |
| Isolated stage comparison | Same ten JPEGs for each configuration; camera and Wi-Fi stopped | Mean wall time per stage; native millisecond resolution; file I/O and USB transfer excluded |
| Output equivalence | Same 800 COCO/VWW and 200 Open Images JPEGs before/after optimization | Native scores, box counts and truncation flags; separate host score and box-coordinate comparison |
| Streaming endurance | Detection disabled for 60 s, then enabled for 600 s in the non-profiling image | Receiver-decoded FPS, completed detections, failures, discards and sampled memory |
| Recovery | 120 s with a ten-second stalled reader, client reconnection every 30 s and Wi-Fi reconnection at 40 s | Receiver progress, gaps and detector progress during deliberate interruptions |
| Concurrent CPU observation | Separate profiling images at 500 and 750 ms intervals; a worker-CPU probe at 750 ms | CPU busy, detector CPU time, call wall time and completed detections |

The stage intervals cover JPEG validation/decoding, Q8 bilinear resize plus quantization, numerical execution including its compute lease and copies, and confidence/box decoding plus bounded NMS. Wall time includes preemption. The production streaming image excludes the serial batch and stage-profiling endpoints.

### Assumptions and limits

The ten-image probe measures execution cost, not detection accuracy. It is one controlled sample set, not a confidence interval. Streaming uses a live scene and different durations, so the disabled/enabled figures are observations rather than a precisely paired estimate of overhead.

**Video FPS means frames decoded by the test receiver.** It does not measure iPhone rendering, and an average FPS cannot exclude brief stalls. Detection cadence counts completed calls; it is neither the requested sampling interval's reciprocal nor the isolated model's maximum throughput. These runs did not qualify SSH, later two-core firmware, night/IR accuracy or multi-day operation.

<a id="reference-and-timing-boundaries"></a>
<a id="controlled-stage-measurements"></a>

## Isolated detection results

| Mean stage time | Original | Selected |
| --- | ---: | ---: |
| JPEG decode | 118.7 ms | 83.7 ms |
| Resize and quantization | 140.7 ms | 47.4 ms |
| Numerical execution | 355.4 ms | 131.6 ms |
| Postprocessing | 3.7 ms | 3.7 ms |
| **Complete call** | **618.6 ms** | **266.5 ms** |

The selected configuration reduced complete-call time by about 57%, or a 2.3× speedup. The resize change retained the same Q8 rounding while reusing coordinates and row bounds. RAM-resident parameters, faster PSRAM and the larger cache reduced the numerical and image-processing intervals. The [original control matrix](history/camera-pico-study.md#controlled-stage-measurements) separates the incremental changes; the two endpoints above represent their combined effect.

Across the full 1,000-image rerun, native scores, box counts and truncation flags matched the earlier results exactly. Native box coordinates were not recorded. The separate host rerun produced identical scores and byte-identical box tables. This supports output preservation within those observations, rather than a new accuracy claim.

<a id="ownership-portability-and-resource-costs"></a>

### Resource cost

The selected implementation holds a 343,120-byte aligned parameter block in addition to the existing 551,936-byte activation arena. Increasing cache capacity consumes another 32 KiB of internal SRAM. The Rust provider owns these lifetimes and the scoped native compute state; the shared detector API and alert policy do not inspect the platform or memory configuration.

<a id="final-production-firmware-on-the-physical-camera"></a>

## Concurrent video and detection results

| Production run | Receiver-decoded video | Completed detections | Inference failures | Camera discards |
| --- | ---: | ---: | ---: | ---: |
| Detection disabled, 60 s | 17.79 FPS | — | — | 0 |
| Detection enabled, 600 s | 17.77 FPS | 797 | 0 | 0 |
| Deliberate interruptions, 120 s | 16.72 FPS | 159 | 0 | 0 |

The 600-second run achieved **1.328 detections/s**. Mean call wall time was 510.432 ms and the maximum was 713 ms. This exceeds the isolated call's 266.5 ms mean because streaming and detection share the core. The recovery run's largest receiver gap was 5.203 seconds; its lower aggregate FPS includes the deliberate disruptions.

Sampled user heap was 2,493,664–2,530,136 bytes and kernel heap 58,544–63,920 bytes. Observed detector stack use was 53,040 of 130,840 usable bytes. Both enabled runs returned to 22,368 user-heap bytes and 53,208 kernel-heap bytes in repeated post-shutdown samples. These are observed lifecycle and high-water results, not general leak-freedom or worst-case stack guarantees.

<a id="complete-corpus-and-concurrent-cpu-measurements"></a>

### CPU headroom

| Separate profiling run | Receiver-decoded video | Detections in 60 s | Median CPU busy |
| --- | ---: | ---: | ---: |
| Requested 500 ms interval | 17.78 FPS | 99 | 99.7% |
| Requested 750 ms interval | 17.78 FPS | 79 | 88.15% |

The 500 ms setting left almost no observed CPU headroom on this single-core configuration. A further 750 ms probe measured 283.563 ms mean detector-thread CPU time versus 588.853 ms wall time across 75 calls after startup. NuttX's thread accounting can include interrupt work attributed to that worker. These figures explain why call wall time under streaming load is not the model's CPU cost.

## Interpretation

**The original slowdown had both memory and preprocessing causes.** With the model unchanged, copying parameters to RAM reduced numerical time from 355.4 to 277.2 ms. Later in the controlled sequence, raising PSRAM speed reduced it from 277.2 to 177.9 ms, and the larger cache reduced it to 131.6 ms. These interventions support memory-access sensitivity as a major cause; they do not measure cache-miss counts or pure bandwidth saturation. Separately, reusing resize coordinates and row bounds reduced resize/quantization from 142.0 to 52.5 ms in its adjacent control. Compiler optimization of the decoder alone barely changed total time. [Controlled sequence](history/camera-pico-study.md#controlled-stage-measurements).

**The optimized pipeline has two comparable costs.** Numerical execution is about 49% of the selected 266.5 ms call; JPEG decoding is 31%, resizing 18% and postprocessing 1%. Even halving numerical time would reduce the total only to about 200.7 ms, a calculated 1.33× speedup if everything else stayed fixed. Eliminating postprocessing would save only 3.7 ms. Reducing decode/resize work is therefore as relevant as tuning the model, provided it preserves input mapping and detection quality.

**Live cadence is constrained by shared execution capacity.** In the worker probe, roughly 284 ms of charged CPU work occupied 589 ms of wall time. That difference is consistent with the worker waiting while other work runs; it is not all extra inference computation. The 500 ms profile's 99.7% busy observation leaves little headroom for bursts or maintenance. The later [SMP study](camera-smp.md#500-ms-sampling) demonstrates a separate, already-tested route to higher cadence; its differing inputs and timing scope prevent treating it as another row in this isolated comparison.

<a id="conclusions"></a>

## Conclusions and next qualification

The selected implementation made detection cheaper without changing the measured outputs. For users, the useful outcome is **fresh detections alongside timely video**, rather than maximum standalone inference rate. Average decoded FPS alone is insufficient: the tests did not measure phone render gaps, and recovery intentionally included a multi-second interruption.

An existing [short SSH coexistence test](dropbear-ssh.md#two-core-wroom-maintenance-image) covers shell activity and gateway-decoded video. The highest-value extension is a paired run on the current maintenance image with the same scene or replay: detection disabled/enabled, actual phone rendering, and an active SSH session. Record frame-gap p95/p99/max, detection age/completion cadence, CPU headroom and recovery across renewal/reconnect. This tests the user-visible failure mode beyond that short gateway observation.

If profiling still shows preparation near half the call cost, evaluate one change that reduces redundant decoding, resizing or copying. Compare the same input/output corpus and stage CPU times, then accept it only if live frame gaps and detection freshness improve without reducing recall or breaking SSH and recovery. The observed split gives no reason to prioritize small postprocessing savings.

<a id="reproduction"></a>

## Evidence and reproduction

- [Numeric evidence](../tests/detection/results/2026-10-22-pico/) and [production provenance](../tests/detection/results/2026-10-22-pico/production-provenance.json): stage tables, equivalence, CPU observations, endurance and recovery records.
- [Original study and commands](history/camera-pico-study.md): full configuration matrix, pinned vendor comparison, ownership tests and rejected controls. The reproduction commands describe that study's revision.
- [Detector accuracy](camera-pico-qualification.md), [alternative-model screening](camera-detector-alternatives.md) and [detection delivery](camera-detection-delivery.md) cover separate questions.
