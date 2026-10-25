# Full Espressif presence reference (IDF 6.0 only)

This isolated experiment runs the **complete vendor algorithm** from
`esp-radar` 0.3.4 + `esp_wifi_sensing` 0.1.1~3 and the pinned IDF 6.0
ESP32-S3 gain 0.1.5 / motion 0.1.1 binaries. It exercises CSI filtering,
gain compensation, amplitude preprocessing, signal windows, numerical motion
features, template/background training, and the vendor presence/motion FSM.
It is not the production NuttX implementation or a Rust port of the vendor FSM.
No vendor source is copied into Nxrs. ZIP digests are checked before extracting
allowlisted files into ignored build output. Current builds insert hash-locked
trace taps into staged radar source; sensing source and numerical archives
remain unchanged. Taps are inactive outside the explicit trace case. Local CMake
registration replaces component-manager packaging helpers. Earlier physical
results retain their original, uninstrumented source/image receipts.

Rust owns this experiment's station, private credential handoff, bounded run,
training commands, diagnostic reporting and error handling. `main/sdk.c` only
translates SDK defaults and diagnostic fields. The 51 shared SDK/libc ABI checks
and ten diagnostic-layout plus fourteen trace-layout checks must agree in the
compiled ELF and at boot. The compensation function signature is also checked
against the SDK header; the build rejects RX linkage that bypasses its observer.

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

Current builds observe `esp_csi_gain_ctrl_compensate_rx_gain` itself, with its
SDK ABI checked, instead of the internal getter. The unchanged function is
called once with its original arguments; its status is preserved. Failed/null
outputs are never read and carry an unavailable NaN factor. Sequence zero,
failed status, nonfinite or nonpositive factors are unavailable in host summaries.
The snapshot tuple remains the last observation at numerical admission, **not**
per-row RF metadata. The earlier physical image's getter sequence zero remains
unavailable; rebuilding cannot repair that saved evidence. The separate trace
below associates actual compensation observations with each preprocessing row.
Calibration details expose training action/status, background mean, latest
wander basis and learned baseline. Periodic snapshots are not an exhaustive
training-event trace.

The snapshot recorder reserves 21,128 bytes even in ordinary mode; the current
image additionally reserves the trace storage described below; earlier images' timing/resource measurements do not transfer to it.
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

The first physical frozen-input empty holdout failed: all 88 reports remained
presence hints, with no numerical errors. Five private paired snapshots agreed
with independent double references; the three sampled holdout template banks
were bit-identical while the current feature distance grew. See the
[physical result and limitations](../../../docs/csi-presence.md#physical-frozen-input-empty-holdout-2026-10-23).

## Prepared RF and preprocessing trace

`PRESENCE_TRACE` is a separate **enqueue-owned-input** experiment. Ordinary
`PRESENCE` still uses vendor ring pointers; `PRESENCE_CAPTURE` freezes them at
the numerical call. The trace case instead copies on the single preprocessing
owner when that owner enqueues a window. Explicit tokens travel through the
vendor queue. Reusing ring slots later cannot change the queued trace matrix.
This ownership change must be named in comparisons; it is not a transparent
measurement of the original rolling-buffer case or a demonstrated fix.

The source insertion tool accepts only the SHA-locked radar source. It adds
SDK taps and queue identity, without changing thresholds, selected LTF,
subcarrier stride, training rules, receiver setup or numerical archives.
The C bridge translates SDK fields; Rust owns recording, bounds, identity
checks, admission, handoff and failures. A linked-image check confirms the RF
receiver calls the actual compensation wrapper, rather than its unobserved
in-archive getter. This proves instrumentation linkage, not physical execution.

Startup gain collection is explicit unavailable data. Only the SDK's documented
`ESP_ERR_INVALID_STATE` while its checked state is `RX_GAIN_COLLECT` is admitted
as warmup. Other failures still fault. Such rows retain a NaN factor and zero
availability flag; they cannot receive a successful sample tag or pass the host
trace reader's gain checks. Readiness requires a complete available window.
The reported `gain_unavailable_rows` counts warmup; any new unavailable row
after readiness is a sticky fault. The maintenance owner waits for
`gain_ready=1` before tagging or training and requires that count to stop growing.
This preserves startup behavior without promoting unknown gain into valid data.

The trace observer is scoped to the associated AP, selected on the control task
before admission. Both the SDK dispatch peer and packet source must match.
The pinned radar also creates a legacy wildcard peer; those callbacks continue
through the unchanged SDK path and are counted as `other_peer_frames`, without
entering the AP trace or consuming its snapshot tags. This is observation scope,
not a radio filter or classifier change. Aggregate numerical-probe counters still
include all SDK peers. Earlier unscoped snapshots cannot establish AP identity.
`first_fault_line` records the first sticky fault site in the exact hashed Rust
source; zero is required for a qualified trace.

The trace records, privately:

- RF sequence, driver RX timestamp, RSSI, AGC/FFT arguments and sample width.
- Actual compensation call sequence/status/factor, whole filtered-payload hashes
  and zero-byte counts before/after; row admission rechecks the post-call payload.
- Selected compensated I/Q values, stride, linear/log choice and exact amplitude
  bits. The packed 12-bit path mirrors the pinned SDK extraction, not an assumed
  ordinary int16 representation. Zero-byte counts are not zero-subcarrier counts.
- Receive-observation, preprocessing, enqueue and compute times on the same local
  monotonic clock. The driver RX clock is retained separately. These allow queue
  delay and sample-age analysis without pretending the clocks have one epoch.
- Queue token and numerical-window identity, with per-window training status,
  action, counts, background sum, before/after training basis and output features.
  These are radar-stage diagnostics, not the later sensing-FSM presence decision.

Limits are 32 pending RF records, 64 ring rows, 16 columns, 32 rows per window,
ten window slots (eight queued, one executing, one pending send) and one export
slot. The measured 13-column case fits; unsupported shapes fail qualification.
The prepared ESP32-S3 trace reserves **119,616 bytes**, plus the 21,128-byte
snapshot recorder: **140,744 bytes total**. Native mutex initialization, SDK
allocations, stacks and other engine state are additional. Both reservations
exist in all modes of this image, so earlier timing/memory results do not transfer.
The builder/startup guard limits trace storage to 131,072 bytes.

Mutexes are initialized on the control task before admission. SDK paths use
bounded `try_lock` transfers, fixed storage and no added logging/allocation or
wait behind a reader. Any contention, shape mismatch, ambiguous identity,
missing gain/payload association or window exhaustion increments a sticky fault;
`run.py` rejects the trace. No guessed mapping or fabricated native result is
accepted. An unprocessed RF record may be evicted from its bounded cache; the
count is reported, and a subsequently missing row fails admission. This count
alone does not measure all vendor queue drops. Failed native output is never
read. Export failure retains the record. A latched per-call tag prevents an
application-task arm from retagging a preempted numerical window.

The opt-in host protocol requires `snapshots=True, trace=True` and the exact
`rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap`
acknowledgement before credentials. For an
authorized maintenance window:

```python
client.start(ssid, password, 180, "linear", snapshots=True, trace=True)
client.arm_snapshot(1)  # one outstanding tag; await both private records
# Keep tags strictly increasing for the entire session. If preflights use
# tags 1 and 2, subsequent room samples must use 3 and higher.
# client.read(deadline) returns snapshots, rf_preprocessing_trace and statuses.
# Save raw events only to the existing mode-0600 private capture.
summary = TRACE.inspect(trace_record, numerical_snapshot)
assert summary["paired_snapshot_verified"]
```

Import `TRACE` from `run.py`. Drain each bounded record with a sufficient read
deadline; the application task emits fragments while SDK workers continue.
`trace.py` rejects bad checksums, missing/reordered/duplicate data, unavailable
gain, reversed RF identities and inconsistent clock order. Pairing requires
both the explicit numerical identity and exact matrix bits. Its independent
hypot/log comparison and timing/gain aggregates do not export raw I/Q or rows.
The synthetic two-row `fixtures/trace-v1.txt` is shared by Rust/Python tests.

Before flashing, revalidate the current supervisor, newly back up/verify all
16 MB and ensure the gateway trust chain covers recovery. Require a new human
empty-room confirmation, fixed placement and a separately retained holdout;
retain negative results and do not proceed to movement after a failed holdout.
Restore that current firmware and verify fresh real TLS WebRTC frames on every
exit. The initial preparation did not flash hardware; subsequent maintenance
trials and restoration evidence are recorded separately in the assessment.
Physical gain response, trace throughput,
worker stack high-water, RF freshness and occupancy accuracy remain unqualified.

## Espressif-only post-training calibration candidate

`PRESENCE_CALIBRATED` is a separate experimental Rust decision policy over the
unchanged Espressif projection/wander routines. It requires the linear-amplitude,
associated-AP, enqueue-owned RF trace. Ordinary `PRESENCE`, `PRESENCE_CAPTURE` and
`PRESENCE_TRACE` retain their vendor decisions. Raw `PRESENCE_SAMPLE` output is
preserved alongside `PRESENCE_CORRECTED`; do not substitute one for the other in
historical receipts. No ESPectre source, model weights or dependencies are used.
The original Rust module is covered by this repository's MIT license; the pinned
Espressif source packages/algorithm archives retain their Apache-2.0 licenses.

The original default rule can report a feature equal to its trained background
as present because it multiplies by 8.25 before comparing. Moreover, vendor
training selects a restricted subset of features and mutates templates. The new
case instead learns the same statistic used during detection, *after* training
has completed and with no template updates. It does not repair unexplained RF
feature drift or establish that presence is distinguishable in a particular room.

The explicit host sequence, after a fresh human empty-room confirmation, is:

```python
client.start(ssid, password, 600, "linear",
             snapshots=True, trace=True, calibrated=True)
# Wait for healthy trace/gain readiness and keep the room empty.
client.command("TRAIN_START")
# Record the bounded vendor-training interval, then check a successful stop.
client.command("TRAIN_STOP")
client.command("CALIBRATE_BACKGROUND")
# Wait >=15 seconds and >=128 complete statistic evaluations.
client.command("CALIBRATE_VALIDATE")
# A separate empty holdout: new history, >=15 seconds and >=128 evaluations.
# Any above-threshold holdout statistic rejects the calibration; retain it.
client.command("CALIBRATE_ACCEPT")
# Only now request a labelled positive movement control and another empty hold.
client.command("END")
```

Command acknowledgements and `phase=ready` are necessary, not proof of presence
accuracy. Count/time gates cannot be satisfied by repeatedly printing a cached
diagnostic. Features arrive from the actual source-scoped SDK decision callback,
carrying the traced source sequence and RF observation times. All 25 history
positions must contain fresh post-command data; the statistic averages the lower
13. The holdout resets this history and does not reuse background samples.

Let `lo`/`hi` be the minimum/maximum background statistics. The named test case
uses `gain = 1 + 29*sensitivity`, `enter = hi + (hi-lo+minimum_margin)/gain` and
`exit = hi`. Sensitivity therefore changes a margin *above* measured background.
It cannot lower the threshold underneath that background. The test parameters
are sensitivity 0.25, minimum margin 0.000001 and a one-second freshness budget;
these are explicit qualification choices, not empirically established product
settings or an accuracy claim. A saturated envelope is rejected. Thresholds stay
fixed throughout validation and later movement; failed validation never raises
them until the room appears clear.

The module has fixed storage, no callback allocation/printing and no new thread.
Its native mutex is prepared on the control task; callback transfers use
`try_lock`, with sticky unavailable status on contention. Native errors, trace
faults, missing/invalid gain, incomplete training, changed template count,
reversed/duplicate source progress, nonfinite features and expired input reject
qualification. Freshness also expires when polling without further callbacks.
`unknown` covers every uncalibrated/rejected state. Rejected trials require a
new controlled run; there is no automatic retraining on potentially occupied data.
The upstream engine remains allocated until reset as in the reference case.

Host regressions and a target build only qualify these contracts and wiring.
A successful independent empty holdout, labelled walking and seated controls,
another empty hold, repeated sessions and resource/timing measurements remain
required before calling the physical calibration or presence accuracy fixed.

## Exact-event calibration evidence

`PRESENCE_DIAGNOSTIC` opts into the same calibrated case with automatic paired
captures. It does not change training, thresholds, numerical routines or radio
settings. Its acknowledgement adds `capture=calibration_pairs_v1` before the
private credential handoff:

```python
client.start(ssid, password, 600, "linear", snapshots=True, trace=True,
             calibrated=True, diagnostic_pairs=True)
client.arm_snapshot(1)  # optional healthy preflight before TRAIN_START
# Apply the same bounded training/background/independent-holdout commands.
# Automatic tags: 14 training_complete, 15 background_ready, 16 first_rejected.
summary = PAIRS.inspect(pair_record, trace_record, numerical_snapshot)
```

Import `PAIRS` from `run.py`. Tags 14–16 are reserved in this case. Each event
retains the current SDK decision's matrix, projection, actual template
comparisons, RF/preprocessing trace, and the policy's 25 scalar history values
with source sequences. The three records must agree on the exact numerical
window, tag and current source sequence. The host reader verifies checksums,
ring order and an exact float32 replay of the lower-13-of-25 statistic. Raw
matrices, RF identities and history remain in mode-0600 private runtime files.

The first event is selected once at its decision callback. Busy output slots,
try-lock contention or a failed exact-window transfer increment
`PRESENCE_PAIR_STATUS failures`; a later window cannot replace that event.
Require zero failures and drain all three records plus a subsequent status
before ending a rejected trial. An early rejection can prevent earlier stages
from being reached; report missing stages explicitly. A non-rejecting run has
no tag 16 and must not manufacture one.

The retained matrix is one contributor to a rolling statistic, not necessarily
its cause. All 25 scalar inputs are saved, but the other 24 matrices are not.
Some rejection paths reject the current input before adding it to history;
the inspector reports whether the saved current window contributed. Template
and numerical comparisons must respect these limits when attributing drift.

The linked diagnostic slot occupies 268 bytes and the expanded calibration
runtime occupies 328 bytes in the prepared ESP32-S3 image. Existing large
snapshot/trace slots are reused; no thread or callback allocation/printing is
added. At these three events only, a native SDK query records the calling task's
stack high-water in bytes (the pinned IDF stack element size is asserted).
This is an event sample, not complete stack, timing or instrumentation-overhead
qualification. Fresh human empty-room confirmation, current full-flash backup,
bounded maintenance and verified real-camera restoration remain required.

## Source-scoped native anomaly capture

`PRESENCE_FAILURES` is a separate opt-in diagnostic case. It includes calibrated
and exact-event diagnostics and adds the acknowledgement
`numeric_failures=source_v1 other_inputs=frozen_enqueue` before credentials:

```python
client.start(ssid, password, 600, "linear", snapshots=True, trace=True,
             calibrated=True, diagnostic_pairs=True, numeric_failures=True)
# numeric_failure records preserve raw matrix/output float bits privately.
summary = FAILURES.inspect(failure_record)
```

Import `FAILURES` from `run.py`. The first native error status, nonfinite input,
or successful call with nonfinite output is retained independently for
`selected` and `other` call scopes. `other` combines every nonselected SDK call;
it does not identify a particular MAC or peer. Attribution is bound to the SDK
numerical worker and its selected-source call scope. No native status or output
is replaced. Failed calls never read output storage. The wire reader checks
shape, exact bits, checksums and that the failure reason matches those bits.

Other-peer windows are copied by the preprocessing owner before SDK queueing,
using ten bounded Rust slots with explicit producer/consumer task ownership.
The numerical worker consumes only the retained queue token and exact split;
missing tokens, wrong splits, overload and contention cannot fall back to the
changing ring. This changes other-peer input ownership in this named case;
it is not a replay of the preceding borrowed-input trial. The selected-source
enqueue-owned trace is unchanged. The capture work buffer copies those owned
inputs again and saves up to 1,024 matrix words and 64 output words per anomaly.
Callbacks neither allocate nor print. All slots live until the mandatory reset.

Require zero `PRESENCE_FAILURE_QUEUE losses` and zero per-scope status losses.
A lost first anomaly cannot retry with a later matrix. Evidence is flushed
before ordinary diagnostic validation so a subsequent rejection does not hide
the retained native anomaly. Counter reports are observations, not a callback
retirement barrier. A bounded maintenance owner must drain the anomaly and both
scope counters, end the run, restore the current full firmware and verify real
TLS camera frames. The image reserves 54,452 additional static bytes for this
capture/queue; physical throughput, heap/stack headroom and instrumentation
effects remain unqualified. No production HAL or detector integration is added.

`replay.py` performs offline counterfactuals on an exact saved trace/snapshot:
baseline, uniform 1/16 scaling, division by the reported per-row gain factor,
and equal row RMS. It keeps the captured templates fixed. Baseline double
projections can be compared with the actual saved native output; transformed
cases use independent double math and the existing host float32 policy model.
They **do not execute the native archive**. Continuous rescaling cannot recover
pre-compensation integer I/Q after rounding/clipping, and agreement is not
occupancy or causal qualification. Keep raw inputs and generated arrays private.

## Experimental runtime counterexamples

The standalone original Rust witness runs the current calibration/diagnostics
modules with the unchanged `EMPTY_HOLDOUT` settings and synthetic fresh input:

```sh
rustc --edition 2021 -O tests/csi-radio/calibration-runtime-witness.rs \
  -o /tmp/nxrs-calibration-runtime-witness
/tmp/nxrs-calibration-runtime-witness
```

It first accepts separate 16-second synthetic background and holdout phases.
Two controlled interleavings then reproduce undesirable permanent rejection:
a read-only snapshot poll encounters the model writer's mutex (without losing
a source observation), or a reader samples time before a newer publication and
later passes that older timestamp to `snapshot`. The latter uses monotonic
clock samples; actual clock rollback is unnecessary. These characterize defects
in the experimental Nxrs runtime, separate from Espressif's numerical archive.

The witness deliberately requires the current rejection outcomes. It is a
baseline counterexample, not qualification of a repair or proof of which path
fired on a physical device. A repair must distinguish read-only contention from
lost source updates, sample time consistently with model access, and retain
genuine freshness, clock-regression and callback-loss rejection coverage.
