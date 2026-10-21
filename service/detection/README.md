# Person detection for the camera application

This Rust service adds on-device person-presence classification and security
events to `app/camera-stream`. It detects whether a person is present; it does
not locate bounding boxes, identify people, or classify vehicles/pets.

`PersonDetector` separates model execution from scheduling and alert policy.
The optional `person-model` feature supplies the Apache-2.0 MLPerf Tiny Visual
Wake Words model, compiled into quantized Rust by MicroFlow. MicroFlow is
MIT/Apache-2.0, zune-jpeg is MIT/Apache-2.0/Zlib, and nalgebra is BSD-3-Clause.
Graph execution, buffers, preprocessing, quantization and policy remain Rust.
No cloud account, runtime model download or native inference runtime is required.
Weight provenance and license notices are in [models](models/NOTICE).

The [qualification report](../../docs/camera-detection-qualification.md) records
17.77 decoded video FPS versus 17.81 with inference off on the same ESP32-S3
firmware at 320×240/quality 81. The baseline model's measured recall is 70.75%
on the VWW sample and 50% on the separate any-person Open Images sample. These
misses are material; this is not a dependable standalone intrusion detector.
640×480/quality 90 failed the device's detection freshness qualification.

## Integration and ownership

The app accepts an explicitly constructed `SecurityDetection` before capture
starts. Product wiring creates `Worker` with a frame capacity, sample interval,
stack budget and model initializer. For example:

```rust,ignore
let worker = Worker::spawn(
    WorkerOptions {
        frame_capacity: 153_600,
        sample_interval_ms: 1_000,
        stack_bytes: 512 * 1024,
    },
    || Ok(MobileNet::default()),
)?;
let detection = SecurityDetection::new(worker, Policy::default())?;
camera.attach_detection(detection)?;
camera.start(320, 240)?;
// The existing owner continues calling camera.poll(now_ms, online).
// camera.detection().unwrap().snapshot() exposes typed state and recent events.
```

One preallocated JPEG buffer moves from producer to inference to completion and
back. A slow model cannot accumulate queued frames, allocate on offers, or retain
a borrowed camera frame. The camera owner only performs a bounded copy and
nonblocking mailbox transfers. Decode/inference runs on one separate worker;
no decoder thread pool is enabled. Inference continues offline. Video remains
independent of model errors. Stop closes admission and joins before dropping
model/buffer storage; a failed worker cannot report clean shutdown. Replace the
worker before restarting a stopped detection-enabled composition.

The fixture chooses one-second minimum sampling and a lower-priority worker
(90 versus the camera owner's 100). This uses one native scheduler call under
`tests/`, because Rust std has no priority setter. No HAL behavior moves to C,
and no platform flags enter inference, transport or alert algorithms.

The owner uses `Mutex::try_lock` to transfer a complete job/result and immediately
skips contention. The worker sleeps on a condition variable and executes the
model outside the lock. A bounded channel's `try_recv` can spin on a partially
published slot; on a fixed-priority RTOS that can starve the lower-priority
producer. The mailbox avoids that publication window. Startup acknowledgement
and explicit stop may wait; capture poll/admission do not wait for the worker.

## Inputs, state and bounds

- Baseline/sequential 8-bit JPEG, grayscale or RGB, at most 640×480 and 153,600
  encoded bytes. Mismatched dimensions, progressive/CMYK inputs, invalid markers
  and decoder errors are rejected. The output-size limit is explicit; input
  metadata also stays bounded by encoded size. Decoder scratch allocations are
  additional to the persistent JPEG/worker stack and are measured on device.
- Full-resolution RGB decoding followed by pixel-center bilinear resize preserves the
  entire frame as 96×96 RGB. There is no crop, orientation guess or letterbox.
  RGB bytes minus 128 exactly match the pinned model's int8 input quantization.
- Default entry score is 500/1000, leave score 300/1000, with two consecutive
  confirming observations. Scores in the middle band preserve the current
  state and cancel a pending transition. Scores are model outputs, not calibrated
  intrusion probabilities.
- Results expire after five seconds; confirmation samples may be at most four
  seconds apart. Future/backwards/repeated metadata is rejected. Failure,
  expiry and startup are `Unavailable`, distinct from `Clear` and `Person`.
- The application retains eight typed events (`PersonEntered`, `PersonLeft`,
  `Unavailable`) with monotonically increasing IDs and a total event count.
  Consumers can detect overwritten history. IDs are per composition instance;
  consumers retaining history across restart also need an owner-supplied session
  identity. Event times use the owner clock,
  not UTC. No notification or recording is delivered implicitly.

Consecutive observations provide debounce, not an independence assumption:
correlated false positives can persist. Static-image classification does not
establish temporal event recall or false alarms per hour. A stopped camera feed
or failed detector must never be interpreted as proof that a scene is clear.

## Reproduce the checks

```sh
cargo test --locked -p nxrs-detection-service -p nxrs-camera-stream \
  --features nxrs-detection-service/person-model
cargo clippy --locked -p nxrs-detection-service -p nxrs-camera-stream \
  --all-targets --features nxrs-detection-service/person-model -- -D warnings
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_architecture.py
python3 -m unittest discover -s tests/host -p test_detection_qualification.py
python3 tests/detection/check_padding_control.py
```

The padding negative control reverses the actual runtime patch in an isolated
copy and requires the real SAME-padding regression to fail. Reference-image
tests also execute the selected model. The unmodified MicroFlow 0.1.3 runtime
uses centered padding for stride-two/even inputs, changing model semantics.
[The small source capsule and patch ledger](../../upstream/microflow/README.md)
keep the correction reproducible without editing a registry cache.

## Dataset evaluation

Use an isolated Python environment with
[reference requirements](../../tests/detection/requirements-reference.txt).
Download evaluation images into a new private directory outside the checkout:

```sh
python3 tests/detection/prepare.py --out /private/tmp/vww-evaluation --count 800 \
  --exclude-dataset tests/detection/results/2026-10-21/exploratory-exclusions.json
cargo run --release --locked -p nxrs-detection-service --features person-model \
  --example evaluate -- /private/tmp/vww-evaluation/manifest.tsv \
  /private/tmp/vww-reference-pixels > /private/tmp/vww-scores.tsv
python3 tests/detection/report.py \
  --dataset /private/tmp/vww-evaluation/dataset.json \
  --scores /private/tmp/vww-scores.tsv --out /private/tmp/vww-report.json \
  --reference-pixels /private/tmp/vww-reference-pixels \
  --model service/detection/models/person_detect.tflite
```

Selection ranks images by SHA-256 of `nxrs-vww-v1:IMAGE_ID`, separately per
class. Labels follow the official Visual Wake Words converter: a person's
COCO **annotated object area** must exceed 0.5% of image area. Bounding-box
rectangle area is different and must not be substituted. `--exclude-dataset`
excludes every exploratory image ID from a later holdout; `--skip-per-class`
selects later ranks. Manifests retain IDs, original licenses and source/prepared
image hashes. No images are redistributed by this repository. Input images are
converted to camera-equivalent 320×240 baseline JPEG quality 81 before running
the actual Rust preprocessing and model.

The report includes confusion counts, precision/recall, Wilson intervals,
ROC AUC and latency. Optional TensorFlow Lite comparison uses every exact
Rust-preprocessed RGB input, rather than a separate approximate Python resize.
The frozen model/500 threshold is evaluated independently of model selection.
These are sample results, not full COCO/VWW benchmark certification. Model
training membership cannot be independently established from the published
weights; scene distribution, tiny/distant people, lighting and JPEG quality can
change performance. Host latency does not establish MCU latency or video FPS.

For the second, distinct labeling task, prepare the Open Images sample with:

```sh
python3 tests/detection/prepare_open_images.py \
  --out /private/tmp/open-images-evaluation --count 200
```

Use its `manifest.tsv` and `dataset.json` with the same evaluator/report commands.
Metadata hashes, explicit human-positive/negative labels, rotation-zero filtering
and per-photo attribution make selection reproducible. This task includes any
visible person, unlike VWW's minimum annotated object area; keep its metrics
separate. Prepared JPEG hashes depend on the recorded Pillow/codec environment.

## Physical qualification

Follow the verified-device and private-credential procedures in the
[stream fixture](../../tests/nuttx-std/stream/README.md), including a backup before
flashing. Build with `bash tests/nuttx-std/stream/build.sh --feature person-detection`.
The streaming command defaults to inference enabled in that build; its final
`no-detection` argument provides a same-binary control.

The existing host runner accepts `--no-detection` for that control, or
`--require-detection` to require independent on-device model smoke scores,
successful fresh inference, resource telemetry, decoded video and clean worker/
camera/Wi-Fi shutdown. It records video FPS independently of inference counts
and wall latency. The qualification image supplies a 512 KiB stack and one
153,600-byte JPEG capacity for inference; actual decoder/allocator/RTOS usage
is additional. This is a PSRAM product profile, not a tiny-SRAM qualification.

Detection results remain in the typed Rust API and local qualification telemetry.
The continuous supervisor also writes validated detection records and their host
receipt time into its existing mode-0600 status file. Disconnect, shutdown or
session failure invalidates the last presence state. These records do not extend
the independent stream-health deadline; consumers must check session health and
receipt age as well as the device's observation age.
No new unauthenticated listener or phone notification is exposed. Authenticated
remote event delivery, bounding-box overlays, outdoor/night field qualification
and long-term security-product reliability require their own integration and
measurements. The existing camera HTTP link retains its documented trusted-LAN
constraints.
