# Pretrained detector screening

On 2026-10-22, two public Edge Impulse FOMO checkpoints and NanoDet-m-0.5x
were evaluated against the same frozen camera JPEGs used for PICO: 800 balanced
COCO/VWW images and 200 balanced Open Images images. Both float32 and int8 FOMO
exports ran through their actual exported WASM engine. NanoDet ran its upstream
PyTorch model, validation preprocessing and box decoder on CPU with one thread.
Five configurations each completed all 1,000 images. An additional 2,000 images
ran through bare int8 FOMO models in TensorFlow Lite as an independent screen.

Keep PICO as the current default. These FOMO checkpoints produce excessive
false positives on our sample; NanoDet offers substantially fewer false positives
but lower person recall at the fixed threshold. These results evaluate these
particular trained weights, not the best possible FOMO or NanoDet architecture.
No production HAL, application policy or running device firmware was changed.

## Primary results

All rows use score 0.5. Scores from different models are not calibrated to the
same probability or operating point; no threshold was tuned on this comparison.
FOMO presence uses its accepted centroids inside the image content. NanoDet's
raw-score classification agrees with accepted person boxes on every sample.
PICO rows reference the preceding frozen host/device qualification.

| Model | COCO/VWW accuracy | Person recall | False positives / 400 negatives | Open Images accuracy | Person recall | False positives / 100 negatives |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| PICO, current | 86.00% | 89.75% | 71 | 76.50% | 67.00% | 14 |
| Norik FOMO, 64×64 RGB, int8 | 53.125% | 89.25% | 332 | 52.50% | 94.00% | 89 |
| Jahou FOMO, 96×96 RGB, int8 | 59.00% | 74.00% | 224 | 60.00% | 83.00% | 63 |
| NanoDet-m-0.5x, 320×320, float32 | 85.125% | 73.00% | 11 | 71.50% | 43.00% | 0 |

Float32 FOMO gives the same qualitative outcome: Norik accuracy 52.75%/52.50%
and Jahou 60.50%/61.00% on COCO/VWW/Open Images respectively. Independent bare
int8 TensorFlow Lite heatmap maxima give Norik 53.25%/51.50% and Jahou
59.25%/60.50%. Raw heatmap maxima include padding cells and precede SDK centroid
grouping/filtering, so they are reported separately. This is corroboration of
poor generalization, not a numerical-equivalence claim between runtimes or
postprocessed confidence values.

For actual boxes, NanoDet produces 397 TP, 32 FP and 885 FN on the same 764
non-crowd COCO images and 1,282 people used for the PICO box assessment:
**92.54% precision, 30.97% recall, 46.41% F1**, at score 0.5 and IoU 0.5,
with at most ten boxes per frame. PICO previously gave **83.18% precision,
41.65% recall, 55.51% F1**. FOMO activation rectangles identify centroids and
are not evaluated as full person bounds. These fixed-threshold figures are not
COCO mAP. Crowded images are excluded using the existing benchmark rule.

## Performance and qualification limits

Mean measured host pipeline time on COCO/VWW was 0.741 ms for Norik int8 WASM,
1.160 ms for Jahou int8 WASM and 8.974 ms for NanoDet float32/PyTorch. The FOMO
figure combines Python JPEG/resize/packing time with the SDK engine's
DSP/numerical/postprocessing time; IPC and file I/O are excluded. NanoDet
includes JPEG decoding, upstream image preparation, numerical execution and
postprocessing, excluding file reads. Each main run warms up twice. Runtime
implementations differ, so these numbers are not an architecture-only speed
comparison. Reports retain means, medians, p95 and maxima.

No alternative was ported/flashed after screening. There is no alternate
ESP32-S3 inference rate, activation-memory measurement or concurrent video FPS
result. The existing PICO camera/gateway remain running; their preceding
**17.766 video FPS and 1.3333 detections/s** measurement remains a PICO result.
Public ESP-EYE/Nicla timings are not relabeled as measurements on our S3.
Adoption would require a separate Rust HAL implementation/qualification and a
same-image video control, concurrent inference test and joined-shutdown checks.

FOMO preprocessing follows each public project's documented `fit-long` input:
centered black letterbox with Lanczos resizing, then packed RGB888 values passed
to the actual SDK DSP. NanoDet uses its original aspect-preserving validation
transform and normalization. All input JPEG hashes and dataset manifests are
checked before inference. Training filenames identify no overlap between the
174 recognizable COCO names in Jahou's project and our frozen COCO sample;
Norik's names are not identifiable COCO IDs. Other filenames and base-model
training membership remain unknown. This does not prove complete independence.
Static balanced samples do not establish field prevalence, temporal incident
recall, false alarms per hour, night/IR performance or phone acceptance.

## Artifacts and licensing

The exact model/source/export pins are in
[`tests/detection/alternatives.json`](../tests/detection/alternatives.json).
The [public Norik project](https://studio.edgeimpulse.com/public/121370/latest)
and [public Jahou project](https://studio.edgeimpulse.com/public/277042/latest)
are version-one snapshots whose internal public IDs are 159781 and 278974.
Their public deployment API provided both precision variants without requiring
new training. API `isEnabled` flags reflect selection, not artifact availability;
the int8 exports were successfully retrieved and evaluated.

Both public projects list Apache 2.0. Generated C++ wrapper files also carry
additional subscription terms; those SDK terms must be assessed separately
before product use. No generated SDK or model binary is committed or adopted.
Edge Impulse's [Developer terms](https://www.edgeimpulse.com/legal/terms-of-service)
distinguish public-project models from SDK assets, and its
[pricing](https://www.edgeimpulse.com/pricing) lists separate production terms.
The [NanoDet repository](https://github.com/RangiLyu/nanodet) is Apache 2.0;
the exact checkpoint and v0.4.2 source revision are pinned. The older Lightning
checkpoint is loaded with PyTorch `weights_only=True`, permitting only an inert
placeholder for its callback-metadata key, and requires every model weight to
match. No executable checkpoint payload is loaded.

Numeric results, per-image scores, host timings and qualification boundaries are
under [2026-10-22-alternatives](../tests/detection/results/2026-10-22-alternatives/).
Runtime logs, credentials and dataset images stay private. All image inference
ran locally; dataset images were not uploaded to Edge Impulse.

## Reproduction

Use a private directory and a Python 3.12 virtual environment. The fetcher
verifies exact archive/member hashes and extracts only the WASM module/loader
and bare TFLite model, excluding generated C++ SDK sources. It fails if a
historical artifact becomes unavailable or changes; it never substitutes a new
model. Install `tests/detection/requirements-alternatives.txt` for NanoDet/WASM
preparation. The independent TFLite screen uses the separate existing
`tests/detection/requirements-reference.txt` environment.

```sh
python3 tests/detection/fetch_alternatives.py --out /private/path/artifacts
git clone --branch v0.4.2 https://github.com/RangiLyu/nanodet /private/path/nanodet
python3 tests/detection/evaluate_alternatives.py --kind fomo \
  --node /path/to/node --export /private/path/artifacts/fomo-159781-int8 \
  --dataset /private/path/vww-official-holdout-800 --out /private/path/norik-coco
python3 tests/detection/evaluate_alternatives.py --kind nanodet \
  --source /private/path/nanodet \
  --checkpoint /private/path/artifacts/nanodet-m-0.5x.ckpt \
  --dataset /private/path/vww-official-holdout-800 --out /private/path/nanodet-coco
python3 tests/detection/verify_fomo_tflite.py \
  --model /private/path/artifacts/fomo-159781-int8.tflite \
  --dataset /private/path/vww-official-holdout-800 --out /private/path/tflite-norik
```

Repeat for snapshot 278974, float32 WASM and the frozen Open Images corpus.
Run `tests/detection/report_boxes.py` on NanoDet's `boxes.tsv` with the verified
official COCO annotations. The original preparation scripts/manifests define
the image selection; score/box TSVs preserve their exact order. Dependency and
source hashes are recorded with the results.
