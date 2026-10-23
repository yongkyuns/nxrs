# Experimental Wi-Fi CSI presence sensing

Espressif provides an official
[Wi-Fi sensing demo](https://github.com/espressif/esp-csi/tree/8633d67152db2808f141cc1595970aa9cf406045/examples/esp-radar/wifi_sensing_demo)
using `esp_wifi_sensing` and `esp-radar`. It supports ESP32-S3 and both motion
and presence hints. An ESP32 and a compatible normal Wi-Fi router can provide
the sensing link; a dedicated ESP transmitter is another arrangement. Detection
depends on placement, environment, traffic, and calibration. It does not identify
people, count occupants, or reliably establish that a home is empty.

The vendor example requires ESP-IDF >=5.4. It is **not a NuttX application**.
Our checked-in NuttX ESP32-S3 Wi-Fi adapter and Nxrs station HAL currently expose
no CSI sample API. Do not link the vendor FreeRTOS sensing FSM into that provider
or claim that a host test qualifies its native ABI.

## What is implemented

`service/presence` is a reusable, dependency-free, allocation-free Rust policy
component. A caller feeds typed classifier hints and polls its deadline from
the caller's existing event loop. It adds no worker. Its responsibilities are:

- Require calibration/readiness before accepting hints.
- Confirm both a minimum number of reports and a minimum elapsed interval.
- Hold a detected hint briefly to reduce flicker, while letting lost input
  invalidate it immediately.
- Reject duplicate/reversed source timestamps without refreshing the deadline.
- Invalidate old evidence on explicit source restart or re-calibration.
- Distinguish `Unknown`, `NotDetected`, motion evidence, and presence evidence.

This component consumes classifier output; it does not implement Espressif's RF
feature extraction or a new presence-detection algorithm.

`tests/csi-presence` connects the official demo's `HMS:` serial diagnostics to
this Rust component. It explicitly selects one peer MAC, bounds input to 2048
bytes per complete line, and emits experimental JSON transitions. Extra demo
channels cannot contaminate the selected channel. One bounded stdin reader
allows the owner to expire its deadline even if stdin remains open and silent.
It opens **no USB device or network listener**, sends no firmware commands,
and does not change the camera, gateway, enrollment, iPhone UI, or recording
policy. Vendor C is not copied into Nxrs. The exact inspected upstream revision
is in `tests/csi-presence/upstream.json`.

## Important diagnostic limitation

The demo's `ts_ms` is the time it **prints a diagnostic report**. Its public
serial message does not include the last CSI packet timestamp, an RF packet
counter, or a reconnect generation. Repeated cached classifier values can be
printed with new report timestamps. Consequently this bridge can detect loss
of the **report stream**, but cannot certify RF-sample freshness or a healthy
Wi-Fi sensing link. Every output declares `advisory_only: true` and
`rf_sample_freshness: "not_exported_by_demo"`.

`not_detected` means a calibrated classifier reported no detection; it must
never be translated into “room empty.” Neither replay nor fresh serial reports
are physical presence qualification. No automatic recording, absence decision,
notification or product “Live” status uses this experimental path.

## Run without hardware

```sh
cargo test --locked -p nxrs-presence-service
cargo test --locked --manifest-path tests/csi-presence/Cargo.toml
cargo run --locked --manifest-path tests/csi-presence/Cargo.toml -- \
  --peer 02:00:00:00:00:01 --mode replay \
  < tests/csi-presence/fixtures/lifecycle.log
```

The fixture is **hand-authored protocol test data**, not a recording from the
home or evidence of detection accuracy. Replay uses the source report clock
to exercise timing without sleeping. EOF always invalidates presence. Observer
mode instead uses the Mac's monotonic clock and expires after two seconds of
report silence; both modes are explicitly labeled in output.

## Physical trial on a spare board

1. Use a spare ESP32-S3 (or another supported Wi-Fi ESP32) and the pinned vendor
   example. Keep the working camera's firmware and USB supervisor unchanged.
   Provision Wi-Fi privately using the vendor tooling; do not commit credentials.
2. Build the example with web-serial diagnostics enabled. Start its stream and
   perform empty-room training with the vendor console. The relevant commands
   are `HMSCMD HELLO`, `HMSCMD START_STREAM`, `HMSCMD TRAIN_START AP`, and
   `HMSCMD TRAIN_STOP AP`. Follow the component's calibration guidance; do not
   treat a successful command acknowledgement as a completed calibration.
3. Have the board's **single serial owner** provide a capture/pipe to stdin.
   Do not open the camera's serial port or let two tools own the same board.
   Select the AP BSSID from that private capture rather than assuming the
   example's extra hard-coded peer addresses are real transmitters.
4. Feed that selected stream with `--peer <actual-peer-MAC> --mode observer`.
   This is read-only evaluation. Record ground truth separately: empty room,
   walking, seated still, small movement, fans/curtains/pets, room next door,
   router power loss and recovery, changing traffic and camera streaming load.
5. Measure false triggers, missed presence, report and RF cadence, detection
   latency, disconnect recovery, CPU/memory and any effect on video throughput.
   No accuracy claims are justified by the protocol regression tests.

## Pinned NuttX radio qualification branch

The separate [CSI radio fixture](../tests/csi-radio/README.md) builds a dedicated
qualification image for a spare or an explicitly authorized camera maintenance
window with complete firmware backup/restoration. Rust owns capture bounds, callback generations, RF timestamp
validation, gain baseline/compensation sequencing and cleanup. The test-local C
witness only translates the pinned SDK headers and calls SDK functions. The
production Wi-Fi HAL, camera profiles, gateway and iOS client remain unchanged.

The kernel-only QEMU witness executes the unchanged IDF 6.0 ESP32-S3 gain and
motion archives against the pinned IDF 6.1-derived SDK. Three fresh boots passed
818 checks each, including all four motion allocation-failure sites, actual SDK
logging, host numerical oracles and two concurrent numerical workers. A
separate deliberate-failure boot failed as required. PHY gain calls in that
witness are recording stubs; synthetic data proves neither RF metadata semantics
nor presence accuracy. Pins, reproduction commands and evidence are in the
fixture; no vendor archives, credentials or private RF captures are committed.

The physical fixture collects raw CSI from one explicit AP BSSID and exchanges
bounded UDP echoes or an explicitly selected TCP control while the existing NuttX station owner keeps checking link
health. Capture does not enable promiscuous mode or force receiver gain. A
separate explicit gain-forcing trial restores automatic gain before stopping
CSI. Real RF field interpretation, actual PHY effects, reference comparisons,
detection accuracy and camera coexistence remain pending. A capture PASS labels
those qualifications as unverified rather than promoting the experiment into a
product feature.

The 2026-10-22 controls isolated and fixed console-command truncation (a 64-byte
shell line and USB receive ring). With checked shell capacity, paced input and
target option/port acknowledgment, UDP transport passed both without CSI
initialization (294 echoes) and with CSI initialized but capture disabled
(283 echoes). Real capture then exported 98 CSI records before its two-second
RF freshness deadline failed while UDP continued. The original firmware and
WebRTC playback were restored and verified. This is partial radio evidence;
continuous CSI, private gain semantics and presence detection remain unqualified.
See [sanitized control evidence](../tests/csi-radio/results/esp32s3-transport-controls-2026-10-22.json).

Follow-up diagnostics recorded 299 matching HT callbacks, no callback drops and
an unchanged AP; a pending UDP request coincided with the RF expiry. A separate
UDP-only control also failed. The single-flight fixture could stop stimulating
CSI after a lost echo. A bounded 32-entry echo window now keeps sending at 100 ms
intervals while preserving both two-second deadlines and zero-loss acceptance.
This is a test-fixture correction, not a demonstrated radio or presence fix.
An SPSC handoff also removes a callback/consumer mutex collision seen in the
first bounded-flight trial. With both changes, 208 frames and 159 gain
compensations were accepted with no callback drops before an overdue UDP echo
failed the trial at 22.070 seconds. CSI was still fresh (50 ms) at that failure,
so loss of the echo did not imply loss of CSI delivery. The requested 60-second
qualification did not pass; all four follow-up windows restored original
firmware and decoded real video. No presence decisions were produced.
See [the retained diagnostic failures](../tests/csi-radio/results/esp32s3-callback-diagnostics-2026-10-22.json).

A separate buffered TCP control then passed 300 echoes in 30 seconds. Following
60- and 120-second CSI sessions passed with 1084 and 2118 accepted frames, 1035
and 2069 valid gain-compensation calls, every echo returned, and zero callback
drops. Both completed their requested native observation windows and confirmed
peer EOF, CSI retirement, station disconnect, worker join and zero NSH status.
The test image explicitly enables bounded TCP write buffers because the pinned
unbuffered sender can wait despite nonblocking mode; producer/consumer ownership
and both two-second deadlines are unchanged. All three TCP maintenance windows
restored original firmware and verified real WebRTC video.

This establishes bounded TCP-stimulated capture and same-boot re-acquisition on
the pinned stack. Earlier failed/cancelled attempts are retained, and TCP success
does not qualify UDP delivery. Independent boots, longer/adverse-network runs,
private gain semantics/reference comparison, coexistence and the complete
presence pipeline remain open. No presence or occupancy decisions were produced.
See [the TCP control evidence](../tests/csi-radio/results/esp32s3-tcp-controls-2026-10-22.json).

## Native Nxrs integration gate

Once the separate-board experiment establishes useful results:

1. Run the separate-board fixture against the exact pinned Wi-Fi/PHY blobs.
   Static/link/QEMU checks establish callable numerical kernels, while real CSI
   continuity, private gain metadata and PHY effects still need physical evidence.
   CSI is disabled at Wi-Fi initialization in the normal camera build. Do not
   replace the camera firmware as part of an unattended monitoring qualification.
2. Put acquisition in `hal/wifi/src/platform/nuttx/` with a typed capability in
   the owning Wi-Fi crate, explicit unsupported capability reporting and shared
   ownership of the radio. Keep target-header layout translation in that
   provider's `ffi/`; callback copying must be bounded and return promptly.
3. Keep resource lifetime, overflow counters, validation, source epochs,
   calibration policy, debounce, loss and recovery in Rust. Prove callback
   quiescence before releasing callback context or buffers. Do not import the
   example's C/FreeRTOS product state machine into a native bridge.
4. Carry actual RF frame progress and channel/configuration identity to the
   classifier. Missing/stalled samples, queue overflow, changed channel/BSSID
   and invalid calibration must produce `Unknown`, even if reports continue.
   CSI/RSSI measurements are not authenticated human-presence evidence.
5. Expose optional authenticated monitoring data to the gateway/iOS only after
   RF freshness and placement qualification. Keep CSI evidence separate from
   camera motion/person detection; do not silently suppress existing recordings.
   Physical camera coexistence and sustained resource tests must pass before
   enabling it in a home-monitoring profile.

Further hardware qualification and the production CSI provider are pending. Host tests
qualify policy, protocol isolation, bounds, rollback and failure behavior. The
separate kernel QEMU evidence qualifies only its explicitly exercised runtime
paths.

References: [ESP-CSI overview](https://github.com/espressif/esp-csi),
[Espressif sensing component](https://components.espressif.com/components/espressif/esp_wifi_sensing),
[HAL architecture](hal-platform-architecture.md),
[NuttX device access](nuttx-device-access.md).

## Complete IDF reference experiment, 2026-10-23

The [full reference fixture](../tests/csi-radio/presence/README.md) now runs
unchanged esp-radar 0.3.4 and esp_wifi_sensing 0.1.1~3 on the native IDF 6.0
ESP32-S3 reference, including preprocessing, native numerical kernels, empty-room
training APIs and presence/motion classification. This is separate from a future
Rust/NuttX implementation; the production HAL and client remain unchanged.

The first **unlabelled** logarithmic-amplitude run could not finish calibration:
one template, zero background points, `ESP_ERR_INVALID_STATE`. A subsequent
instrumented run did calibrate and emitted presence hints, but 642 of 2,685 native
numerical windows contained nonfinite inputs/outputs. The unchanged vendor
preprocessing calls `log10f(amplitude)` without guarding zero. This is a plausible
source-level explanation, not a measured attribution of each invalid sample.

Selecting the vendor's supported **linear-amplitude** option before admitting the
AP peer removed observed nonfinite inputs/outputs in a third run: 2,715 windows,
zero nonfinite inputs/outputs and no uninspected calls. It collected 45 templates
and 1,129 background points, completed calibration, and emitted 88 presence-hint
reports. **Two native numerical calls still returned errors**, first observed
around 86.5 and 110.1 seconds; their exact status codes were not retained by this
witness. They remain an execution/reliability gate. No gain forcing, classifier
threshold adjustment or substituted numerical output was used.

No person supplied occupancy labels, and no empty-room baseline was confirmed.
These positive reports are **not proof that someone was detected** or a count
of people. The reference snapshot also lacks RF freshness, and the upstream
consumer can skip failed numerical calls while retaining old classifier state.
Do not deliver this as a product presence/absence decision. The next test needs
a supervised empty-room baseline, entry/movement, seated stillness and exit,
with failure/staleness handled as unavailable and native errors diagnosed.

Every maintenance window restores the original full flash and verifies real
WebRTC frames. The two early windows initially received 404 while their publisher
reconnected; subsequent fresh real-frame verification succeeded. Those delays
and the unsuccessful calibration are retained, not promoted into clean results.
See the sanitized full-reference receipt under `tests/csi-radio/results/`.

## Supervised room trial, 2026-10-23

The user confirmed an empty room, returned and walked, sat still, and left again.
The same complete IDF reference ran with linear amplitude and unchanged vendor
classifier sensitivity/threshold rules. Empty-room training completed with
44 collected templates and 1,374 background points. The first waiting window
had expired safely before the user confirmed leaving; it was restored and is
not counted as an occupancy test. A fresh supervised window then completed.

| User-confirmed phase | Retained reports | Presence hints | Not detected |
| --- | ---: | ---: | ---: |
| Empty before entry | 72 | 72 | 0 |
| Walking inside | 67 | 67 | 0 |
| Seated still | 85 | 85 | 0 |
| Empty after exit | 56 | 56 | 0 |

**This setup did not distinguish an empty room from an occupied one.** These
are correlated diagnostic reports, not independent accuracy trials. Unconfirmed
transitions were excluded; retained samples start two seconds after confirmation
and end one second before the next movement instruction. Ground truth is the
user's phase report, not an independent video measurement or a mapped RF zone.

Across the full supervised window, 10,180 numerical calls had zero nonfinite
inputs/outputs and 132 native failures. The new diagnostic snapshots retained
`ESP_FAIL` (-1) and input shapes (13 columns with varying row splits). This
identified the observed SDK status before the offline investigation below;
snapshots retain the latest failure and do not log every individual error. Sparse active-motion
reports appeared while walking and in the empty phases. The reference's
failure/freshness behavior and presence output remain unsuitable for product
occupancy decisions. No thresholds were tuned to make the labeled trial pass.

Remaining engineering steps include capturing a bounded failing RF-derived
numerical input, diagnosing calibration-feature behavior, and repeating held-out
labeled trials with a defined sensing zone before proceeding
with a Rust/NuttX provider. The original camera firmware and real playback are
restored after the supervised window. Exact source/build and aggregate phase
evidence is in
[`esp32s3-supervised-presence-2026-10-23.json`](../tests/csi-radio/results/esp32s3-supervised-presence-2026-10-23.json).


## Native numerical failure investigation, 2026-10-23

Here “kernel” means the vendor's numerical routine, not the operating-system
kernel. The supervised failures did not indicate an OS crash. Disassembly of
the pinned `esp_radar_motion_dec` 0.1.1 IDF 6.0 ESP32-S3 archive identifies the
`ESP_FAIL` (-1) branch in `matrix_power_method_eigen`: reaching the fixed
30-iteration budget. `esp_radar_motion_dec_compute` passes that status back
without writing the output projection. Allocation failures instead return
`ESP_ERR_NO_MEM` (257), as the separate four-site fault tests verify.

The routine forms an uncentered Gram matrix and uses mixed float32/double power
iteration. Its stopping test uses an absolute tolerance with float32 bits
`0x38d1b717` (approximately 0.0001). A deterministic, finite, positive 10x13
synthetic matrix (LCG seed 785, entries 10 through 120) exposes a precision-sensitive
failure. The host model alternates between estimates 3868.360397338867 and
3868.3602752685547, a difference of 0.0001220703125, above that tolerance. The
cycle persists with a 300-iteration model budget. This is a model explanation;
the native SDK log exposes iteration counts, not its internal estimate trace.

The unchanged archive reproduces the failure in NuttX ESP32-S3 QEMU. Three fresh
boots each exercise four row splits (10+0, 7+3, 3+7, 1+9), repeated three times.
Every unscaled call returns -1 at iteration 30; all four allocations succeed,
cleanup completes, guards remain intact and output sentinels remain unchanged.
Scaling the same data by 1/16 preserves its independent double-reference
eigenvector direction; every scaled native call succeeds in five iterations
and its projection matches the reference within 0.00002. A separate positive
seed-1 control succeeds unscaled. Each numerical boot passes 398 assertions;
three fresh 818-assertion legacy boots and the deliberate-failure control also
pass. This demonstrates numerical robustness limitations without changing the
vendor code, HAL, classifier, thresholds or camera firmware.

The original failing RF matrices were not retained. The reproduction explains
the captured -1 return path and a concrete way valid data reaches it; it does
not replay or attribute each of the 132 physical failures. Successful feature
progress with no new errors also occurred during both confirmed-empty windows
(60 and 41 retained diagnostic reports, all still presence hints). These counts
use the same transition exclusions as the supervised trial and consecutive
probe intervals with advancing window/wander counters and unchanged error
counters. They do not establish RF freshness or independent accuracy. Numerical
failures alone therefore do not establish the cause of always-positive output.

The [sanitized numerical evidence](../tests/csi-radio/results/esp32s3-numerical-convergence-2026-10-23.json)
records archive/build provenance, native results and host references. Reproduce
with the kernel build/QEMU instructions in `tests/csi-radio/README.md`; the runner
now includes `csikernel numerics`. No hardware was accessed for this investigation.
A future fix needs numerical equivalence, scale/adverse-input tests and labeled
RF requalification; increasing the iteration budget alone does not resolve the
demonstrated cycle. Failed or stale inference must remain unavailable in any
future product provider.

## Calibration and false-presence investigation, 2026-10-23

Offline analysis of the same supervised capture confirms that the reported
presence matches the vendor decision rule; it is not an inverted boolean in
the Rust diagnostic bridge. The default presence sensitivity is 0.25, which
the vendor maps to gain `1 + 29 * sensitivity = 8.25`. Presence requires the
lower-half trimmed average of 25 wander features, multiplied by this gain, to
exceed the learned baseline. The captured baseline was 0.00088180; the resulting
effective threshold was approximately 0.00010688.

| Confirmed phase | Reports | Minimum average | Median average | Maximum average |
| --- | ---: | ---: | ---: | ---: |
| Empty before entry | 72 | 0.00121394 | 0.03511427 | 0.12802869 |
| Walking inside | 67 | 0.01549575 | 0.05766316 | 0.29046261 |
| Seated still | 85 | 0.18839312 | 0.32121021 | 0.47637957 |
| Empty after exit | 56 | 0.05924261 | 0.205207245 | 0.25230765 |

All retained averages exceed the learned baseline itself. The least-sensitive
supported setting has gain one, so **no supported sensitivity setting would
clear these saved empty-room averages**. The analysis accounts for eight-decimal
diagnostic rounding. This is an offline scalar-rule comparison against saved
averages, not a new full-FSM or RF trial.

The empty and occupied averages also overlap. A hypothetical threshold as high
as the largest empty average (0.25230765), outside the supported sensitivity
range for this calibration, would retain only 2/67 walking and 61/85 seated
positive reports. No single scalar threshold perfectly separates the recorded
phases. These correlated, same-capture counts are descriptive; they are not
held-out accuracy or a recommended threshold.

The new host witness compiles eight unchanged function bodies from SHA-verified
vendor source packages. Its test-local structures, allocator and logging stubs
exercise selected numerical/calibration rules, not SDK layouts, task behavior,
radio or a complete host port of the FSM. Thirty assertions and a deliberate
failure control establish these cases:

- A steady synthetic feature exactly matching its learned background is still
  marked present at the default gain 8.25. An exact binary-fraction input avoids
  rounding ambiguity. The minimum gain clears this exact tie; a feature twice
  the baseline is present even at minimum gain.
- Training accumulates background statistics only for the previous wander
  basis above 0.00001 and at or below 0.002. A larger basis instead collects a
  template and forces the reported training wander to zero. Thus training
  statistics select low values, and template insertion changes the feature
  stream delivered during training. This is a mechanism to investigate, not
  proof that it caused the measured post-training drift.
- The template bank has ten slots. The captured count 44 means collection
  events, including replacements, rather than 44 simultaneously retained
  reference vectors. The presence statistic averages the lowest 13 of 25
  history values, not a symmetric trimmed mean.

The [sanitized analysis and host witness receipt](../tests/csi-radio/results/esp32s3-presence-calibration-analysis-2026-10-23.json)
retain package/function hashes and source-image/config provenance. Existing
phase counts, clocks and transition exclusions match the supervised result.
No classifier, sensitivity, template, numerical library or firmware changed;
this investigation did not access hardware.

### Next physical experiment

Prepare a bounded private capture before another supervised trial. It needs
the actual input matrix and output of a failed motion call, representative
successful calls, the template/current vectors used for wander, template
replacement actions, and calibration/background statistics. Correlate these
with source RF/window progress and gain-compensation state; retain sequence
identity rather than treating diagnostic print times as RF freshness. Storage
and handoff must be bounded, with no logging/allocation in the numerical call
and no wait behind a preempted consumer. Keep RF data, AP identity and runtime
credentials outside Git.

Use fixed placement and a defined AP/receiver sensing zone. After training,
test a separate confirmed-empty holdout while the template bank is frozen,
before asking for walking/seated phases. Preserve a failed holdout rather than
tuning it until it passes. Compare the retained numerical inputs against an
independent double-precision reference and inspect preprocessing/template
changes to separate numerical drift from a changing RF environment. Only a
subsequent held-out labeled trial can qualify a correction. Failed/stale
inference must become unavailable in a future product provider. No additional
firmware maintenance or physical trial was started by this offline step.

## Prepared frozen-input recorder, 2026-10-23

The isolated IDF fixture now implements the bounded capture described above.
Its opt-in `PRESENCE_CAPTURE` case uses fixed owned input storage when calling
the unchanged numerical routines, so a captured matrix/template pair matches
the actual calculation. This changes input ownership for this named experiment;
it must not be presented as a measurement of the original rolling-buffer case.
The ordinary `PRESENCE` mode keeps its original algorithm/input behavior.

One monotonic SDK-task identity binds motion and subsequent wander calls; raw
task addresses are not exported. A sequence number and numerical-call timestamp
identify the record. Original output address and exact current-vector bits must
match before associating template comparisons. The next motion call finalizes
a successful window; a failed call is retained immediately without reading
uninitialized output. A separate slot keeps the first failure while up to 16
explicitly tagged successful samples can be requested. Static storage and
nonblocking transfers bound overload; mutex initialization occurs on the
control task before admission because ESP-IDF initializes pthread mutexes lazily.

The expanded mechanical SDK diagnostic bridge reports background mean, training
action/status and latest wander basis. All ten compiled C/Rust layout values
must match, alongside the existing 51 SDK ABI witnesses. Periodic gain and
calibration diagnostics do not establish per-row RF metadata or source freshness.
Array payloads remain private; public evidence contains only hashes/aggregates.
See the [reference reproduction and protocol](../tests/csi-radio/presence/README.md#bounded-private-frozen-input-capture).

Host qualification covers paused readers, contention fallback, wrong-task/frame
rejection, bounded admission, failed-output safety, failed drains, exact-bit
Rust/Python wire agreement, corrupt/incomplete transfer rejection, and credential
withholding on a mismatched experiment acknowledgement. No production HAL or
classifier policy is changed. Physical timing, stack high-water, RF freshness,
and labeled accuracy still require the next controlled maintenance window.

## Physical frozen-input empty holdout, 2026-10-23

After a new human empty-room confirmation, the prepared image ran the distinct
owned-input experiment: approximately ten seconds settling, sixty seconds
empty-room training and a separate fifty-second empty holdout. The holdout
failed: **88/88 retained reports were presence hints**. Walking/seated phases
were not requested after this failure. These are correlated diagnostics from
one user-labeled room condition; the RF sensing zone was not independently
measured. The classifier and sensitivity were unchanged.

The run retained five successful exact-bit input/output snapshots: one settling,
one training and three holdout windows. Capture recorded zero skips, foreign
calls or invalid records. The final probe covered 3,337 motion windows and
23,421 wander calls with zero native errors, nonfinite inputs/outputs or
uninspected windows. No failing physical matrix was collected in this run;
the prior failure observations and synthetic QEMU reproduction remain separate.

For the five captured windows, native projections agree with an independent
double reference within 0.00000185, and paired native wander values agree with
double Pearson-distance calculations within 0.000000188. The host float32
policy model converges in four iterations for each saved matrix; this is host
analysis, not a new native replay. The three sampled holdout template banks
are bit-identical. Their minimum native template distances nevertheless grow
from 0.00130826 through 0.00387335 to 0.12375331. The learned baseline is
0.00084859 and the reported effective threshold 0.00010286; all retained
holdout averages (0.00058474 through 0.19946946) exceed that threshold.

This narrows the investigation: the sampled false-presence features are
reproducible calculations, and sampled post-training template replacement does
not explain their drift. It does not attribute that drift to RF, preprocessing,
gain or input ownership. Observed amplitudes were smaller than the synthetic
failure case; there was no matched rolling-buffer/owned-input A/B comparison.
Zero errors here therefore do not establish that freezing inputs fixes the
earlier numerical failures. RF source freshness, instrumented throughput and
worker stack high-water remain unqualified.

The gain observer has a concrete blind spot: all saved getter sequences are
zero, meaning **unavailable**, not zero physical gain. The full radar calls
`esp_csi_gain_ctrl_compensate_rx_gain`, whose internal getter call within its
archive object bypasses the fixture's getter wrapper. Vendor defaults enable
compensation, but this trial did not observe its execution state. The next
trace must observe the actual compensation entry and correlate source RF
sequences, preprocessing/windows and training actions before assigning a cause.
Do not tune the threshold against this failed holdout.

The current camera's full 16 MB firmware was newly backed up, restored and
hash-verified. Playback initially failed because the existing HTTPS leaf
expired during maintenance. Renewing that leaf under the existing trusted CA,
preserving its key and endpoint identities, and reloading the TLS listeners
restored control HTTPS and three fresh real 320x240 WHEP frames with certificate
validation enabled. The camera supervisor resumed telemetry; enrollment and
iPhone configuration were preserved. This private leaf expires 2026-10-24
15:13 UTC, shortly before its existing trust root; long-term certificate
lifecycle remains separate work.

The [sanitized physical evidence](../tests/csi-radio/results/esp32s3-frozen-input-empty-holdout-2026-10-23.json)
retains source/image provenance, phase counts, numerical/reference aggregates,
gain-observation limitations and restoration evidence. Raw matrices, template
vectors, credentials and flash backups stay outside Git. Presence accuracy
remains unqualified; no production HAL or NuttX full pipeline was changed.

Validation for this evidence update: 70 CSI host tests, 33 Rust reference tests,
15 architecture and eight HAL-boundary regressions, plus the 41-package audit
pass. Source/image hashes still match the physical build receipt; no fixture
source changed after the measured firmware was built.
