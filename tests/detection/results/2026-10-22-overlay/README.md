# Frame overlays and localized presence correction

This follow-up qualifies the delivery mapping and fixes localized presence policy.
The PICO model, preprocessing, threshold and numerical operators are unchanged;
the preceding 1,000-case accuracy qualification remains applicable. These tests
do not establish a new model accuracy result.

The previous policy used raw-score hysteresis for localized output. Rejected
peaks between 300 and 500 could preserve `Person` indefinitely even with no
accepted boxes. The regression first enters Person, then supplies two empty
observations with raw scores 415 and 364 and requires PersonLeft/Clear. Scores
remain diagnostic; failure and expiry remain unavailable.

`physical-whep.json` records the final 120-second physical ESP32-S3 run: **17.766
decoded FPS**, 160 completed observations (**1.3333/s**), zero inference failures,
zero camera discards and zero additional stream errors. The 32 KiB owner task
retained more than 17 KiB of stack margin. One prior stream error was expected
when the owned ingest process was replaced; the measurement checks unchanged
counters rather than claiming a zero lifetime error count. The live scene had
empty accepted output and stayed Clear. `firmware.json` identifies the flashed
image and its exact MCU source hashes. No firmware binary or camera images are
checked in.

`native-ios.json` records 60 seconds through the actual app peer/decoder on an
owned iOS Simulator: **16.716 decoded FPS**, 910/910 sampled rendered-frame
identities mapped, 80 distinct observations, one epoch and no boxes in the clear
scene. Sampling counts differ from the decoder counter because the polling probe
can coalesce frames. Simulator throughput is reported separately from physical
camera/WHEP throughput and is not a physical-iPhone performance measurement.

`pixel-identity.json` independently checks positive/negative identity. The actual
Rust stream fixture alternates red/person frames with blue/clear frames through
the encoder, in-band SEI, RTSP/RTP, MediaMTX and native SDK. A renderer compares
actual decoded chroma with the matched status/boxes. Both runs pass with **zero
pixel/metadata mismatches**; a source stall retires publication and a restart
occurs between runs. This synthetic fixture's throughput is not a camera
performance benchmark. Native unit tests additionally cover SDK timestamp
normalization, unrelated buffers, alias ambiguity, absent metadata, replay and
late callbacks after close. Shared Rust/Swift fixtures and geometry tests cover
serialization, bounds, expiry, rotation and letterboxing. A slow partial NAL
cannot renew the gateway's complete-frame deadline.

`validation.json` records local checks and source hashes. CI also exercises the
annotated path's generated systemd sandbox, both child processes, forced owner
termination and dependency cleanup on Linux. Those Linux-only checks are not
claimed as local macOS results. The full Simulator app and signed iPhone build
pass; installation/physical phone acceptance is pending reconnection of the
existing target. Existing enrollment, trust files and credentials are unchanged.

Reproduce the isolated pixel reference with the pinned media dependencies:

```sh
python3 gateway/tests/build_camera_source.py --output /private/tmp/overlay-source
python3 gateway/tests/detection_video_reference.py \
  --gateway gateway/target/debug/nxrs-gateway-control \
  --ingest gateway/target/debug/nxrs-camera-ingest \
  --mediamtx /private/path/mediamtx \
  --camera-source /private/tmp/overlay-source/nxrs_camera_source \
  --probe clients/ios/Native/.build/debug/NativePlaybackProbe
```

The reference owns private loopback services and a temporary CA scoped to its
URLSessions. It changes neither system trust nor an existing gateway. Runtime
credentials, logs and temporary media are removed on exit.
