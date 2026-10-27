# Public pretrained alternative screening

See [the evaluation report](../../../../docs/camera-detector-alternatives.md)
for methodology, interpretation, licensing and reproduction. These are host
screening results, not ESP32-S3 or concurrent video measurements.

Each `fomo-*` or `nanodet-*` folder contains the actual per-image scores,
typed-output geometry, artifact provenance and fixed-threshold metrics.
`tflite-*` folders independently screen bare quantized FOMO models before
centroid postprocessing; their output semantics differ and no bit-equivalence
claim is made. Model/SDK binaries, camera/dataset images and private logs are
excluded. `training-overlap.json` records the partial filename-based check and
its limits. `validation.json` records source hashes and local checks.

The existing PICO qualification remains the baseline. No candidate replaces the
running implementation: FOMO false positives are excessive on this sample;
NanoDet has higher precision and lower recall at the fixed threshold. Scores
are uncalibrated between models and were not tuned on this comparison.
