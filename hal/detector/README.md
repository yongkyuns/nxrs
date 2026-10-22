# Detector capability

The detector HAL owns the common Rust interface and its implementations. The
camera app and detection service do not import SDK types, select platforms or
change their algorithms for a model. The root platform tree supplies bindings:
`std` for host profiles and `nuttx` for the ESP32-S3 camera-stream profile.

```rust,ignore
use detector::{Config, Detector, Image};

let mut detector = detector::open(Config {
    require_localization: true,
    ..Config::default()
})?;
let capabilities = detector.capabilities();
let result = detector.detect(Image::jpeg(&jpeg, width, height))?;
println!("person score: {}", result.person_score.permille());
for person in result.objects.iter() {
    // Bounds use 0..=1000 coordinates relative to the full source image.
    println!("{:?}", person.bounds);
}
// result.objects.truncated explicitly reports bounded output/candidate loss.
```

`Detector` is a synchronous, exclusively owned `Send` resource. It retains no
borrowed image. Errors are capability-owned `DeviceError`; failure does not
produce an empty successful result. Input is baseline 8-bit JPEG, at most 640×480
and 153,600 bytes. Providers report their model dimensions and localization
support. Requesting localization from a presence-only provider is unsupported.
This input-safety limit is separate from a platform's qualified real-time profile.

`service/detection` supplies one owned JPEG, a lower-priority worker, a sampling
budget and temporal alert policy. Model execution cannot queue camera frames or
block the capture loop. The service joins the worker before releasing the HAL.
The current camera application consumes presence/events. It does not yet draw
HAL-provided boxes in the viewer or deliver remote notifications.

## Models and platform implementations

- `person-model`: unchanged Apache-2.0 MLPerf Tiny VWW, 96×96 RGB int8, Rust
  MicroFlow execution. No native inference SDK is needed. Weights and provenance
  are in [models/NOTICE](models/NOTICE); runtime patches are under
  `upstream/microflow`. This retains the previous baseline implementation.
- `pico-model`: unchanged MIT-licensed ESP-DL PICO_S8_V1, 224×224 RGB int8, with
  three person score/box heads. The shared Rust implementation validates and
  decodes JPEG, resizes, quantizes, decodes geometric confidence and DFL boxes,
  and applies bounded NMS. The host uses scalar SDK operators; ESP32-S3 uses its
  optimized SIMD operators. See [model notice](models/PICO-NOTICE).

These are optional model packages, not per-platform provider features. Product
profiles select implementations. With PICO compiled, `open` supplies PICO; with
only VWW compiled, it supplies VWW. No model is implicitly a mock. Contract-only
builds work without either runtime. Upper layers inspect capabilities, not OS
or model identity, when requesting localization.

Private native glue lives beside its consuming implementation in
`src/platform/std/ffi/`; NuttX reuses the Rust model and owns its saved-SIMD-context
binding in `src/platform/nuttx/ffi/`. The native SDK launches no tasks, retains no
caller buffers and has no camera/network callbacks. NuttX prepares the calling
worker's `cop_ai` context only during numerical execution; context switches
preserve that state. A Rust lease serializes kernels and restores the prior
enabled state before releasing ownership, including unwind. JPEG/box processing
and idle time do not retain a newly enabled SIMD context. This adapter requires
the qualified single-core flat NuttX profile. Shared Rust behavior contains no platform/provider flags.

## Numerical SDK scope and ownership

`upstream/esp-dl/source.json` pins source revision, model bytes and every retained
source before and after the narrow SDK port. Preparation removes SDK dual-core
task launching and unused iostream initialization, and corrects a host pointer
alignment check. An attempted SDK dual-core path aborts; all generated calls
explicitly request single-core execution on the Rust-owned worker.

A fixed graph is generated from the pinned FlatBuffer at build time. The target
contains numerical operators and weights, with a 551,936-byte aligned activation
arena. The lifetime plan allocates an output before retiring its inputs and
keeps every result head alive through result copy. SDK tensor views borrow this
arena. The generated SDK owner explicitly deletes registered tensor views:
`ModelContext` otherwise skips them when it does not own a root allocation.
Operators and views are destroyed before the arena. LUT views have unique
owners; native bias packing owns its separate allocations. The opaque SDK graph
has one Rust owner and all calls require exclusive access.

No generic model loader, prebuilt FlatBuffer parser, encrypted model support,
flash/partition access, FreeRTOS emulation or SDK image/alert pipeline is linked.
The SDK subset is fetched explicitly; builds do not silently download models.
Keep MIT notices with distributions. The native C++ runtime has its own runtime
license/exception requirements.

## Build and qualification

```sh
python3 upstream/esp-dl/prepare.py
NXRS_PLATFORM=std cargo test --locked -p nxrs-detector --features pico-model,person-model
NXRS_PLATFORM=std cargo clippy --locked -p nxrs-detector -p nxrs-detection-service \
  -p nxrs-camera-stream --all-targets \
  --features nxrs-detector/pico-model,nxrs-detection-service/person-model -- -D warnings
python3 tools/check-architecture.py
python3 -m unittest discover -s tests/host -p test_architecture.py
```

`NXRS_PICO_SDK` can point at an explicitly prepared source directory. The build
verifies its full inventory against the reviewed hashes. Host PICO builds need a
C++17 compiler/runtime. The ESP32-S3 factory build supplies the actual NuttX
headers and pinned Xtensa C++ compiler. Other CPU/SDK combinations require their
own numerical and physical qualification.

```sh
NXRS_PLATFORM=std cargo run --release --locked -p nxrs-detector --features pico-model \
  --example evaluate-pico -- /path/to/prepared/manifest.tsv /private/tmp/pico-boxes.tsv
bash tests/nuttx-std/stream/build.sh --feature pico-detection
# Serial-only corpus fixture, without a listener or Wi-Fi/camera startup:
bash tests/nuttx-std/stream/build.sh --feature pico-evaluation
```

Run `python3 tests/detection/check_pico_asan.py` for the SDK ownership sanitizer
and allocation-balance check, including on macOS without LeakSanitizer.
The [PICO qualification report](../../docs/camera-pico-qualification.md)
keeps paired accuracy, localization and physical performance evidence.

The evaluator records full HAL-call latency; it does not expose or separately
claim preprocessing time. Dataset preparation remains in `tests/detection`.
Video FPS, per-inference time, inference cadence and detection accuracy are
separate measurements. Static-image accuracy cannot establish false alerts per
hour or night/IR reliability. PICO's higher recall at the existing threshold
comes with increased false positives; retain field qualification and expiry.
