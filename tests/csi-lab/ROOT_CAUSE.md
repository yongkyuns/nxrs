# Active CSI root-cause and qualification goal

Root-cause and fix the failed Espressif empty-room calibration, retaining a
commercial-friendly implementation without GPL/ESPectre code. Compare detection
quality and execution/resource costs with the observed ESPectre baseline under
comparable labelled conditions. This goal is active; no fix or parity is
qualified. Changes remain experimental and PR #31 stays draft.

## Baseline and acceptance

The pinned [ESPectre walking receipt](../csi-radio/results/esp32s3-espectre-walking-2026-10-24.json)
reports 108/117 walking motion evaluations (92.3%) at about 3.9 Hz. Its initial
empty holdout has 27/235 motion evaluations (11.5%); the final empty holdout has
0/235. A separate empty trial has 0/236 filtered motion evaluations despite five
raw threshold crossings. These are correlated observations in one room, not
independent accuracy estimates or a controlled superiority benchmark.

The goal requires sustained walking detection, independent empty holdout and
empty return, freshness/recovery, and measured cadence/resource cost. Compare
raw scores and confirmed alerts separately. Preserve empty-phase failures;
do not cherry-pick the all-idle reference phase, treat unknown as clear, tune
on occupied evaluation data or substitute retrospective parameter searches for
independent validation. Freeze a declared candidate before new labelled tests.
Stationary-person presence and precise physical entry/exit latency were not
measured by that baseline and require separate evidence. A detector must also
exclude an always-idle implementation through positive observations.

Hardware comparisons must retain the board/AP/placement and record capture
format, traffic rate, SDK version and output semantics. Host/QEMU results do
not establish radio, execution cost, NuttX or simultaneous camera qualification.
Resource figures must distinguish CPU from wall time, feature cadence from
packet rate, and recording overhead from detector cost. Qualify the current
experimental app before proposing production integration.

## Established evidence

The [v4 empty-holdout receipt](../csi-radio/results/csi-lab-model-owner-empty-holdout-2026-10-25.json)
contains 102,912 lossless records and all 3,298 accepted features. Independent
holdout rejects at score 0.449381679 against frozen threshold 0.435317695.
Exact host replay reproduces every checkpoint. Relevant SDK numerical calls
reproduce in QEMU. Source ownership/transport integrity are preserved; the
only model rejection is the declared holdout failure.

Further offline investigation finds a large profile-shape change already at
the SDK **filtered-IQ** boundary before gain compensation, in both observed
packet-length groups. Exact float32 reconstruction matches every retained
matrix row examined during background/holdout; a profile classification is
accepted only when all matching values agree. Ambiguous frame occurrences are
not assigned identity. Packet lengths do not stand in for complete PHY format.

This localizes the immediate failure: the absolute-distance feature stays far
from its frozen training profiles after the input profile changes. It does not
yet establish why the filtered IQ changed. Environment/other-room movement,
PHY behavior and SDK filtering remain possible upstream causes. The user
confirmed the room empty; movement elsewhere remains unknown unless clarified.
Do not label the radio or native arithmetic faulty on this evidence.

Gain compensation and PCA are not required to create this particular shape
change: it exists before them. Replacing PCA output with a mean/median over
the same captured windows still retains a large distance. The retained
post-training comparison bank contains ten distinct vectors; the SDK collected
sample counter (25) is separate from its ten-comparison cap.

## Current offline experiments

`core/temporal.rs` is an original bounded feature experiment using maximum
absolute-Pearson distance to three recent owned vectors. No SDK pointer, new
thread, allocation or lock enters this core. Time/identity faults are sticky;
warmup and numerically degenerate profiles are unavailable. Contributing RF
time includes all reference vectors, preserving phase and freshness boundaries.

`signals FILE --case recent-three-v1` verifies the original archive first and
emits explicit counterfactual features. `verify-distance FILE` checks the Rust
arithmetic against all recorded SDK comparison bits. Historical source faults
remain unavailable; they cannot be reconstructed or suppressed by a new feature.

The feature arithmetic matches 70,396 recorded native comparisons across the
empty, v4 training and historical fault recordings. Two signal runs per archive
are identical; all 1,322 historical fault observations remain unavailable.
This is host arithmetic qualification, not deployment or target-cost evidence.

The recent-three feature with the **unchanged historical calibration rule**
still rejects: approximately 0.001768823 exceeds 0.001752114. It is not a fix.
A prospective development-only policy using 250 ms score medians, robust
background dispersion and four-on/three-off confirmation has no confirmed
motion in the remaining ~15 seconds of this development recording. It was not
a parameter sweep. That short, previously inspected suffix cannot qualify a
complete independent holdout or any walking sensitivity.

`preview-motion FILE --case temporal-motion-v1` now evaluates that declared
policy through the complete immutable archive. It first verifies the historical
model and all native input joins, then validates the derived pass before any
output. It never replaces the recorded negative result. It requires a successful
recorded vendor training stop and explicit empty label; an unknown-occupancy
recording cannot calibrate it. The schedule fits 128 complete 250 ms bins over
at least 32 seconds, then requires 240 independent empty bins over at least
60 seconds. Thresholds freeze at median + six MAD for entry and median + three
MAD for exit, with a minimum dispersion of 0.000001. Each bin requires 3–64
fresh observations. Four positive bins confirm moving; three quiet bins confirm
idle. Phase boundaries discard partial and pre-command RF support. Confirmation
never makes background/holdout available, and acceptance clears the earlier
confirmation before live evidence begins. There is no occupied-data adaptation.

The saved empty archive supplies 128 background and only 57 holdout bins. Five
raw positive bins do not form sustained motion. The model correctly remains
holdout/unknown: the missing 183 holdout bins cannot be manufactured by replay.
The unknown-occupancy archive remains uncalibrated, and the historical source
fault remains rejected/unknown. Two previews of each archive are byte-identical;
the earlier feature-only outputs remain byte-identical too.

Full-container synthetic witnesses exercise a complete background/holdout,
sustained profile changes, return to idle, frozen thresholds, source failure,
silent expiry and a false-motion holdout rejection. The rejecting bin is retained
as evidence. Corruption and missing native window identity produce no derived
output prefix. These are software witnesses, not measured physical accuracy.
The candidate emits **motion evidence**, not stationary-person occupancy; the
ESPectre walking baseline also measures motion. The broader presence goal stays
open and requires its own stationary-person evidence.

## Standalone device candidate

The explicit `PRESENCE_MOTION` case now runs the same schedule in
`core/motion_trial.rs` on the existing control task. `core/motion_pipeline.rs`
owns the recent-vector feature and policy. The native callback copies a bounded
owned profile into the existing source handoff; it does not run the model, create
a worker or hold a model mutex. This named LLTF/step-four case supports 2–16
columns and rejects unsupported shapes. The observed source has 13 columns.
Vectors are copied before their SDK output storage is reused, retain exact
native window identity, and are cleared when a trace window slot is reused.

The acknowledgement is `record=csi_lab_v5 model_owner=control_v1
motion=temporal_motion_v1`. A recorded case declaration `[4,1,16,0]` and new kind-10,
32-word version-1 checkpoints prevent silently replaying this case as a
historical-only capture. Native outputs remain recorded independently. Ordinary
`replay` reconstructs the new policy from those outputs and checks every new
checkpoint, in addition to the unchanged absolute model. Missing, reordered or
forged checkpoints fail replay. The new case refuses historical calibration
commands: its frozen schedule owns the phase transitions. The original
`PRESENCE_RECORD` acknowledgement and negative comparisons remain available.

Full-container owner witnesses reproduce all checkpoints through a complete
synthetic holdout, sustained motion, return, source fault, silent expiry and
holdout rejection. A semantic forgery with valid rebuilt CRCs is rejected.
Label/training clock rollback and failed vendor stops are sticky. A source fault
seen in a clock/poll immediately makes motion unavailable even without another
source feature. This strengthens the earlier development preview's fault timing;
the original feature-only signal outputs on all three archives remain identical.

The source-locked ESP32-S3 build passes 75 native ABI checks and the unchanged
resource/512-byte producer-frame gates. Its Rust source handoff consumes 8,208
bytes plus native mutex/allocator overhead, and the motion pipeline has a separate
1,824-byte Rust heap allocation. No worker is added. New callback-path frames
are 48 bytes for profile handoff, 80 bytes for the compute bridge, and 112 bytes
for profile retention. These are individual compiler frames, not complete native
call-chain high-water measurements. There is no new physical trial or CPU-cost
qualification. The provider's optional strict host Clippy invocation still has
pre-existing errors; an isolated baseline comparison finds no added diagnostics.

## Target execution and numerical correction

The new UART/no-radio probe executes the actual shared Rust pipeline on Xtensa
from original inputs. Its output is compared word for word with the host, and
its numerical checks retain the first/last 16 recorded SDK comparisons per
capture. None of the recorded policy checkpoints or thresholds are supplied
as answers. The full original capture and native identity joins are verified
before export. Unknown-source recordings and historical faults remain unknown.

The first observable target run rejected a Rust/SDK numerical mismatch.
Disassembly showed `madd.s` at the three Pearson accumulations where the SDK
kernel uses separate multiplication/addition. Explicitly materializing f32
product bits removes that contraction; the unchanged input then matches all
3,303 empty-capture checkpoints and 32 SDK comparisons. The full three-capture
target run matches 8,399 checkpoints and 96 SDK comparisons. All 70,396 SDK
comparisons still match on the host, and all three historical feature-only
outputs remain byte-identical. Threshold fitting uses the same rounding boundary.
Actual target disassembly gates enforce separate products in both routines.

This is a new-candidate reproducibility correction, **not** evidence that the
earlier vendor-only calibration failure was caused by arithmetic contraction.
The [GCC Xtensa documentation](https://gcc.gnu.org/onlinedocs/gcc-12.1.0/gcc/Xtensa-Options.html)
describes why combined multiply/add instructions can change intermediate
rounding. No compiler flag, threshold or acceptance limit is loosened here.
The first emulator observation also exposed a console-owner bug: closing its
stdin disconnects the stdio backend. The runner now retains the pipe and waits
for whole terminal lines. Initial failed observations remain private negative
evidence; they are not counted as passing runs.

## Readiness and prospective physical trial

The current version-6 motion case also requires valid vendor training/gain
metadata on RF windows wholly after the successful training stop. Queued
pre-stop RF remains excluded through the existing support boundary. Missing
readiness rejects as unavailable, with the first numerical/source fault
preserved. A duplicate stop cannot move that boundary; retraining after a stop
is refused. No threshold, holdout duration or positive-confirmation rule changes.

The [readiness/cue receipt](../csi-radio/results/csi-lab-motion-readiness-2026-10-26.json)
records exact host/Xtensa agreement for 26,318 checkpoints across nine cases,
including lost vendor readiness and duplicate stops. The saved empty input
still supplies only 57/240 holdout bins and cannot establish admission. The
current pipeline allocation is 1,840 Rust heap bytes; source handoff and native
frame gates retain their budgets. Physical cost remains unqualified.

The software-only host controller now measures complete recorded RF bins,
requires actual device label acknowledgements, freezes threshold bits, and
provides independent empty control, walking, seated and empty-return phases.
Each phase requires at least 240 bins spanning 60 seconds. It has explicit
source, calibration, recorder, clock and human-reply failure paths. The private
maintenance owner is prepared with the original full-flash restoration and
real-frame verification procedure; no new physical trial has run. A fresh Out
reply is still required before that procedure starts.

## October 27 artifact and recovery preflight

The fresh labelled trial did not start: its preflight found missing temporary
credentials and verifier dependencies. Subsequent inspection also found missing
tracked checkout files and gateway runtime files. The camera supervisor later
exited with `console_stalled`, before the investigation opened its unowned port.
The cause of the file disappearance and of that camera stall is unproven.

The [recovery receipt](../csi-radio/results/csi-lab-artifact-recovery-2026-10-27.json)
preserves the three original recordings and 33 full-flash backups in owner-only,
persistent storage outside checkouts. Only missing tracked files were restored
from the pushed commit; a managed durable checkout now holds the same source.
Both host replay passes per recording agree, and all 70,396 retained SDK
comparisons still match. The unchanged candidate's source and image hashes
verify. Repeated checks are not new independent radio observations.

The initial emulator recovery attempts stopped because its ROM file was also
missing. Restoring that fixed ROM from the checksum-verified original Espressif
release, whose executable matches the retained binary, restores all 26,318 exact
host/target checkpoints across the same nine cases. No threshold or comparison
gate changed. No QEMU source or ROM is committed to Nxrs.

A five-second serial read sent no commands and kept DTR/RTS disabled. It saw a
NuttX boot and `USB_UART_CHIP_RESET` marker, with no panic marker. This does not
establish the reset's timing/cause or prove that opening the port had no effect.
No firmware write was attempted at that recovery checkpoint. The camera was
**not yet verified live** because its original supervisor had exited. Do not reopen or flash the board merely
because the saved image is intact. Recover the required private connection and
gateway verification inputs, verify real video, then request a fresh Out before
running the prepared labelled trial. Existing enrollments, certificates and
media services were not replaced or restarted.

## Physical temporal-motion recording with uncertain occupancy

The [2026-10-28 recording receipt](../csi-radio/results/csi-lab-temporal-motion-confounded-2026-10-28.json)
retains a physical trial of the unchanged version-6 candidate. The journal records
an empty label, but an unrecorded occupancy change was reported afterward with
unknown timing and duration. Keep those protocol labels immutable and annotate
the independent ground-truth limitation. Neither empty-room false alarms nor
successful human detection can be established from this run. Background fitting
may also be confounded; do not use it for threshold tuning.

The model rejected at holdout bin 206 of 240. Seven bins exceeded the frozen
entry threshold, with four consecutive positives at rejection. It remained
unknown, issued no walking/seated cue and was never admitted. All 117,469 records
and 3,777 accepted features are retained without loss or pending source work.
Two complete replays and two policy previews agree byte for byte; all 35,061
recorded SDK distance comparisons match Rust exactly. These numerical and
ownership observations do not resolve the missing occupancy timing.

Device-recorded feature delivery averaged 24.317 Hz. Newest-RF-to-policy age
had median 13.911 ms, p99 38.630 ms and maximum 91.879 ms across all 3,777
features. Completed policy bins retained about 409 ms median RF support and
had at most 137.092 ms age from their newest contributing RF sample. These
measurements include scheduling and recording overhead; they are not CPU time,
admitted detector cadence, physical entry/exit latency or ESPectre cost parity.
Occupancy uncertainty does not alter these recorded clock differences.

The original firmware was restored and verified. The physical iPhone then
verified three advancing native 320×240 frames over HTTPS/WHEP and joined session
cleanup with unchanged enrollment. Repeat the frozen candidate with fresh,
explicit labels before drawing detection conclusions or changing parameters.

## Fresh empty repeat stopped by source support expiry

The [second 2026-10-28 trial](../csi-radio/results/csi-lab-temporal-motion-source-expiry-2026-10-28.json)
used a fresh explicit empty confirmation, with the phone outside the room. The
unchanged candidate stopped before vendor training completed. No background,
holdout or occupied measurement was reached. This is an availability failure,
not evidence of a false alarm or successful presence detection. The earlier
occupancy-confounded holdout is a separate recording and remains inconclusive.

Every live checkpoint reproduces in two byte-identical replays and previews.
The first fault is `Signal(Expired)` at device time 62,189,023 us. The arriving
window's newest RF was only 10,392 us old, but the oldest of the three retained
reference profiles was 1,166,175 us old, exceeding the unchanged 1,000,000 us
support deadline. Feature delivery paused for 953,073 us. The source owner
applied all 1,440 accepted features with no pending work; all 38,103 records
were retained without loss and all 6,823 native distance comparisons match.
There is no observed kernel panic or numerical error in this recording.

At the SDK-filtered IQ boundary, the selected peer's 216-byte class had a
1,011,220 us callback gap and a 1,011,216 us radio-timestamp gap. Selected-peer
104-byte frames continued. These are filtered packet-length classes, not
complete PHY identities or proof of ICMP packet identity. The pinned router
ping requests 100 Hz, but its default receive timeout remains 1,000 ms and its
send loop waits for reception before sending again. That is a plausible source
of a one-second sampling interruption; no ping timeout counters or ICMP trace
were captured, so causality is unproven. The capture does establish that this
class gap precedes the feature interruption; do not label it complete RF silence
or a recorder/model queue backlog.

Preserve the frozen policy and its fail-closed freshness decision. Before a new
room trial, instrument send/reply/timeout timing and the SDK window admission
path, then qualify a bounded traffic remedy against loss and resource budgets.
Do not suppress stale reference support, change the threshold or claim this
availability investigation fixes the original calibration drift. The full
original camera firmware was restored and verified; the physical iPhone again
decoded three advancing 320x240 HTTPS/WHEP frames and joined native cleanup.

## Sampling-provenance candidate

The [version-7 build receipt](../csi-radio/results/csi-lab-sampling-provenance-build-2026-10-28.json)
adds exact-source taps to the original SDK ping reply/timeout callbacks and the
actual window-update decision branches. It retains the same vendor ping timeout
and all frozen model, freshness and admission rules. Kind-11 sampling records
join the existing loss-fatal bounded journal; `csi-lab sampling` verifies the
whole capture and all policy checkpoints before emitting observations. Missing
or failed SDK profile reads remain unavailable. Callback completion times are
not ICMP send times, and these taps do not supply complete raw PHY identity.

The ESP32-S3 build passes the original 75 ABI checks and RX observer linkage
plus disassembly checks for all four sampling call paths and four public ping
profile reads. The new native bridge/Rust callback frames are 48/96/112 bytes,
within the unchanged 512-byte individual-frame limit. Ping and preprocessing
task minimum free-stack values are recorded; margins below 512 bytes invalidate
the dataset. Source handoff and pipeline storage remain 8,208 and 1,840 Rust
bytes, with no added worker or callback allocation. Physical call-chain margins,
recording overhead, availability and performance remain unqualified.

All 160 Rust, 154 CSI Python and 23 architecture/HAL-boundary regressions pass.
The dependency archive hashes are unchanged. Official registry downloads varied
only in per-download ZIP member times; restoring those metadata bytes recovered
the exact original whole-archive hashes, with every member byte unchanged. The
first physical version-7 attempt is recorded below. Its truncated capture does
not qualify the diagnostic overhead, timing evidence or candidate behavior.

## Exact SDK window replay of the source-expiry recording

A [host witness of the pinned SDK window routine](../csi-radio/results/csi-lab-sdk-window-replay-2026-10-28.json)
now executes its exact original and instrumented function bodies on all 7,230
selected filtered callback rows. Every context state and return value agrees;
two complete decision streams are byte-identical. With the pinned 200 ms handle
time and 20 ms receive setting, the function predicts 1,441 windows. Its exact
half-open input support matches all 1,440 recorded model feature identities in
order; the additional predicted/numerical window remains unmatched to a model
feature and is not substituted for one.

Near the expiry, the prediction resets its window four times at 101/102/101/101
ms gaps against the 100 ms timestamp guard. That explains how continued frame
arrivals can coexist with interrupted feature support under these timing rules.
It is conditional on callbacks reaching preprocessing: old recordings do not
contain actual branch events or ping outcomes. This evidence strengthens the
window-timing explanation; it does not prove the ping-timeout cause or the
original profile drift's cause. The new physical diagnostics remain necessary.

This check also caught incorrect unit labels in the unflashed diagnostic image's
host view: the SDK ring stores radio timestamps divided by 1,000, and window
deltas/limits are **milliseconds**. Those fields are now named explicitly in ms;
the independent journal event clock remains microseconds. The trigger row at
`end` is excluded from the numerical `begin..end` range. It is now labelled
`trigger_source_sequence` so it cannot stand in for the newest included matrix
row. No physical version-7 recording used the earlier labels, and no model,
threshold, freshness deadline or native window rule changed.

## Version-7 physical attempt: native Wi-Fi receive fault

The [fresh empty-room attempt](../csi-radio/results/csi-lab-sampling-cache-fault-2026-10-28.json)
ended in the initial settling phase, about 6.24 seconds into the recorded prefix,
before calibration or any walking/seated cue. Firmware reported a cache-access
panic in `wDev_SnifferRxData`, through `wDev_ProcessRxSucData` and `ppTask`.
This is an external IDF reference failure, not evidence of a NuttX kernel fault
or of presence accuracy. The user was released from the empty-room hold as soon
as the failure was observed.

Offline symbolization uses the exact image's hash-verified ELF. The sniffer
frame resolves to its ROM `memcpy` call after subtracting header and aligned CSI
lengths from a descriptor length. The saved copy-length register is
`0xffffff58` (signed -168), consistent with an invalid unsigned copy size.
These are fault-time registers, not a pre-call argument/descriptor witness.
The original descriptor and buffer ownership history are absent. Native length
underflow, earlier corruption and interaction with instrumentation remain
hypotheses; the symbolized call stack alone proves none of them. No vendor
binary, model, threshold, freshness rule or ping timeout was changed afterward.

There are 4,920 contiguous CRC-valid prefix records, including 1,396 sampling
records and 153 model feature events. The latest status had zero reported
losses and free-stack margins of 4,444/4,444/1,360 bytes, but those snapshots
precede the fault and do not establish final accounting. There is **no END
footer**. Replay, sampling output and full-policy preview each reject the
original capture twice identically, with no stdout results. Do not append a
footer, admit the prefix as a dataset, or infer ping/window causes from it.

The controller restored and hash-verified all 16 MB of original firmware,
restarted the original continuous camera supervisor and passed the physical
iPhone native test: at least three advancing 320x240 HTTPS/WHEP frames, joined
session retirement and normal app relaunch using unchanged enrollment.

The next experiment must first isolate native Wi-Fi receive stability, with a
bounded standalone witness and explicit driver/buffer provenance, before another
long labelled calibration sequence. Keep the pinned baseline and any changed
SDK or receive configuration in separate named cases. Do not replace the older
source-expiry or occupancy-confounded evidence with this different failure.

## Remaining work

1. Review upstream filtered-IQ changes and any clarification about movement
   outside the room. Extend provenance at the raw-driver/filter boundary if
   needed; do not infer a physical cause from packet timing or buffer addresses.
2. Preserve the declared temporal-motion candidate and its original negative
   comparisons. The bounded policy and deterministic full-policy preview are
   implemented; physical sensitivity, complete independent holdout and
   stationary-person occupancy remain unqualified.
3. Add target numerical/execution witnesses for the new Rust policy and measure
   cost separately from recording/USB overhead. Exact Xtensa replay now covers
   all saved inputs; synthetic positive/return/fault cases exercise the other
   paths. No replay/build result establishes radio sensitivity or NuttX
   qualification. Verify all source hashes before a new trial; older builds are
   historical artifacts rather than deployable current candidates.
4. Qualify a fresh, independently labelled empty/occupied/empty sequence and
   aligned reference comparison. Do not request another room trial until a
   concrete candidate and recovery owner are ready. Require a fresh human label;
   previous Out confirmations have expired. Restore and verify the real camera.
   Post-training metadata/retraining admission and the host cue/restore
   controller are now prepared and checked in software. The first physical
   recording has uncertain occupancy; its fresh repeat stopped during training
   with stale reference support. Preserve both. Instrument the sampling
   interruption with version-7 diagnostics only after isolating the newly
   observed native Wi-Fi receive fault. Its first attempt has no complete
   footer and supplies no qualified timing or positive-performance evidence.
5. Commit evidence normally to draft PR #31. Mark the goal complete only after
   the cause/fix is substantiated and comparable observations establish the
   required performance; never equate a passing empty phase with detection.
