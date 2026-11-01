# PICO camera detection qualification

The [performance follow-up](camera-pico-performance.md) records stage profiling,
unchanged-model equivalence and the optimized memory/resize implementation.
The image hashes, two-second budget and measurements below remain historical
qualification of their original firmware; they are not relabeled as new results.

The detector API and implementations live in `hal/detector`. The camera and
Rust detection service consume `detector::open`, `Detector`, borrowed `Image`,
reported capabilities and bounded normalized boxes. SDK types and platform
selection stay below that API. The service retains its one-owned-frame mailbox,
nonblocking admission, expiry, temporal confirmation and joined shutdown.
Camera snapshots preserve the boxes; the viewer does not yet draw overlays or
send remote notifications.

## Model and runtime

This is Espressif's MIT-licensed **PICO_S8_V1 pedestrian detector** from ESP-DL
v3.3.0, revision `998b76af271101b1a09b005958ed9e97ec8925a0`, with unchanged
224×224 RGB int8 weights. It is distinct from ESPDet-Pico.
[Model SHA256 and MIT notice](../hal/detector/models/PICO-NOTICE),
[SDK source/port ledger](../upstream/esp-dl/source.json) and
[build/ownership guide](../hal/detector/README.md) are recorded separately.

Rust validates/decompresses JPEGs, performs full-image Q8 bilinear resize and
round-half-up int8 input quantization, decodes the SDK's square-root confidence
and 8-bin DFL box distances, and applies IoU-0.5 NMS. It retains the best 128
candidates and at most ten boxes, with explicit truncation. No image references
are retained by a HAL call. A reusable heap tensor avoids large nested stack
arrays. The SDK supplies only synchronous numerical operations; it launches no
tasks and has no model loader, flash access, camera pipeline or alert logic.

The host uses scalar operators with the SDK's packed weights unpacked for its
layout; ESP32-S3 uses the original packed SIMD kernels. NuttX scopes the compute context to native numerical execution, serializes
owners and restores its prior state through target-header glue beside that HAL
implementation. The selected single-core flat profile is checked at build time.
Host-injected tests cover initially disabled/enabled state, unwind retirement and
sticky rejection after poisoning; the device fixture checks before/after state
for every detector call, including its real-image startup witnesses.
Unbound platforms acquire `Unsupported` and do not build private SDK glue.
The contract builds without models on bare-metal ARM and WebAssembly.

Native qualification exposed an assembler flag fault: text-section literals
could occupy a kernel's exported function address before `ENTRY`. Assembly now
uses separate literal sections. All 284 SDK assembly entry points pass an ELF
byte check; compiling the original bad flag is rejected by the same check.
AddressSanitizer passed 41 native SDK calls with two independent owners,
reverse teardown and continued use of the surviving owner. Linux LeakSanitizer
exposed a teardown leak: SDK `ModelContext` deletes tensor views only when it
owns a root allocation, whereas this graph owns a separate arena. The generated
SDK owner now explicitly retires those views after the operators. The witness
also requires exactly balanced live allocations, covering macOS ASan without
LeakSanitizer. Removing view retirement makes that witness fail on macOS.
Host lifetime tests
also exercise the public HAL with real JPEGs, invalid-input recovery and repeat
inference. These checks complement physical qualification.

## Paired person-presence accuracy

The existing 800-image COCO/VWW holdout and 200-image Open Images sample were
reused byte-for-byte, without threshold tuning. Both are balanced, public,
camera-equivalent 320×240 quality-81 JPEG samples. Their labeling tasks differ
and must not be pooled. See the original [sample identities and limitations](camera-detection-qualification.md).
The primary threshold remains 500/1000. The 700-threshold rows in JSON are
secondary descriptive results, not a retuned deployed threshold.

| Sample/model | Accuracy | Precision | Recall | TP / TN / FP / FN |
| --- | ---: | ---: | ---: | --- |
| COCO/VWW, original VWW | 81.125% | 89.27% | 70.75% | 283 / 366 / 34 / 117 |
| COCO/VWW, PICO | 86.00% | 83.49% | 89.75% | 359 / 329 / 71 / 41 |
| Open Images, original VWW | 72.00% | 89.29% | 50.00% | 50 / 94 / 6 / 50 |
| Open Images, PICO | 76.50% | 82.72% | 67.00% | 67 / 86 / 14 / 33 |

PICO detects more person-positive images at this threshold and produces more
false positives. Its COCO/VWW accuracy Wilson 95% interval is 83.42–88.23%;
recall is 86.39–92.35%. Open Images intervals are 70.16–81.84% and 57.31–75.44%.
These precision values do not establish deployment precision or false alerts
per hour. Training membership cannot be independently established. This is
not night/IR, field security reliability or temporal incident qualification.

Scalar host full-HAL-call time averaged 10.262 ms (p95 13.050 ms, maximum
54.833 ms) on COCO/VWW and 9.530 ms (p95 10.627 ms, maximum 11.895 ms) on Open
Images. These macOS/aarch64 measurements exclude image-file/result-file I/O and
scheduling, but are not an isolated host-load benchmark. PICO is slower than
the VWW scalar host baseline; the optimized S3 kernel is measured separately.
The HAL keeps preprocessing private, so no separate preprocessing latency is
claimed for PICO.

## Native corpus qualification

The same 1,000 frozen JPEGs ran through the actual HAL on one physical Freenove
ESP32-S3 N16R8 with OV3660, one 240 MHz core and 8 MiB PSRAM. Every person score
matched the scalar host exactly: mean/max score difference zero and zero
classification disagreements at 500. Box counts and truncation flags also
matched. The native fixture does not retain coordinates, so this does not claim
bit-equivalence of every box coordinate.

Full HAL-call time, excluding USB upload and without camera/Wi-Fi running,
averaged 620.195 ms (p95 629.000 ms, maximum 641.000 ms) on COCO/VWW
and 618.415 ms (p95 627.000 ms, maximum 632.000 ms) on Open Images.
NuttX timing has millisecond granularity. Both batches completed all images,
dropped their model and joined. These isolated timings do not establish video
FPS, scheduling latency or temporal alert response.

## Fixed-threshold box localization

The actual HAL boxes were matched one-to-one to COCO person annotations at
score ≥500 and IoU ≥0.5. All non-crowd person sizes count; the 36 images containing
person crowds are excluded entirely. This leaves 764 images with 1,282 ground
truth people: **534 TP, 108 FP, 748 FN**, **83.18% precision, 41.65% recall,
55.51% F1**. No image hit the candidate/output truncation flag in this sample.
Small and occluded people remain a material limitation. This fixed-threshold,
max-ten-box convenience-sample result is not COCO mAP.

Public numeric evidence is under
[2026-10-21-pico](../tests/detection/results/2026-10-21-pico/), referencing the
original per-image manifests and attribution. No dataset or private camera
photos are redistributed.

## Selected streaming profile

The same measured image ran the video-only control, short model check, ten-minute
run and recovery test: scoped native compute context, lower-priority worker 90
(camera owner 100), a two-second minimum analysis interval, 128 KiB requested
worker stack and one 153,600-byte owned JPEG. All 117 selected native ABI
witnesses passed. These sequential FPS runs show preserved throughput; the small
on/off difference is not a claimed speedup from enabling detection. The fixture checks native enabled-state restoration before
and after each detection, including real-image startup scores 940 and 415.

| 320×240, quality 81 | Duration | Fully decoded video FPS | Result |
| --- | ---: | ---: | --- |
| Detection off, same image | 60 s | 17.66 | PASS |
| Detection on, selected profile | 600 s | 17.77 | PASS; 10,594 frames |
| Stalled reader + forced Wi-Fi reconnect | 120 s | 16.70 including intentional stall/outage | PASS; camera and inference continued, stream recovered |

The ten-minute window recorded 300 completed observations, about
0.50 results/s, with zero inference failures and zero driver discards.
Processing wall time with capture/transport running averaged
1.245 s and peaked at 1.318 s. Video FPS is separate
from inference cadence; repeated-evidence confirmation takes multiple seconds.
This is one static indoor bench scene on one LAN, not RF-load or multi-day
security reliability qualification. 640×480 remains an input-safety limit, not
a qualified real-time profile for this device.

Sampled stack-coloration high water before model destruction was 52,928 bytes
out of an adjusted 130,848, leaving 77,920 bytes. The VWW fixture retains
its separate 512 KiB budget. User-heap samples ranged from
2,150,560 to 2,150,560 bytes; kernel samples from
58,544 to 61,824. These are periodic samples, not allocator peaks.
After joined shutdown, two successive observations matched at
76,536 user-heap and 53,208 kernel-heap bytes. This image predates the tensor-view
teardown correction; stable samples within a run did not establish leak freedom
across detector lifetimes. The corrected firmware is checked separately below.

An initial one-second/512 KiB profile completed inference but saturated CPU and
delivered 16.39 video FPS with 784 driver discards. It was rejected for video
performance. The selected profile scopes the native context and supplies an
explicit lower analysis duty cycle. That tradeoff is recorded in the numeric
artifacts, rather than describing the initial functional pass as an FPS pass.

The ordinary continuous camera supervisor and authenticated gateway ingest were
restored with this image. A receiver with TLS certificate validation enabled
decoded 89 physical-camera H.264 frames in 5.001 seconds (17.8 FPS) through the
existing gateway. This is a local receiver check, not phone qualification.

### Tensor-view teardown correction

The later [corrected firmware](../tests/detection/results/2026-10-21-pico/ownership-correction.json)
uses the same model, Rust preprocessing/postprocessing and resource profile.
All 65 separately compiled numerical SDK objects are byte-identical; only the
generated SDK graph owner changes. The earlier corpus and ten-minute evidence
remain attached to their original image rather than being relabeled.

The corrected image passed a separate 180-second run at **17.77 decoded video
FPS**, 3,100 decoded frames, 90 completed observations and zero inference failures
or driver discards. Mean inference wall time was 1.228 s, maximum 1.302 s.
Real-image startup scores remained 940 and 415; every call checked native context
restoration. Stack high water was 52,928 of 130,840 adjusted bytes.
After joined shutdown, both samples measured **22,368 user-heap bytes** and
53,208 kernel-heap bytes, compared with 76,536 user-heap bytes in the earlier
image. Host ASan checks exact allocation balance and rejects the missing-view
retirement negative control. See [corrected streaming evidence](../tests/detection/results/2026-10-21-pico/ownership-180s-on.json).

The corrected image also passed the 120-second stalled-reader/forced Wi-Fi
reconnect test: 16.79 delivered FPS including the intentional stall/outage,
60 successful detections, zero driver discards and the same shutdown heap usage.
The continuous supervisor and authenticated ingest were restored with this
image. A verified-TLS gateway receiver decoded 88 H.264 frames in 5.001 seconds
(17.6 FPS). These bounded local checks do not establish field reliability.
See [recovery](../tests/detection/results/2026-10-21-pico/ownership-recovery.json)
and [gateway restoration](../tests/detection/results/2026-10-21-pico/ownership-gateway-restoration.json).

## Reproduction

```sh
python3 upstream/esp-dl/prepare.py
NXRS_PLATFORM=linux cargo test --locked -p nxrs-detector -p nxrs-detection-service \
  -p nxrs-camera-stream --features nxrs-detector/pico-model,nxrs-detector/person-model
NXRS_PLATFORM=linux cargo run --release --locked -p nxrs-detector --features pico-model \
  --example evaluate-pico -- /private/tmp/prepared/manifest.tsv /private/tmp/boxes.tsv \
  > /private/tmp/scores.tsv
python3 tests/detection/report.py --dataset /private/tmp/prepared/dataset.json \
  --scores /private/tmp/scores.tsv --out /private/tmp/accuracy.json --opaque-hal-timing
python3 tests/detection/report_boxes.py --dataset /private/tmp/prepared/dataset.json \
  --annotations /private/tmp/instances_val2017.json --boxes /private/tmp/boxes.tsv \
  --out /private/tmp/localization.json
bash tests/nuttx-std/stream/build.sh --platform esp32s3-camera-stream --probe
python3 tests/detection/run_device.py --port /dev/cu.usbmodemCAMERA \
  --expected-serial CAMERA_SERIAL --dataset /private/tmp/prepared \
  --scores /private/tmp/native.tsv --report /private/tmp/native.json
bash tests/nuttx-std/stream/build.sh --platform esp32s3-camera-stream
```

Follow the verified-device, recovery-backup and private-credential procedures in
the [stream fixture](../tests/nuttx-std/stream/README.md). Serial evaluation starts
no camera or Wi-Fi, uses bounded acknowledged chunks for the 64-byte console
ring, preserves partial failure evidence and joins before returning. Its timings
exclude USB transfer and do not establish streaming FPS. Use the stream runner's
`--no-detection` and `--require-detection` for same-image streaming measurements.
