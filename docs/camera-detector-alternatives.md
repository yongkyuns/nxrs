# Pretrained person-detector comparison

This study evaluated whether available FOMO or NanoDet weights offered a better person-detection tradeoff than the camera's PICO model. It compared frozen images at a fixed threshold, without retraining or changing the production detector. The October 22, 2026 results apply to the named checkpoints, not to every model in those families.

## Test setup

### Models and execution

| Model | Input and weights | Execution used for this comparison |
| --- | --- | --- |
| PICO reference | PICO_S8_V1, 224×224 RGB int8 | Previously qualified frozen host/device results |
| Norik FOMO | 64×64 RGB; public snapshot `159781`; int8 and float32 exports | Exported Edge Impulse WASM SDK |
| Jahou FOMO | 96×96 RGB; public snapshot `278974`; int8 and float32 exports | Exported Edge Impulse WASM SDK |
| NanoDet-m-0.5x | 320×320 float32; pinned checkpoint and v0.4.2 source | Upstream PyTorch CPU model and decoder, one thread |

All alternatives ran on an arm64 macOS host; the retained environment record does not identify the CPU model. Five configurations each processed all 1,000 images. Independent TensorFlow Lite runs also evaluated both int8 FOMO exports on the same corpus, adding 2,000 image evaluations. Exact model, source and export hashes are in the [artifact manifest](../tests/detection/alternatives.json).

### Datasets and scoring

| Evaluation | Sample and label definition | Measurement |
| --- | --- | --- |
| COCO/VWW presence | 800 JPEGs: 400 positive, 400 negative; positive means an annotated person's area exceeds 0.5% of the image | Image-level accuracy, person recall and false-positive count |
| Open Images presence | 200 JPEGs: 100 positive, 100 negative; explicit human labels for any person | Same presence metrics, reported separately because the label rule differs |
| COCO boxes | 764 non-crowd images containing 1,282 people | Person precision, recall and F1 at IoU 0.5, score 0.5 and at most ten boxes/frame |
| Host execution | COCO/VWW runs after two warm-up calls per configuration | Mean JPEG preparation + inference + postprocessing time; file reads and IPC excluded |

Every model used score threshold **0.5**, without threshold tuning on these samples. Model scores are not calibrated to the same probability. FOMO presence counted accepted centroids within image content; NanoDet presence counted accepted person boxes. FOMO centroids were not scored as full person bounds. The box figures are fixed-threshold metrics, not COCO mAP.

FOMO used each project's `fit-long` preprocessing: centered black letterboxing, Lanczos resizing and RGB888 values passed to its SDK DSP. NanoDet used its upstream aspect-preserving validation transform and normalization. Input hashes and dataset manifests were checked before evaluation.

### Assumptions and limits

The balanced static samples do not estimate field prevalence, false alarms per hour, temporal incident recall or night/IR reliability. Complete independence from model training data was not established: recognizable Jahou COCO filenames did not overlap this sample, but other training names and base-model membership remain unknown.

The host timings include different runtimes and preparation paths. They do not isolate architecture efficiency and cannot predict ESP32-S3 throughput. No alternative was flashed, so the study provides no alternate-model device RAM, concurrent video or joined-shutdown result. Public SDK licensing and deployment terms are also separate from this accuracy screen.

<a id="primary-results"></a>

## Presence results

### COCO/VWW: 400 positive and 400 negative images

| Model | Accuracy | Person recall | False positives / 400 negatives |
| --- | ---: | ---: | ---: |
| PICO reference | 86.00% | 89.75% | 71 |
| Norik FOMO int8 | 53.125% | 89.25% | 332 |
| Jahou FOMO int8 | 59.00% | 74.00% | 224 |
| NanoDet-m-0.5x | 85.125% | 73.00% | 11 |

### Open Images: 100 positive and 100 negative images

| Model | Accuracy | Person recall | False positives / 100 negatives |
| --- | ---: | ---: | ---: |
| PICO reference | 76.50% | 67.00% | 14 |
| Norik FOMO int8 | 52.50% | 94.00% | 89 |
| Jahou FOMO int8 | 60.00% | 83.00% | 63 |
| NanoDet-m-0.5x | 71.50% | 43.00% | 0 |

Both FOMO checkpoints classified many negative images as containing people. Their float32 exports and the independent int8 TensorFlow Lite screen showed the same qualitative weakness. The latter used raw heatmap maxima, so it corroborates the screening result without establishing numerical equivalence with SDK centroid processing. [Full precision/runtime results](history/detector-screening-study.md#primary-results).

NanoDet produced fewer false positives than PICO in both datasets, at the cost of lower person recall. A single fixed threshold does not establish which model has the best achievable operating point after calibration.

## Box-localization results

| Model | Precision | Recall | F1 |
| --- | ---: | ---: | ---: |
| PICO reference | 83.18% | 41.65% | 55.51% |
| NanoDet-m-0.5x | 92.54% | 30.97% | 46.41% |

NanoDet produced 397 true positives, 32 false positives and 885 false negatives. Its accepted boxes were more precise, but it located fewer of the annotated people. Image-level presence recall and per-person box recall measure different outcomes.

<a id="performance-and-qualification-limits"></a>

## Host execution results

| Alternative implementation | Mean measured pipeline time |
| --- | ---: |
| Norik int8 / WASM SDK | 0.741 ms |
| Jahou int8 / WASM SDK | 1.160 ms |
| NanoDet float32 / PyTorch | 8.974 ms |

FOMO's small host execution cost did not compensate for its false-positive rate in this screen. These host observations are not comparable to the [physical PICO streaming measurements](camera-pico-performance.md).

## Interpretation

**False positives, rather than host speed, disqualify these FOMO checkpoints at the tested threshold.** Norik marked 83% of COCO/VWW negative images and 89% of Open Images negatives as positive; Jahou marked 56% and 63%. Similar outcomes from float32 exports and a second int8 engine make quantization alone or one runtime defect unlikely explanations. Training-domain mismatch and score calibration remain plausible causes, not demonstrated ones. The screen does not isolate input resolution or model architecture as the dominant cause.

**NanoDet makes a different error tradeoff.** On COCO/VWW it reduces false positives from PICO's 71 to 11, but also reduces person recall from 89.75% to 73%. Higher box precision alongside lower box recall means fewer incorrect boxes and more missed people. A user prioritizing fewer nuisance alerts may prefer that tradeoff; one prioritizing incident recall may not. Neither model's score 0.5 represents a matched operating point, so the table cannot establish the best achievable tradeoff.

**Presence and localization answer different product questions.** PICO's 89.75% COCO/VWW presence recall means it often finds some person in a positive image; its 41.65% box recall means many individual annotated people remain unlocalized. Multi-person coverage therefore needs per-person evaluation. The lower Open Images presence results cannot be attributed solely to domain shift: that dataset labels any person, while the VWW rule labels images containing only sufficiently small people as negative.

Balanced-image accuracy is also a poor proxy for an always-on alarm. When person-free scenes dominate, even a modest false-positive rate can generate many positive frames. Consecutive frames are correlated, so multiplying independent per-frame error probabilities would not predict the service's temporal-confirmation false-alarm rate.

<a id="conclusions"></a>

## Conclusions and next qualification

PICO remains the supported choice from this screen, with NanoDet a precision-oriented candidate. The first improvement step is **operating-point evaluation**, not a device port: use a separate calibration set to choose thresholds at a common false-positive budget, then compare recall on an untouched holdout, including person-size and lighting breakdowns. If these FOMO weights remain poor at a useful recall, evaluate domain-matched training rather than spending effort porting the current exports.

Only a candidate that improves the required tradeoff should proceed to the device. Measure peak model/preprocessing memory and inference alongside video, then evaluate recorded sequences for incident recall, alert delay and false alerts per camera-hour using the actual temporal policy. Keep the commercial SDK review separate. This sequence resolves model suitability before spending effort on embedded integration.

<a id="artifacts-and-licensing"></a>
<a id="reproduction"></a>

## Evidence and reproduction

- [Pinned models and exports](../tests/detection/alternatives.json) and [numeric evidence](../tests/detection/results/2026-10-22-alternatives/): per-image scores, timing distributions and environment/source records.
- [Original screening report](history/detector-screening-study.md): fetch/evaluation commands, independent engine checks and recorded license provenance. Public-project Apache 2.0 labels do not cover every generated SDK file; no exported SDK or model was adopted by this screen.
- [PICO reference qualification](camera-pico-qualification.md) and [device pipeline performance](camera-pico-performance.md).

Images were evaluated locally. Public evidence contains numeric results and hashes; dataset images, credentials and runtime logs remain private.
