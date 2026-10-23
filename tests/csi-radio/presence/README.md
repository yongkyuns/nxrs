# Full Espressif presence reference (IDF 6.0 only)

This isolated experiment runs the **complete, unchanged** vendor
`esp-radar` 0.3.4 + `esp_wifi_sensing` 0.1.1~3 sources and the pinned IDF 6.0
ESP32-S3 gain 0.1.5 / motion 0.1.1 binaries. It exercises CSI filtering,
gain compensation, amplitude preprocessing, signal windows, numerical motion
features, template/background training, and the vendor presence/motion FSM.
It is not the production NuttX implementation or a Rust port of the vendor FSM.
No vendor source is copied into Nxrs. ZIP digests are checked before extracting
allowlisted files into ignored build output. Their C sources remain unchanged;
local CMake registration replaces component-manager packaging helpers.

Rust owns this experiment's station, private credential handoff, bounded run,
training commands, diagnostic reporting and error handling. `main/sdk.c` only
translates SDK defaults and diagnostic fields. The 51 shared SDK/libc ABI checks
and five diagnostic-layout checks must agree in the compiled ELF and at boot.

Build with the [IDF reference prerequisites](../idf60/README.md):

```sh
python tests/csi-radio/presence/build.py \
  --idf /path/to/pinned/esp-idf-v6.0 \
  --tools /path/to/isolated/idf-tools \
  --python /path/to/idf6.0/python \
  --rust-sysroot /path/to/qualified/xtensa-rust \
  --gain-package /private/path/esp_csi_gain_ctrl-0.1.5.zip \
  --motion-package /private/path/esp_radar_motion_dec-0.1.1.zip \
  --radar-package /private/path/esp-radar-0.3.4.zip \
  --sensing-package /private/path/esp_wifi_sensing-0.1.1-3.zip \
  --out target/csi-radio-presence-reference
```

The builder never discovers, opens, resets or flashes a board. Its receipt
includes all fixture inputs, vendor source/archive hashes, resolved SDK config,
compiler/ELF/image hashes, and both ABI witnesses. Credentials are runtime-only,
kept in RAM and cleared from the Rust handoff buffer; they are never build inputs.

## Radio and resource scope

The sole channel is the connected AP. Standard vendor CSI settings enable
promiscuous reception, all supported LTFs, unmerged/unfiltered CSI and automatic
gain compensation. Receiver gain is **not forced**. The vendor's default router
ping stimulus is 100 Hz; this is distinct from achieved RF/decoder cadence.
No AP, router channel, radio transmit power, ESP-NOW or MAC configuration is
changed. There is no camera or video workload in this firmware.

In addition to IDF network tasks and its application task, the upstream library
owns its sensing FSM task (4096-byte stack, priority 5), its CSI processing and
combine tasks (3072-byte stacks each, priority 24 in this SDK), and the SDK ping task. These are reference-library internals,
not a proposed Nxrs ownership model. CPU load, high-water stacks, callback drops,
RF freshness and camera coexistence are **not qualified** by this experiment.

The upstream task/callback retirement path has not been qualified. This fixture
therefore deliberately retains the single engine and station until a whole-chip
reset. It never frees them based on an assumed callback barrier. Completion
emits `resources=retained_until_reset`, not a clean-shutdown claim. An authorized
maintenance owner must reset/restore the original firmware even after failure,
then verify fresh real-camera frames. Use a spare otherwise.

## Host protocol and labeled trial

`run.py` exposes `Client(console)` for the single serial owner. `start(ssid,
password, seconds, amplitude="log")` accepts bytes or strings, verifies the exact duration,
engine and `force=0` acknowledgement, then sends a bounded private credential
frame. `amplitude="linear"` selects the public upstream amplitude-log option
before adding the AP peer; `log` retains its default. The exact case is
acknowledged before credentials. Duration is 30–600 seconds. `command()` accepts only `TRAIN_START`,
`TRAIN_STOP` and `END`; `read(deadline)` returns validated diagnostics or training
acknowledgements. It has no flashing/reset/discovery operations. Use
`IDF.RADIO.private_capture(path)` for a new mode-0600 capture outside Git.

For an accuracy trial, first agree on placement and human ground truth. Confirm
that the room is empty before `TRAIN_START`; stop training after a stable
background interval and check that training really produced templates,
background points and valid thresholds. Then record timed phases: empty room,
entry/walking, seated still, small motion, exit and empty room again. Include
actual human labels alongside the diagnostic clock; never infer labels from
classifier output. Keep tuning fixed for the initial test. Report latency,
false triggers, missed presence and still-person behavior separately.

`unknown` covers training, missing calibration and insufficient presence history.
`not_detected` is a classifier hint, **not proof of an empty room**. Output is
always advisory: the vendor public diagnostic snapshot does not expose RF sample
freshness, so a newly printed report is not evidence of fresh RF. Repeated report
clocks, nonfinite values, malformed diagnostics and unavailable prerequisites
are rejected. The host owner must discard observations after `END`, EOF, host failure or restoration.

An unlabelled smoke run may exercise training and classifier execution, but its
baseline is not confirmed empty and it cannot establish human detection accuracy.
Do not promote its positive-report count into a count of detected people.

```sh
python -m unittest discover -s tests/host -p 'test_csi_*.py'
cargo test --locked --manifest-path tests/csi-radio/presence/Cargo.toml --lib
```

Host tests cover protocol safety and shared queue/transport/lifecycle regressions;
they do not establish RF, presence accuracy, NuttX compatibility or safe upstream
resource retirement.

The instrumented reference also exports `PRESENCE_PROBE` snapshots. Test-only
linker witnesses count nonfinite numerical inputs/outputs, native failures and
constant template/current vectors. They do not allocate or print inside the
native call, change thresholds, replace results or change vendor sources.
These counters diagnose execution; they do not establish RF freshness or
human accuracy. A failed/invalid numerical window is not valid absence evidence.

## Measured limits

The October 23 unlabelled trials are recorded in
[the sanitized receipt](../results/esp32s3-full-presence-2026-10-23.json).
The first default/log run could not calibrate. An instrumented log run
calibrated but exposed 642 nonfinite numerical windows. The supported linear
option removed observed nonfinite windows in a 2,715-window run, but two native
calls returned errors; that witness counted failures without recording their
exact status codes. They first appeared in reports around 86.5 and 110.1 seconds.
The upstream detection task skips a failed call without supplying a new result,
while the sensing FSM may retain older state. All presence reports remain
unqualified; failed/stale inference must become unavailable in a future Nxrs
provider. Diagnose these errors and collect human ground truth before making
accuracy claims or integrating product behavior.

The [supervised room trial](../results/esp32s3-supervised-presence-2026-10-23.json)
also failed room occupancy discrimination: every retained report was a presence
hint in both confirmed-empty phases, while walking and while seated. The updated
numerical snapshots preserve signed native error status and the last failing
input dimensions. Captured failures were `ESP_FAIL` (-1); the later offline
[investigation](../../../docs/csi-presence.md#native-numerical-failure-investigation-2026-10-23)
traces that status to the vendor eigensolver iteration limit and reproduces a
finite-input rounding cycle in QEMU. Exact failing RF matrices were not saved. Transition intervals are excluded, report samples are correlated,
and the RF sensing zone was not independently mapped. The upstream output must
remain advisory until numerical failures, freshness and calibration/features
are qualified against labeled cases.

## Reproduce the offline policy investigation

```sh
python tests/csi-radio/presence/policy.py \
  --radar-package /private/path/esp-radar-0.3.4.zip \
  --sensing-package /private/path/esp_wifi_sensing-0.1.1-3.zip \
  --out target/csi-radio-presence-policy
python tests/csi-radio/presence/analyze.py \
  --capture /private/path/completed-supervised-capture \
  --reference-build target/csi-radio-presence-supervised \
  --policy-result target/csi-radio-presence-policy/policy-result.json \
  --out target/csi-radio-presence-analysis
```

Both commands are offline and require a fresh output directory. The policy
witness compiles eight selected unchanged vendor function bodies with `cc`,
SHA-checking the packages first. Its structures and allocation/logging stubs
are host test inputs, not SDK layouts or a complete FSM port. It checks a
stationary-background counterexample, sensitivity bounds, conditional training
statistics, ten-slot template replacement and one-sided history trimming.
The positive run requires 30 assertions plus typed result markers; a separate
deliberate failure must exit nonzero without success evidence.

The analyzer requires the original private `presence-diagnostics.raw`,
`camera-maintenance-result.json` and `instruction-markers.jsonl`. It validates
clocks, prerequisites, advisory status, completed calibration, image/config and
vendor-source provenance, settings and diagnostic decision consistency. It
applies the same transition exclusions as the supervised trial and emits only
aggregates and hashes, accounting for printed-value precision. The source data
stays private; the output cannot replay full-rate feature history or prove RF
freshness. Do not use its same-capture threshold comparisons as tuning or
independent accuracy results.

The [measured analysis](../results/esp32s3-presence-calibration-analysis-2026-10-23.json)
shows that every retained empty-room average exceeds even the least-sensitive
supported threshold, and empty/occupied averages overlap. A threshold that
rejects all recorded empty averages would retain only 2/67 walking reports.
This rules out a simple sensitivity adjustment for this capture; the cause
of post-training feature drift still requires bounded private input/template
captures. The [investigation and next experiment](../../../docs/csi-presence.md#calibration-and-false-presence-investigation-2026-10-23)
preserve that distinction. No hardware or production behavior changed.

## Bounded private frozen-input capture

The optional `PRESENCE_CAPTURE` experiment supplies owned copies of the motion
matrix and wander template/current vectors to the unchanged vendor routines.
This is a distinct input-ownership case: the original `PRESENCE` command keeps
its original pointer/algorithm behavior and does not export private arrays.
Both cases retain the same vendor classifier and sensitivity. The host must
receive the exact `snapshots=1 frozen_inputs=1` acknowledgement before sending
credentials; use `Client.start(..., snapshots=True)` explicitly.

`Client.arm_snapshot(tag)` admits one pending successful-window sample, for
strictly increasing tags 1 through 16. The recorder also retains the first
failed motion call once per run. Each snapshot carries the exact split matrix,
successful output (never read on failure), up to ten template/current
comparisons, native wander results and a numerical-call sequence/time. A
successful window is finalized by the next motion call on the same SDK task,
so its following wander calls belong to that window. Wrong task, pointer,
shape, frame contents, overflow or contention invalidate capture qualification;
they never change a native return value. Source RF timestamps are not exported.

The last observed gain compensation tuple is sampled at numerical admission.
It is **not** per-row RF metadata, and a numerical clock does not prove fresh
RF. Calibration details expose the SDK's training action/status, background
mean, latest wander basis and learned baseline. These are periodic diagnostic
snapshots, not an exhaustive training-event trace.

The prepared image reserves 21,128 bytes for the recorder even in ordinary
mode; earlier images' timing/resource measurements do not transfer to it.
Storage is fixed: three bounded records (working, successful sample and first
failure), each with at most 1,024 matrix words, 64 output words and ten 64-word
templates. Mutexes are initialized on the control task before capture admission;
SDK workers only use `try_lock`, without waiting, printing or adding allocation
inside the numerical wrappers. The control task drains private arrays in bounded
hex fragments. FNV-1a checks the metadata/payload for transfer corruption; this
is an integrity check, not authentication. Reader failure retains its slot.
Raw arrays and credentials must stay outside Git in a mode-0600 private capture.
`snapshots.py` rejects incomplete, duplicate, reordered or corrupted records and
provides independent double-precision correlation summaries without exporting
the arrays. The shared synthetic wire fixture is checked by both Rust and Python.

Use only a spare or the previously authorized camera maintenance procedure:
verify the **current** supervisor and firmware, back up all 16 MB before flashing,
and restore/verify that backup plus fresh real frames on every exit path. An
older CSI backup must not replace a newer camera firmware. A new empty-room
confirmation and separate empty holdout are required; a failed holdout does
not proceed automatically to walking/seated phases. The engine still retains
resources until reset. Host tests and a successful 51+10 compiled ABI build
do not qualify RF, instrumented cadence/stack high-water or presence accuracy.
