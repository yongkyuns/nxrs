# CSI laboratory app

This is an isolated **experimental CSI-only application and replay harness**.
It has no camera, monitoring gateway or iOS dependency. Its live frontend is the
IDF reference fixture's explicit `PRESENCE_RECORD` case. A 60-second physical
recording and exact host/native replay have passed; this is not a production
NuttX CSI provider or a qualified presence detector.

The live frontend and host executable link **one Rust calibration/runtime
implementation** in `core/`. There is no host copy of the policy. The extraction
preserves the existing thresholds, separate empty background/holdout admission,
rolling statistic, sticky failures and freshness policy. Existing qualification
results remain historical evidence for their recorded source/image hashes.

## What can be replayed

| Level | Inputs and execution | Evidence |
| --- | --- | --- |
| Calibration | Every accepted feature, calibration command, timer poll and applied external fault through the same Rust code; recorded detector time replaces wall time | Every state, rejection, count and float bit pattern must match the live checkpoint |
| Numerical calls | Exact frozen motion matrices, splits, template/current vectors and native status/output bits | Export into a reusable pinned Xtensa/QEMU witness; compare native results bit for bit |
| RF investigation | All filtered SDK RX peers before gain compensation; exact bytes/gain parameters before and after native compensation | Preserves observations for preprocessing/gain investigation; **does not emulate RF or the driver** |

The full vendor radio/preprocessing/training/FreeRTOS sensing FSM is **not**
replayed on the Mac or in QEMU. Numerical call replay exercises the vendor
archive, not that FSM. Raw recording begins at the SDK **filtered CSI** hook,
not at a promiscuous Wi-Fi packet/PHY boundary. RX metadata is the pinned
filtered API's sequence, timestamp, RSSI, AGC, FFT, data type and byte length;
dispatch and source MACs are retained separately. It is not an opaque dump of
SDK structs. Other peers are retained because they can affect global gain.
Only the associated AP feeds the experimental calibrated candidate.

Older walking trials did not retain this continuous input stream. Their sampled
reports and selected snapshots cannot reconstruct it. The synthetic `fixture`
command tests software and is explicitly labelled `synthetic_test`; it is not
room-detection evidence.

## Build and verify without hardware

From the checkout root:

```sh
cargo build --locked --release --manifest-path tests/csi-lab/Cargo.toml --bin csi-lab
cargo test --locked --manifest-path tests/csi-lab/Cargo.toml --workspace
cargo clippy --locked --manifest-path tests/csi-lab/Cargo.toml --workspace --all-targets -- -D warnings
cargo run --locked --manifest-path tests/csi-lab/Cargo.toml --bin calibration-runtime-witness
```

Build the CSI-only firmware with the existing
[IDF build instructions](../csi-radio/presence/README.md). Its build receipt
includes the shared core sources and recorder allocation witness. The builder
does not flash, reset or open USB. Use a fresh `target/csi-radio-NAME` output.

## Collect one reusable real dataset

Flashing/restoration and exclusive device ownership remain outside this app.
Arrange those separately for an authorized maintenance window or a spare board.
Do not run the collector against a live camera supervisor. The collector checks
for an existing port owner, uses an exclusive open and keeps DTR/RTS inactive;
it never flashes, resets, discovers hardware or stops another process.

Create provenance from the matching build (Python 3.9+):

```sh
python3 tests/csi-lab/manifest.py --build target/csi-radio-LAB \
  --output /private/tmp/csi-trial-manifest.json
```

This verifies local source/image hashes, not the actual flashed device. Confirm
flash identity separately. Credentials use an owner-only JSON file containing
`ssid` and `password`; do not pass them as command-line arguments. With Python
and PySerial available:

```sh
python3 tests/csi-lab/collect.py --port /dev/cu.EXPLICIT-SPARE \
  --credentials /private/tmp/private-wifi.json \
  --manifest /private/tmp/csi-trial-manifest.json \
  --output /private/tmp/csi-trial.csilab \
  --csi-lab tests/csi-lab/target/release/csi-lab --seconds 600
```

Controls are explicit newline-delimited commands on stdin:

1. `LABEL_EMPTY`, `TRAIN_START`, then `TRAIN_STOP` after vendor training.
2. `CALIBRATE_BACKGROUND`; collect the required features and minimum duration.
3. `CALIBRATE_VALIDATE`; collect a **separate** empty holdout.
4. `CALIBRATE_ACCEPT` only when that holdout passes.
5. `LABEL_WALKING`, `LABEL_SEATED`, `LABEL_EMPTY` as the actual room state changes.
6. `END` completes recording. No command automatically asserts a room label or
   accepts a failed calibration.

A rejected admission aborts the trial; a partial file remains incomplete. Labels
are recorded on the device's model/control clock, not guessed from USB arrival.
The collector streams the complete transport from startup, including records
before `PRESENCE_BEGIN`. The normal presence reports remain available through
the existing parser but are not substituted for raw/feature records.
Continuous mode requires the exact `record=csi_lab_v2` acknowledgement with
`snapshots=0`. It retains the frozen-input/other-peer ownership checks without
enabling the legacy sampled snapshots or automatic calibration-pair capture.
Those modes have separate retention limits and remain available for their
original experiments.

For an existing console log containing `CSI_LAB` transport lines:

```sh
tests/csi-lab/target/release/csi-lab record \
  /private/tmp/csi-trial-manifest.json /private/tmp/csi-trial.csilab < /private/tmp/console.log
```

Files are created exclusively with owner-only permissions. Keep manifests,
exports and recordings outside Git; they contain RF/MAC observations. The
collector never prints Wi-Fi credentials or arbitrary console lines.

## Deterministic replay and parameter comparison

```sh
tests/csi-lab/target/release/csi-lab replay /private/tmp/csi-trial.csilab \
  > /private/tmp/baseline.jsonl
tests/csi-lab/target/release/csi-lab evaluate /private/tmp/csi-trial.csilab \
  --sensitivity 0.50 > /private/tmp/candidate.jsonl
```

`replay` verifies the entire capture before emitting states and requires exact
live checkpoints. Repeating it produces identical output. `evaluate` first
verifies baseline replay, then explicitly runs a changed sensitivity without
pretending its new states match the old checkpoints. Summary counts separate
unknown, clear and presence for unknown/empty/walking/seated labels. These are
feature counts, **not independent accuracy estimates**; overlapping windows
and calibration phases are correlated. Thresholds never adapt during an
occupied evaluation phase. Other policy experiments should use explicit config
changes with new regression cases and recorded provenance.

## Native numerical playback in QEMU

Export complete numerical calls after validating the whole recording:

```sh
tests/csi-lab/target/release/csi-lab export-kernels /private/tmp/csi-trial.csilab \
  /private/tmp/csi-trial-kernels.jsonl
```

Build `tests/csi-radio/build.py kernel` using its existing pinned package/cache
arguments. The resulting `csikernel replay` witness consumes bounded exact
float bits and invokes the unchanged SDK entry points. Build once, then replay
many datasets without recompiling Nxrs:

```sh
python3 tests/csi-lab/native_replay.py --build target/csi-radio-KERNEL \
  --qemu /path/to/qemu-system-xtensa \
  --export /private/tmp/csi-trial-kernels.jsonl \
  --transcript /private/tmp/csi-native-replay.txt
```

The runner verifies the pinned build/image/source receipt, disables QEMU
networking, uses a snapshot and requires exact call identity, status, output
bits, final count and shell exit status. Failed native calls have no readable
output. A numerical replay mismatch fails; it is never hidden behind a float
tolerance or a host reference implementation. The emulator cannot qualify
radio behavior or device timing.

## Record format and ownership

Container version 1 starts with one JSON manifest line. Records then use the
`CSI_LAB ` envelope followed by 32-bit hex words. Numeric words are exact bits;
the CRC-32 is calculated over each word's **little-endian** bytes.

Header: magic `0x4c495343`, schema version, global sequence, detector/observation
clock low/high, kind, payload words. The payload is followed by CRC-32. Each
payload is at most 256 words. Kinds: session=1, model=2, raw=3, gain=4,
motion matrix=5, wander=6, label=7, end=8, vendor training control=9. Sequence starts at zero. A second
session requires a separate file/epoch; it cannot be concatenated silently.

Session contains all five calibration parameters, including split u64 durations.
Control tag 3 records the associated AP MAC and channel before radio admission;
hardware-origin captures require this unique source identity.
Vendor training controls (tags 1/2) retain start/stop, native status, threshold outputs and
the control clock sampled after SDK return; they do not execute the vendor FSM
on host replay. Model records contain operation, external fault before/after execution, admission
result and checkpoint (phase/state/rejection/count plus five f32 bit patterns).
Features retain source sequence, oldest/newest RF times, template count,
training and gain admission and wander bits. Word 23 joins selected features to
the native motion-window ID; zero means no native identity (host fixtures).
RF timestamps and detector time have different meanings; events are never
re-sorted by RF time. True clock rollback is replayed as an algorithm rejection.

Raw records contain the synchronous callback data address as a within-epoch
correlation hint, radio fields, both MACs and exact bytes. A pointer is never
dereferenced during replay and cannot identify a frame across reuse. Gain
records have call ID, address, before/after phase, status/factor bits, gain state,
AGC/FFT, sample width, byte length and data. Native matrix/vector chunks have
call ID, part (input0/input1/output), columns, row split, total words, offset,
status and exact float bits. The host rejects mismatched identities, shape
changes, reordered chunks, incomplete gain/native calls and nonzero padding.

The device allocates 128 record slots **before** radio admission: the target
Rust slot representation occupies 136,192 bytes, bounded to 150 KB by the build
witness. Separately allocated native mutex objects are not included in that
byte count; every mutex is initialized on the owner task before callbacks.
Each producer reserves a sequence once and fills its owned slot using try-lock;
it never allocates, prints, waits for USB or retains borrowed SDK data. Generation
tags preserve order without a global producer mutex or spinning behind a
preempted producer. The consumer returns when the next slot is not published,
releases slot ownership before formatting/I/O, and computes CRCs on its own
task. Native-call leases keep recording admission across synchronous execution;
END closes admission and waits for those leases. Storage remains allocated
until the recorder is dropped/reset. SDK engine/radio resources still follow
the fixture's existing retained-until-reset policy; recorder shutdown does not
claim radio retirement.

The control task uses a nonblocking USB read in continuous mode and bulk SDK
writes with explicit byte counts. Rust preserves partial/zero writes under a
three-second write deadline. This avoids the IDF stdio VFS path, which can drop
characters after its per-character timeout while reporting the requested size.
Legacy modes retain their existing console path. No new worker thread is added.

END carries record count, rolling CRC chain and loss count. Missing END,
overflow, contention, malformed or missing records, corrupted bits or checkpoint
mismatch invalidate the capture. This is an integrity check, not authentication.
An incomplete or invalid trial cannot be used as clean RF/accuracy evidence.

Continuous recording adds USB/CPU/memory load. The physical receipt below
qualifies lossless transport and sampled stack margin for one 60-second case;
it does not measure instrumentation overhead against an uninstrumented control
or qualify longer captures. Host tests do not change camera/radio firmware.

## Recorder callback stack qualification

The first real recorder trial exposed a stack overflow before any numerical
measurements were retained: Espressif's `csi_handle` worker has a 3,072-byte
stack, while the original float-export function alone used a 3,248-byte frame.
The incomplete capture was rejected and the original camera was restored.

Callback producers now fill preallocated slots in place and carry a small
25-word model event. They do not construct or pass maximum-size wire records
on the SDK worker stack. Host-only wire conveniences may still construct those
records; formatting and draining remain on the separately sized control task.
The wire format and calibration inputs/checkpoints are unchanged.

The IDF builder inspects actual Xtensa prologues and rejects a recorder producer
frame above 512 bytes. This is an individual-frame regression gate, not a proof
of the complete native call-chain budget. `PRESENCE_LAB_STATUS` separately reports
recording losses and sampled native stack high-water marks for RX, gain and
numerical contexts. `4294967295` means a context has not been observed yet; the
host parser exposes it as unavailable. Stack probes use a bounded single compare
exchange; lost diagnostic publication invalidates the capture. Physical recorder
qualification requires at least 512 remaining bytes in every observed context,
zero recorder losses and successful replay of the resulting file.

## Physical recording qualification, 2026-10-25

The [physical receipt](../csi-radio/results/csi-lab-physical-recording-2026-10-25.json)
records the failed initial attempts as well as the corrected 60-second case.
The corrected recorder retained 34,975 records (9,339 raw CSI frames, 18,678 gain
observations, 1,615 feature events and 1,741 complete native motion calls) with
zero losses and a valid END/count/CRC chain. Sampled minimum free stacks were
4,444 bytes for RX/gain and 1,216 bytes for numerical execution. The original
camera firmware was restored and verified with three advancing real frames.

Two full host replays verified every live checkpoint and produced identical
output, taking approximately 100 ms and 97 ms including file verification and
state output. All 1,741 exported motion calls then matched native result bits
in the pinned NuttX/QEMU numerical witness. No wander calls were exercised.
Occupancy was **unknown**, training/calibration were not requested, and every
feature state remained unavailable/uncalibrated. This validates the recorder
and replay path, not presence accuracy or the complete vendor sensing FSM.
Labelled empty/background/holdout and walking/seated datasets remain required.
