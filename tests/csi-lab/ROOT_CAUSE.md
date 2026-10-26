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
   qualification. Rebuild after rounding changes, and verify all source hashes
   before a new trial; v7 and v10 images are stale.
4. Qualify a fresh, independently labelled empty/occupied/empty sequence and
   aligned reference comparison. Do not request another room trial until a
   concrete candidate and recovery owner are ready. Require a fresh human label;
   previous Out confirmations have expired. Restore and verify the real camera.
   Before that trial, harden post-training metadata/retraining admission and
   prepare the new case's host cue/restore controller; an available build alone
   is not the complete physical trial procedure.
5. Commit evidence normally to draft PR #31. Mark the goal complete only after
   the cause/fix is substantiated and comparable observations establish the
   required performance; never equate a passing empty phase with detection.
