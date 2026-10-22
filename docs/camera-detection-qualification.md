# VWW baseline camera person-detection qualification

This report preserves the original VWW model measurements. The detector now
lives in `hal/detector`; [PICO qualification](camera-pico-qualification.md) records
the subsequent localized model and its separate device measurements.

The camera now has an opt-in Rust person-presence service with bounded owned
frames, hysteresis/debounce, expiring observations, typed events and joined
shutdown. The existing 320×240, JPEG-quality-81 stream remains the qualified
ESP32-S3 configuration. This is a baseline classifier: it has material missed
person rates, produces no bounding boxes, and has no remote alert delivery.

## Reproducible accuracy

The unchanged Apache-2.0 MLPerf Tiny RGB VWW model runs through the reviewed
Rust MicroFlow runtime and scalar Rust JPEG preprocessing. Model identity,
licenses, source patches, input preparation and commands are in the
[service guide](../service/detection/README.md). The entry threshold stayed at
500/1000 throughout evaluation; the leave threshold is 300 and transitions need
two confirming observations.

| Dataset and labeling task | Samples | Accuracy | Precision | Recall | TP / TN / FP / FN |
| --- | ---: | ---: | ---: | ---: | --- |
| COCO val2017 / VWW, person annotated area >0.5% of image | 800, balanced | 81.125% | 89.27% | 70.75% | 283 / 366 / 34 / 117 |
| Open Images validation, human-verified any-person presence/absence | 200, balanced | 72.00% | 89.29% | 50.00% | 50 / 94 / 6 / 50 |

The VWW sample excludes all 200 exploratory image IDs. Open Images selection
uses only explicit, nonconflicting human labels, rotation-zero metadata and
CC-BY-2.0 photo records. Missing annotations are never assumed to be negatives.
The task definitions differ, so the two results must not be pooled. Both are
balanced convenience samples; their precision is not deployment precision.
Neither establishes model-training disjointness, temporal alarm accuracy,
false alarms per hour or night/IR performance. Misses include small or partial
people and unusual poses; the baseline is not a dependable intrusion detector.

VWW accuracy's 95% Wilson interval is 78.27–83.69%; recall's is 66.11–74.99%.
Open Images accuracy's interval is 65.41–77.76%; recall's is 40.38–59.62%.
ROC AUC is 0.8897 and 0.8296, respectively. Full scores, image IDs, source/prepared
hashes, attribution and uncertainty are recorded under
[results](../tests/detection/results/2026-10-21/). No evaluation or private camera
photos are redistributed.

TensorFlow Lite 2.20.0 evaluated every exact Rust-preprocessed RGB tensor. The
VWW comparison has one classification disagreement out of 800, mean absolute
score difference 0.43/1000 and maximum 27/1000. Open Images has zero disagreements,
mean difference 0.40/1000 and maximum 8/1000. MicroFlow uses floating-point
requantization, so this is close numerical agreement rather than bit-exact
integer-kernel equivalence. The approximation gate is explicit in the reports.

On an Apple M6 with Rust 1.90.0, release compilation, service optimization level
3 and fat LTO, VWW JPEG preprocessing plus inference averaged 1.191 ms, with
1.302 ms p95. Open Images averaged 1.193 ms, with 1.512 ms p95. File I/O,
thread dispatch, capture, transport and temporal confirmation are excluded.
These host numbers do not establish MCU performance.

## Physical ESP32-S3 results

One Freenove ESP32-S3 N16R8 with OV3660, one 240 MHz core and 8 MiB PSRAM ran the
same release binary for all following measurements. The model and all inference
behavior are Rust; no native inference SDK is linked. The worker has priority 90
versus the camera owner's 100, a 512 KiB requested stack, and one 153,600-byte
JPEG allocation. It analyzes the latest available frame with a one-second
minimum admission interval, skipping work while occupied.

| Configuration | Test | Fully decoded video FPS | Outcome |
| --- | --- | ---: | --- |
| 320×240 / quality 81 / detection off | 60 s control | 17.81 | PASS |
| 320×240 / quality 81 / detection on | 600 s | 17.77 | PASS; 10,578 decoded frames |
| 320×240 / quality 81 / detection on | stalled reader + forced Wi-Fi reconnect, 120 s | 16.90 including outage | PASS; stream recovered and capture/inference continued |
| 640×480 / quality 90 / detection off | 60 s boundary control | 7.01 | Video PASS |
| 640×480 / quality 90 / detection on | 60 s boundary test | 7.93 | Detection FAIL: freshness/latency budget exceeded |

The ten-minute run completed 324 successful inferences: about 0.54 results/s,
mean processing wall time 1.843 s and maximum 2.076 s. It recorded zero detection
failures and zero camera-driver discards. Video FPS is separate from inference
cadence; confirmation therefore takes seconds. This is not 17 inferences/s.

The model worker's stack-coloration high water was 453,408 bytes out of an
adjusted 524,056-byte stack, leaving 70,648 bytes. Typical sampled user heap was
1,730,664 bytes, compared with 821,896 in the video-only control; the highest
sample was 1,767,136 bytes. Kernel samples ranged from 58,544 to 63,424 bytes.
Heap observations are periodic samples, not peak
allocator measurements. After shutdown, two successive observations matched at
22,328 user-heap bytes and 53,208 kernel-heap bytes. Camera, inference and Wi-Fi
reported joined/quiescent shutdown.

The VGA/quality-90 run reached 5.197 s per inference and correctly lost its fresh
presence state. Validating JPEG dimensions up to 640×480 is an input-safety bound,
not a promise of real-time performance for that resolution on this MCU. Keep the
qualified QVGA profile for this device; do not describe the VGA run as a pass.

All 117 selected native ABI witnesses passed against the staged NuttX headers.
This qualifies their recorded scope, not every NuttX libc/signal API. Exact
firmware/config/source hashes, all numeric runs, failed boundary evidence and
measurement limitations are in [device.json](../tests/detection/results/2026-10-21/device.json).
The scene was one static indoor bench view on one LAN; complex scenes, RF load
and multi-day reliability still need field measurements.

## Ownership and operating checks

An earlier bounded-channel implementation could spin in `try_recv` while a
lower-priority producer was preempted during publication. Fixed-priority task
snapshots exposed the resulting starvation. The final implementation transfers
whole jobs/results through one mutex-protected mailbox: capture only tries the
lock and immediately skips contention. JPEG copying and model execution occur
outside the lock. Startup and stop may wait; the capture loop does not wait for
the inference worker. Tests cover deliberately held mailbox locks, overload,
owned-copy isolation, initializer failure/unwind, sticky worker panic, expiry,
replay, hysteresis, and joined shutdown.

The ordinary camera session and authenticated gateway ingest were restored with
inference enabled. A verified-TLS WebRTC receiver decoded 88 physical-camera
H.264 frames in 5.002 seconds (17.59 FPS) through that existing gateway. This was
a local receiver, not an iPhone qualification. The continuous supervisor writes
validated detection state into its existing private status file and invalidates
it on disconnect/shutdown; detection telemetry does not renew camera health.
No additional listener, remote notification or footage retention is implicit.
