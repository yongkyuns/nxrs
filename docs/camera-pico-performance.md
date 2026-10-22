# ESP32-S3 PICO performance qualification

The original 0.5 observations/s was a two-second product sampling budget. It was
not the PICO model's maximum throughput. Stage measurements exposed expensive
Rust image preparation and a mismatch between the product's memory settings
and Espressif's numerical workload. The portable detector API, model, threshold,
image mapping and output policy remain unchanged.

## Reference and timing boundaries

The pinned ESP-DL v3.3.0 [pedestrian model benchmark](https://github.com/espressif/esp-dl/blob/998b76af271101b1a09b005958ed9e97ec8925a0/models/pedestrian_detect/README.md)
reports 9.2 ms preprocessing, 118.3 ms model execution and 2.4 ms postprocessing
for PICO_S8_V1 at 224×224 RGB int8. These sum to 129.9 ms, whose reciprocal is
about 7.7 executions/s. This is not a simultaneous JPEG-camera/Wi-Fi throughput
measurement. The table does not specify enough input-encoding and timing detail
to equate its preprocessing with our complete JPEG decoding stage.

The same pinned [S3 example configuration](https://github.com/espressif/esp-dl/blob/998b76af271101b1a09b005958ed9e97ec8925a0/examples/pedestrian_detect/sdkconfig.defaults.esp32s3)
selects a 240 MHz CPU, 80 MHz octal PSRAM, a 64 KiB data cache and 64-byte data
cache lines. Our earlier product used 40 MHz PSRAM and a 32 KiB/32-byte data
cache. The numerical parameters were also read directly from flash, whereas
the SDK normally copies parameters into RAM.

An opt-in Rust qualification wrapper observes the actual HAL pipeline:

- Decode: frame validation, JPEG headers and full RGB decode.
- Resize: full-image Q8 bilinear interpolation and int8 quantization.
- Numerical: the scoped compute lease, input copy, SDK graph and output copy.
- Postprocess: maximum confidence, DFL boxes and bounded NMS.

All times are monotonic wall time, including preemption. USB transfer and image
file I/O are outside the native per-call measurements. NuttX observations have
millisecond resolution. The normal `detector::open` path supplies an empty
observation closure and stores no timing data. Product builds exclude the
`qualification` feature, profiling output and serial batch entrypoint.

## Controlled stage measurements

One Freenove ESP32-S3 N16R8/OV3660, one 240 MHz core, ten unchanged 320×240
quality-81 JPEGs from the frozen COCO holdout: five evenly spaced rows per class.
No camera or Wi-Fi runs during this probe. These ten images are a performance
probe, not a new accuracy sample. Each row below reports the mean in milliseconds.

| Configuration | Decode | Resize/quantize | Numerical | Postprocess | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original memory/resize | 118.7 | 140.7 | 355.4 | 3.7 | 618.6 |
| RAM-resident parameters | 118.5 | 140.5 | 277.2 | 3.9 | 540.2 |
| Decoder crates at release opt-level 3 | 115.9 | 142.0 | 277.3 | 3.7 | 538.9 |
| Shared Rust axis/row-slice resize | 116.2 | 52.5 | 277.2 | 3.6 | 449.5 |
| 80 MHz PSRAM | 94.5 | 49.0 | 177.9 | 3.5 | 325.0 |
| 64 KiB/64-byte data cache; selected | 83.7 | 47.4 | 131.6 | 3.7 | 266.5 |

The selected numerical interval is about 11% above the vendor's model-only
number and includes additional lease/copy work. The end-to-end JPEG call is
about 2.3 times faster than the original probe. Its reciprocal is not a claimed
streaming inference rate; capture, networking and detector work share one CPU.

Two further experiments were rejected: raising flash from 40 to 80 MHz reduced
the 325 ms probe to 320 ms, and extra horizontal-row caching reduced 266.5 ms to
262.8 ms while adding scratch storage and complexity. The product keeps 40 MHz
flash and the simpler axis/row-slice resize. Decoder compiler settings alone
did not materially account for the improvement.

## Ownership, portability and resource costs

The resize stays in the shared Rust implementation. Horizontal coordinates are
computed once per image, row/pixel bounds checks are shared across channels,
and Q8 interpolation is algebraically regrouped without changing rounding.
A scalar reference checks complete output bytes at 96 and 224 model sizes,
including RGB/grayscale, odd dimensions, edges, upsampling and downsampling.

The unchanged numerical parameters occupy one aligned 343,120-byte RAM block on
S3, in addition to the existing 551,936-byte activation arena. Native SDK views
retire before either block. Acquisition releases the activation arena if the
parameter allocation fails. ASan and exact allocation balance exercise two
independent SDK owners and a surviving owner after the first is destroyed.
The scoped Rust compute lease and its prior-state/unwind/sticky-failure tests
remain in place.

The 64 KiB cache consumes 32 KiB more internal SRAM than the previous layout.
PSRAM speed, cache layout, flash speed and boot RAM testing belong to the product
profile. They do not enter the common detector API, image algorithms, worker
or alert policy. The provider remains single-core/flat NuttX only until another
execution configuration is independently qualified.

## Complete corpus and concurrent CPU measurements

The optimized HAL reran the same 800 frozen COCO/VWW and 200 Open Images JPEGs.
All 1,000 native scores, box counts and truncation flags match the earlier device
results exactly. Native box coordinates are not recorded by the serial fixture.
The host reevaluation also produces identical scores and byte-identical box TSVs
on all 1,000 images; accuracy remains unchanged. Numerical execution averaged
131.381 ms on COCO and 131.475 ms on Open Images. Full JPEG-to-detections call
latency averaged 264.386 ms and 263.775 ms respectively, without camera/Wi-Fi.

At a requested 500 ms streaming interval, the profiling image delivered 17.78
decoded video FPS and 99 completed detections in 60 seconds, with zero failures
and driver discards. CPU busy reached a median 997/1000. This was rejected for
CPU headroom; it does not establish two detections per second.

At 750 ms, the same image delivered 17.78 video FPS and 79 completed detections
in 60 seconds; median busy was 881.5/1000. A second profiling image additionally
observed the detector worker's thread CPU clock: mean CPU time 283.563 ms versus
588.853 ms wall time across 75 calls after startup. The SDK reported 240 MHz
throughout. Thread CPU accounting includes interrupt time attributed to that
worker. This separates computation from time waiting while sharing the CPU with
capture/networking; it is not an isolated inference-FPS claim.

The factory's selected PICO budget is 750 ms with a 128 KiB worker stack. The VWW
composition retains its separate two-second/512 KiB budget. The shared worker,
HAL API and two-sample confirmation policy do not branch on model or platform.
Faster sampling changes confirmation latency; static-image equivalence does not
establish temporal false-alert reliability.

## Final production firmware on the physical camera

The non-profiling `pico-detection` image is SHA-256
`5f9a59be40991b499d302187fab54f8943a2234ea328848114e201a93333b9f2`.
Its configuration and production source hashes are in
[production-provenance.json](../tests/detection/results/2026-10-22-pico/production-provenance.json).
The firmware reports its actual 750 ms default; neither run overrides the budget.
The qualification-only profiling and serial batch endpoints are absent.

| Production run | Decoded video FPS | Completed detections | Failures | Camera discards |
| --- | ---: | ---: | ---: | ---: |
| Detection disabled, 60 s | 17.79 | — | — | 0 |
| Detection enabled, 600 s | 17.77 | 797 | 0 | 0 |
| Slow reader and forced Wi-Fi reconnect, 120 s | 16.72 | 159 | 0 | 0 |

The steady run completed 1.328 detections/s using the final device report's
600.004 s uptime. Mean call wall time was 510.432 ms, maximum 713 ms. CPU busy
averaged 789.75/1000 across the ten-second samples; scene and connection load
affect this, so it is separate from the controlled profiling comparison.
Sampled user heap ranged from 2,493,664 to 2,530,136 bytes and kernel heap from
58,544 to 63,920 bytes. First/last steady heap samples match; that alone is not
proof of leak freedom. The observed stack high-water mark was 53,040 of 130,840
usable bytes, leaving 77,800 bytes. Both detection runs returned to 22,368 user
heap bytes and 53,208 kernel heap bytes in repeated post-shutdown samples.

The recovery run deliberately stalled a reader for ten seconds, reconnected the
client every thirty seconds and forced a Wi-Fi reconnect at forty seconds. It
retired the stalled connection and rejoined Wi-Fi; the largest receiver gap was
5.203 s. Its aggregate FPS includes those interruptions. Streaming and detection
continued with zero inference failures and camera discards.

The continuous camera supervisor and authenticated ingest were restored with
this image. A separate verified-TLS gateway receiver decoded 88 physical-camera
frames over 5.001 s (17.6 FPS), then closed its temporary playback session.

The public [numeric evidence](../tests/detection/results/2026-10-22-pico/) retains
the control matrix, full native score/stage TSVs, host/native equivalence and
the rejected saturated streaming configuration, production endurance/recovery
records and playback verification. No private camera images,
credentials, network endpoints or local filesystem paths are included.

## Reproduction

```sh
python3 upstream/esp-dl/prepare.py
NXRS_PLATFORM=linux cargo test --locked -p nxrs-detector --features probe,person-model
bash tests/nuttx-std/stream/build.sh --detector pico --probe
python3 tests/detection/run_device.py --port /dev/cu.CAMERA \
  --expected-serial CAMERA_SERIAL --dataset /private/tmp/prepared \
  --scores /private/tmp/native-scores.tsv --report /private/tmp/native-report.json --profile
python3 tests/detection/report_profile.py --report /private/tmp/native-report.json \
  --reference-scores /private/tmp/reference-scores.tsv --out /private/tmp/native-stages.json
bash tests/nuttx-std/stream/build.sh --detector pico --probe
python3 tests/nuttx-std/stream/run.py --port /dev/cu.CAMERA --host-ip HOST_IPV4 \
  --wifi-file /private/tmp/wifi --seconds 60 --reconnect-at 0 \
  --client-reconnect-every 0 --sample-ms 500 --require-detection \
  --output /private/tmp/frame.jpg --report /private/tmp/stream-report.json
python3 tests/detection/report_profile.py --report /private/tmp/stream-report.json \
  --out /private/tmp/stream-stages.json
```

Use new private paths and the existing verified-device/backup procedures.
Production builds use `pico-detection`. Static-image accuracy and bounded local
performance checks do not establish night/IR reliability, false alerts per hour,
multi-client/RF-load performance or multi-day security-camera reliability.
