# Current-source std and selected-owner qualification

This finite qualification package closes the current-source executable gap
between the ordinary std channel witnesses and the real independent-input
owner in [`tests/nuttx-std/wifi`](../nuttx-std/wifi). It builds through the normal
`cargo nxrs` frontend. It imports the actual owner and Wi-Fi controller; it does
not copy the owner into a benchmark adapter or add an application framework.

The [Wi-Fi scan owner contract](../nuttx-std/wifi/CONCURRENCY.md) records the
actual ownership, queues, wait dependencies, and lifecycle that these cases
exercise. Its real-provider scheduling and timing limits remain distinct from
the synthetic schedules below.

The inputs are synthetic `Wifi` implementations and ordinary task-context
source workers. No radio is acquired or enabled. Physical radio acquisition,
SDK callback lifetime, sensor I/O, network service, timing bounds and resource
measurements retain their separate qualification obligations. In particular,
these small functional witnesses are not a cross-RTOS RAM comparison.

There are **twelve distinct cases**, split across four native build suites. The `owner`
suite contains eight channel, owner and latest-value cases. The
`mutex-inherit` suite contains two cases; `mutex-no-inherit` contains one.
The `publication` suite contains the production publication witness and repeats
`std-mutex-inherit` in the same image as its native mutex control.
Each suite uses a separate image with the corresponding pthread mutex default.
Full suite coverage uses four builds and thirteen fresh-boot captures per target.
Host mode runs all twelve cases in fresh processes. Every case must complete with checked results
and joined threads; a hang is a failed capture, never an expected pass.

## Case inventory

| Case | Controlled witness | Exact successful work / cleanup |
|---|---|---|
| `std-blocked-delivery` | Current `nxrs-messaging` bounded std channel, capacity one; priority-180 receiver is observed waiting before each priority-120 producer release. Sixteen sequenced payloads include checked data. | 16 admitted / handled, 16 phase-matched native waits, 2 joins, sender disconnection observed. |
| `std-full-progress` | Priority-180 producer fills capacity one, observes `Full` with original payload, then blocks in a bounded sleep gate so the priority-120 receiver can drain. One explicit retry succeeds. | 3 attempts, 2 admitted / handled, 1 rejection, 2 joins. This qualifies this explicit yielding contract. |
| `std-disconnect` | Priority-180 receiver is observed waiting on an empty inbox before the coordinator drops the last sender. | 1 native wait, `Disconnected` observed, 1 join. |
| `owner-independent-inputs` | Actual `OwnerLoop::run` waits across its independent inputs. Separate source workers deliver an important link event, an ordinary scan result and an ordinary completion, each after a phase-matched blocked-owner observation. | 3 admitted / handled, 3 native waits, 3 joins; provider reclamation follows both source joins and the owner join. Old command/event endpoints report `Closed`. |
| `owner-full-stop` | Important and ordinary queues are both full. Additional data rejects with `Full`; the isolated stop remains admissible and duplicate stop reports `AlreadyPending`. | 4 data attempts, 2 admitted then explicitly discarded by receiver retirement, 2 rejections, 1 join; old endpoints report `Closed`. |
| `owner-fairness-deadline` | Two finite owner runs: an ordinary result is serviced after at most seven queued link events plus one accepted important command; logical time then demonstrates deadline reconciliation before the first selected link event. | 23 admitted, 21 handled, 2 discarded on stop, 2 joins. The deadline subcase deliberately releases a timed-out operation before shutdown. This is ordering, not measured deadline latency. |
| `owner-failure-recovery` | Failure notification is rejected by a full queue while the provider retains its terminal fault. Observer refusal and one injected stop refusal are reported, then the same owner/controller resumes and stops. | 2 data attempts, 1 admitted then discarded, 1 rejection, 3 failure observations, 2 stop attempts, 1 join; sticky fault retained, underlying provider `finish` called once despite an explicit second finish. |
| `std-mutex-inherit` | Low/medium/high workers at FIFO priorities 120/150/180 contend on an ordinary `std::sync::Mutex`; the high worker is observed blocked while the low holder inherits priority 180. Low and medium are then made runnable together. | Protected value becomes 42; high acquires before medium starts; low returns to priority 120 after unlock; 3 worker joins and schedule restoration. |
| `std-mutex-no-inherit` | The same Rust mutex workload with kernel PI still enabled and the default pthread mutex protocol set to `NONE`. The low holder remains at priority 120. | Medium completes its finite work window before low releases and high acquires; protected value becomes 42; 3 worker joins and schedule restoration. |
| `std-mutex-nested` | A priority-120 owner holds nine ordinary Rust mutexes while nine waiters at priorities 140–180 block in turn. The owner releases them in reverse order, then repeats the complete cycle. | 18 checked handoffs, 48 native waits, 10 worker joins; tracked holders grow from zero to nine and return to zero each round, with donation and stepwise priority restoration. All nine final protected values are 22. |
| `latest-value-progress` | Two fixed triple-buffer readers. A priority-120 writer pauses during record preparation and between the two publications. A priority-120 reader then holds a slot while the coordinator publishes to both readers. | 2 complete prior reads, 1 check for different complete versions across readers, 32 publications while the slow reader retains its slot, both peers joined. Separate from FIFO accounting. |
| `publication-progress` | Actual navigation publication API with two readers. A priority-120 writer is paused while a priority-180 reader obtains 64 complete prior snapshots; a priority-120 reader is then paused while a priority-180 writer publishes 64 IMU updates. | 4 joins, 2 native waits, original timestamps/freshness, independent sources, sticky IMU/GNSS/satellite faults and late-sample suppression. Coalescing is separate from FIFO accounting. |

The latest-value case compares 4,096 uncontended rounds for IMU, GNSS fix and
satellite values with reception stamps. A round publishes once and reads at
both destinations. Reported time is elapsed time for the whole batch. Reported
record bytes exclude endpoint metadata and allocator padding. This is a storage
and copy comparison, with product fanout, lifecycle, and interrupt costs still
to qualify. The pinned crate uses an `AtomicU8` exchange on publish and on a
dirty read. Compiler lowering and target progress require separate evidence.

Three fresh boots on CoreS3 using `m5stack-cores3-dual-imu` passed both paused-peer
controls and the fanout-skew check. The fixture used single-core ESP32-S3 NuttX
with priority inheritance disabled. The linked candidate's byte exchanges use
the toolchain's `s32c1i` retry loops. The timings below are medians of the three
batch times divided by 4,096. The runs have no real sensor I/O or BLE sessions.

| Stamped record | Record bytes | Mutex, µs per round | Two mailboxes, µs per round |
|---|---:|---:|---:|
| IMU | 64 | 6.12 | 11.48 |
| GNSS fix | 112 | 6.58 | 19.10 |
| Satellites | 104 | 6.57 | 17.88 |

For two IMUs, a fix and satellite reports, the candidate needs 2,064 bytes for
payload slots before padding, endpoint state and allocation overhead. Product
latency, footprint and the effect of sensor and transport concurrency are still
adoption gates.

### Production publication comparison

`publication-progress` exercises `navigation::observations::publication`, the
same fixed-reader API used by Motion Tracker. Native peer snapshots retain
their thread IDs, FIFO policy, base/effective priority 120 and semaphore-wait
state. The higher-priority worker completes while that peer remains paused.
Both pauses are **between production API calls**; they do not instrument the
middle of a payload copy or atomic instruction. The original raw-buffer case
retains its separate preparation/fanout checks.

After all four workers join, a serial comparison runs 4,096 rounds. Each round
updates two IMUs and takes two snapshots. Both variants use original reception
times and the same freshness semantics. The baseline is one
`Arc<Mutex<Observations<2>>>` with separate writer and reader handles; the
candidate uses the production publishers and readers. GNSS is disabled in this
timed comparison, while the functional portion checks fixes and satellites.
The same native image uses the normal PI-enabled mutex default, verified by
runtime configuration and its separate `std-mutex-inherit` case.

The diagnostic global allocator records requested Rust bytes and blocks in
contiguous setup, first-use, steady-state and teardown windows. Publication
first use and both steady-state windows require zero allocator calls. Mutex
first use is measured separately because native std mutex initialization can
allocate. Both variants must return to their setup baseline after teardown.
Measurements exclude native allocations that bypass the Rust allocator and do
not establish OS heap usage or stack high-water. Probe instrumentation and
uncontended timings cannot establish physical worst-case latency. The probe
itself can retry while allocator hooks are active, so snapshots run only after
the workers have joined; it is not part of production publication.

The implementation's ARM MPS2 QEMU run passed both publication and mutex
inheritance cases on fresh boots of the same image, including 126 native ABI
witnesses at build time. The retained build identity was
`81862211df48d6f2ce24fdcdac4854af43f472f40102a95d9e01139cb76100ae`.
The two-IMU, two-reader comparison (GNSS disabled) observed:

| Requested Rust allocation | Mutex model | Production publication API |
| --- | ---: | ---: |
| Setup bytes / blocks | 304 / 1 | 1,036 / 5 |
| Additional first-use bytes / blocks | 48 / 1 | 0 / 0 |
| Steady-state allocator calls | 0 | 0 |
| Bytes / blocks remaining after teardown, relative to baseline | 0 / 0 | 0 / 0 |

The additional mutex allocation was a zeroed first-use allocation. These
measurements quantify this matched model, not Motion Tracker's full four-stream
publication set or native heap usage outside Rust. QEMU elapsed times are kept
in the raw records without a physical speedup or deadline claim.

Data accounting excludes the isolated stop token, endpoint-closure probes and
the owner’s derived reports. A handler may create several reports for one input.
`admitted = handled + discarded` and `attempted = admitted + rejected` are
checked independently of report counts. Joins are actual `std::thread::JoinHandle`
joins, counted only after successful return. The report’s quiescence basis is
`all-fixture-workers-joined`; it does not claim a measured zero count for real
SDK callbacks.

## Execution contract and limits

Native runs require flat, single-core NuttX. The selected suite fixes the
synchronization configuration; the build receipt, effective `.config`, and
runtime observations must agree.

| Native suite | Kernel priority inheritance | Default pthread mutex protocol | Admitted cases |
| --- | --- | --- | --- |
| `owner` (default) | Disabled | `NONE` | Eight channel/owner/latest-value cases |
| `mutex-inherit` | Enabled | `INHERIT` | `std-mutex-inherit`, `std-mutex-nested` |
| `mutex-no-inherit` | Enabled | `NONE` | `std-mutex-no-inherit` |
| `publication` | Enabled | `INHERIT` | `publication-progress`, `std-mutex-inherit` |

The mutex pair keeps kernel PI enabled and changes the pthread default rather
than treating kernel support as proof that a mutex inherits. Native `nxmutex`
resource locks still inherit in the no-inherit control. A small native
binding reads freshly initialized default attributes; it never casts or
accesses the private layout of a Rust mutex. The observed priority donation
and acquisition order come from contention on the actual `std::sync::Mutex`.
The prepared std source and effective configuration remain bound to the build.
The frontend compares target libc and native pthread layouts after linking,
before accepting the build. That ABI gate's `pthread_rwlock_t` coverage does
not qualify Rust `RwLock` donation; these runtime cases exercise Rust mutexes.

The inherit and publication suites use the normal
[resource-mutex policy](../../docs/build-system.md#resource-mutex-policy), including
`CONFIG_SEM_PREALLOCHOLDERS=0` for embedded holder records. The owner suite
deliberately requests `kernel-pi-disabled` to preserve its scheduling baseline;
the no-inherit suite requests `no-inherit` as the HML negative control. The
workflow supplies `--qualification-mutex-policy` and a nonempty
`--qualification-mutex-reason` for those two experiments. Its marked fixture
manifest admits these controls; ordinary profile settings cannot select them.
Current receipts retain the frontend's policy and compare it with the suite's
expected policy and effective configuration.

The selected-owner cases set **every fixture participant** to FIFO priority 180:
coordinator, source workers, owner, command/event callers and stop caller.
The std channel cases use their listed priorities and a priority-220
coordinator. The latest-value case uses priority-120 peers and a priority-220
coordinator. The HML pair uses low/medium/high priorities 120/150/180 and
coordinator priority 220. The nested case uses owner priority 120, nine waiter
priorities from 140 through 180 in steps of five, and coordinator priority 220.
The production publication case uses two separate priority-120 paused peers,
two priority-180 progress workers, and a priority-220 coordinator.
Every participant records native thread ID, policy
and priority before entry, at entry, before exit and after restoring its
previous schedule. The parser checks the complete inventory and restoration.

For the mutex pair, the coordinator observes the high worker in semaphore wait
after its lock-attempt marker, records the low holder's effective priority, and
makes low and medium runnable together before blocking. The worker trace records
the low worker immediately before dropping its guard, high acquisition, medium
start/finish, and the holder's return to its base priority. The `low_unlock`
marker precedes the actual unlock: high can preempt inside guard drop, so this
is not a post-unlock timestamp. Medium does work for a finite 25 ms observation window. That
window demonstrates controlled interference; it is not a product blocking
budget or worst-case execution-time claim. Native ordering is required only
for the matching target suite.

The nested case checks more than the former shared pool's eight holder records:
each of nine resource mutexes retains its own owner record. Native snapshots
observe the parked owner before contention, after each new waiter, and after
each release. Two rounds check return to zero tracked holders and reuse of the
same mutexes. This does not establish a global lock order, arbitrary nesting
depth, transitive PI through every native subsystem, or a blocking-time bound.
The experiment reserves ten worker stacks; it is not a proposed product design.
After joining those workers, the same case exercises the public ordered-lock
API: valid nested acquisition, rejection of a fresh root inside an active scope,
and successful reuse after scope exit. This checks its std mutex and thread-local
paths on the selected target; the separate compile controls reject invalid
rank orders before a program can run.

All endpoint calls occur in task context after scheduling setup. No ISR calls
std or Crossbeam. Gate loops have a four-second failure deadline and sleep for
one millisecond on each unsuccessful iteration, allowing eligible peers to run.
An independent native 15-second watchdog aborts a stuck invocation. Those values
bound a qualification failure path; they are not application response-time or
worst-case execution-time guarantees. Main and worker stacks are explicitly
16 KiB on NuttX, a qualification budget rather than a demonstrated minimum.

`native.c` translates target headers for scheduling, mutex configuration,
atomic TCB wait/runnable observations, and the watchdog. Rust owns the mutex,
queues, threads, gates, cases, errors, stops and joins. A native blocked witness requires both
the intended unfinished Rust phase and the live target thread’s wait state,
effective priority and FIFO policy. The selected-owner phase uses the actual
owner’s pre-selection clock call. This identifies a pending receive/select
window; it does not claim a particular semaphore address or expose an internal
queue reservation. The std full-queue case does not cover adversarial
preemption inside an unpublished reservation.

Host execution verifies payload ownership, accounting, fairness, failure and
join behavior with the host’s scheduler. The mutex cases check protected data
and finite control flow, without asserting native donation or ordering.
Host reports have `domain` set to `host-functional`, null native schedule
fields, `native_waits: 0`, and `mutex.native: null` for the HML pair. Host nested
runs also retain null native configuration and holder snapshots.
Host publication reports retain null native configuration and paused-peer
snapshots. Their progress, lifecycle and allocation observations remain host evidence.
Host runs cannot establish NuttX PI or fixed-priority progress. Frozen historical
P1/P2 artifacts remain separate evidence for their exact source revisions.

## Build and inspect

Use the repository’s usual Rust and native target toolchain environment. The
two qualification profiles keep radios disabled and retain a normal NSH entry:

```sh
python3 tests/owner-qualification/workflow.py build \
  --profile esp32s3-ram-footprint --out /tmp/owner-s3-build
python3 tests/owner-qualification/workflow.py build \
  --profile pico2w-std-qualification --out /tmp/owner-pico-build

python3 tests/owner-qualification/workflow.py inspect --build /tmp/owner-s3-build
```

The commands above default to `--suite owner`. Build the mutex comparison as
two separate images using the same profile and toolchain, for example:

```sh
python3 tests/owner-qualification/workflow.py build \
  --profile pico2w-std-qualification --suite mutex-inherit \
  --out /tmp/mutex-pico-inherit
python3 tests/owner-qualification/workflow.py build \
  --profile pico2w-std-qualification --suite mutex-no-inherit \
  --out /tmp/mutex-pico-no-inherit

python3 tests/owner-qualification/workflow.py inspect \
  --build /tmp/mutex-pico-inherit
```

Repeat the pair with `esp32s3-ram-footprint` for WROOM qualification. `inspect`
returns only the cases admitted by that retained build. Capture rejects a
case from another suite; do not run both mutex expectations against one image
or relabel an older owner build as a mutex qualification.

Output directories must be new. `build` invokes the normal frontend with the
entry manifest, `owner_qualification` command, the test-only native source and
the explicit stack/configuration controls. It retains:

- The final ELF and selected firmware image, preserving `.bin` or `.uf2`.
- The normal frontend’s resolved configuration, toolchain/provenance, provider,
  ABI and patch receipts.
- Exact current source bytes and their hashes for this fixture, actual owner,
  controller, in-repo dependency/build support and the checked-in std/kernel
  patches, plus the indexed NuttX/apps revisions.
- A build identity computed before compilation and embedded in both the
  executable and firmware payload. Rebuilding produces a new build identity.

`inspect --build DIR` is a read-only admission check. It rehashes retained
sources and artifacts, verifies the selected runtime identity and emits JSON
with `build_id`, `build_sha256`, `source_manifest_sha256`, `domain`, `profile`,
`case_count`, `cases`, `image`, `elf` and `executable`. Each artifact entry gives
an absolute `path`, `relative_path`, `sha256` and `bytes`. The UF2 identity check
handles markers split across payload blocks. No import of this script is needed
by a campaign coordinator.

Older receipts keep their original case inventory. A receipt without an
explicit suite cannot admit any mutex case, and `latest-value-progress`
requires `src/scenarios/latest.rs` in its retained source manifest. Original
seven-case owner receipts and earlier host receipts remain replayable without
acquiring claims for added cases. `std-mutex-nested` similarly requires
`src/mutex_nested.rs`; earlier inherit-suite receipts retain their single case.
`publication-progress` requires `src/scenarios/publication.rs`; earlier
eleven-case host receipts do not acquire the new claim. A publication-suite
receipt without that source is rejected. Retained source inventories include
navigation, Motion Tracker and the diagnostic allocator in addition to the
fixture and its existing platform dependencies.
Receipts that predate the policy field keep their historical configuration
claims and are not relabeled as evidence for the new default.

These receipts bind the retained build/capture bytes. They do not independently
attest the physical board or operator’s flashing action. All results retain
`physical_execution_verified: false`, `timing_qualified: false` and
`production_qualified: false`.

## ARM QEMU execution

The emulator must contain QEMU's [MPS2 UART interrupt wiring fix](https://github.com/qemu/qemu/commit/5a558be93ad628e5bed6e0ee062870f49251725c).
The runner admits release versions 8.2.7 or later in the 8.2 branch, 9.0.3 or
later in the 9.0 branch, and 9.1 or later. It rejects older or unrecognized
versions before starting a capture. Ubuntu 24.04's QEMU 8.2.2 reverses the UART
receive/transmit interrupt wiring: the tested image prints the NSH greeting,
then repeatedly enters the receive handler while a transmit interrupt remains
pending. Increasing the console deadline does not repair that mismatch.
Version admission is a prerequisite; successful runtime captures are still
required, and historical receipt replay keeps its original rules.

CI builds only `arm-softmmu` from the official QEMU 8.2.7 archive using
[install-qemu.sh](install-qemu.sh), with a pinned SHA256 verified on downloads
and cache hits. The source signature was verified against the release key
published by QEMU. Configuration disables further downloads and optional
features; the build records its options, compiler, archive and executable hashes.
It leaves the system and ESP32 emulators unchanged. The native firmware build
and ABI gates still use the normal frontend. To use that same emulator locally,
install a C compiler, make, Ninja, pkg-config, Python with venv/distlib support, and the
glib, pixman, libfdt and zlib development packages, then run:

```sh
bash tests/owner-qualification/install-qemu.sh
```

Pass `--qemu target/owner-qemu-tools/qemu-system-arm` to the capture commands
below. Successful installation removes only its own temporary source/object
directory; a failed build retains that directory and its log for diagnosis.

With the ARM [NuttX std prerequisites](../../docs/nuttx-std.md) and
`qemu-system-arm` available, build the same mutex pair for `mps2-an521-mock`:

```sh
python3 tests/owner-qualification/workflow.py build \
  --profile mps2-an521-mock --suite mutex-inherit \
  --out target/owner-mutex-inherit
python3 tests/owner-qualification/workflow.py build \
  --profile mps2-an521-mock --suite mutex-no-inherit \
  --out target/owner-mutex-no-inherit
python3 tests/owner-qualification/workflow.py build \
  --profile mps2-an521-mock --suite publication \
  --out target/owner-publication

python3 tests/owner-qualification/qemu.py \
  --build target/owner-mutex-inherit --out target/owner-mutex-inherit-qemu
python3 tests/owner-qualification/qemu.py \
  --build target/owner-mutex-no-inherit --out target/owner-mutex-no-inherit-qemu
python3 tests/owner-qualification/qemu.py \
  --build target/owner-publication --out target/owner-publication-qemu
```

The runner accepts only that ARM profile and automatically selects the cases
admitted by the selected suite. Each case starts a fresh emulated NuttX
boot, checks the case and shell exit status, observes a final console tail,
and shuts QEMU down. A new output directory retains the source-bound build,
console, analysis, and capture receipt with `runtime: qemu`, emulator version,
and emulator executable hash. The runner replays the capture before reporting
success. Replay a retained result independently with its explicit artifact root:

```sh
python3 tests/owner-qualification/workflow.py replay \
  target/owner-mutex-inherit-qemu/runs/std-mutex-inherit \
  --artifact-root target/owner-mutex-inherit-qemu
python3 tests/owner-qualification/workflow.py replay \
  target/owner-mutex-inherit-qemu/runs/std-mutex-nested \
  --artifact-root target/owner-mutex-inherit-qemu
python3 tests/owner-qualification/workflow.py replay \
  target/owner-mutex-no-inherit-qemu/runs/std-mutex-no-inherit \
  --artifact-root target/owner-mutex-no-inherit-qemu
```

This exercises the NuttX scheduler and std mutex implementation in an emulator.
It does not establish physical-board execution, hardware timing, or a blocking
bound. The separate `mutex-qemu` matrix job in
[MPSC comparison CI](../../.github/workflows/mpsc-comparison.yml) builds and runs
the two mutex suites and the publication suite after host validation, retaining each suite's build and QEMU
evidence even when a step fails. Physical WROOM and Pico captures still use
the hardware profiles and procedure below.

## Fresh-boot capture and replay

The capture command never flashes or resets a board. First flash the exact
selected image using the existing board workflow and verify its identity.
Create one session JSON for each case, using values from `inspect`:

```json
{
  "schema": "owner-qualification-session-v1",
  "measurement_session": "board-label-and-campaign-label",
  "case": "owner-independent-inputs",
  "token": "unique-run-token-001",
  "build_id": "COPY_THE_64_HEX_BUILD_ID",
  "image_sha256": "COPY_THE_SELECTED_IMAGE_SHA256",
  "fresh_boot": true,
  "flash_verified": true
}
```

The token must contain 8–64 ASCII letters, digits or hyphens, and be unique to
the invocation. The session is an explicit operator assertion, not an automated
hardware attestation. Run capture, then reset the board when it reports ready:

```sh
python3 tests/owner-qualification/workflow.py capture \
  --build /tmp/owner-s3-build --case owner-independent-inputs \
  --session /tmp/owner-session.json --port /dev/ttyUSB0 \
  --out /tmp/owner-s3-independent-inputs

python3 tests/owner-qualification/workflow.py replay \
  /tmp/owner-s3-independent-inputs
```

Capture requires `pyserial`. It observes one fresh boot through the NuttX
loader banner or NSH greeting, then waits for the shell prompt and sends the exact
case/token command once, retains the complete console, waits for the result and
return to the prompt, requests the terminal exit status and observes a further
one-second tail. Before command submission it tolerates USB removal and
re-enumeration by reopening the selected port within the same 90-second capture
deadline; use a stable device path. It retains any bytes already received and
still requires exactly one boot banner. A disconnect after command submission
fails the run and never retries the command. A panic or native failure anywhere in retained output,
including after the success marker, fails admission. Missing or duplicated
records, changed identities, incorrect accounting/schedules, nonzero or
ambiguous status, a second boot, a missing prompt, timeout and incomplete
captures also fail. No finite capture can exclude failures occurring after its
retained observation window.

Each physical run is self-contained: it includes the exact build/source/image
receipt, session, raw console, capture digest and parsed analysis. `replay`
rehashes them and reruns validation, including late-failure rejection; it does
not execute a board or generate measurements. Failed captures retain their
console and failure receipt and never become a pass during replay.

## Local validation

```sh
python3 tests/owner-qualification/workflow.py host --out /tmp/owner-host
python3 tests/owner-qualification/workflow.py inspect --build /tmp/owner-host
python3 tests/owner-qualification/workflow.py replay \
  /tmp/owner-host/runs/owner-independent-inputs --artifact-root /tmp/owner-host
python3 -m unittest discover -s tests/owner-qualification -p 'test_*.py'
```

The host command builds the current release source with a bound identity, runs
all twelve cases in separate fresh processes and replays every retained result.
Parser controls use explicitly synthetic records to reject missing joins,
incorrect priority/phase evidence, host-to-target relabeling, wrong image or
source, late native failure, malformed sessions, duplicate output and altered
analysis. Mutex controls additionally reject incompatible suites/protocols,
missing donation or priority restoration, impossible acquisition order,
incomplete nested holder accounting, and missing second-round reuse.
Publication controls reject missing paused peers, lost freshness or terminal
state, changed comparison scope, allocator activity in a zero-call window,
discontinuous allocation accounting and incomplete reclamation.
They check evidence admission and do not create physical results.
