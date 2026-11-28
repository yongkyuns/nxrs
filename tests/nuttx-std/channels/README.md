# Native bounded-channel host controls

Run against a private SDK produced by the normal frontend:

```sh
python3 tests/nuttx-std/channels/run.py --prepared-sdk PREPARED --out NEW_OUTPUT
```

The runner verifies the exact queue patch, overrides its selector only in a
private copy, rebuilds std and checks Cargo's actual selected source. Nine
controls cover capacity/errors, producer FIFO, MPMC ownership, disconnect/drop,
timeouts, reentrant destruction, unrelated park permits, zero/unbounded channels
and notification after the completed owner has joined. The host supplies mutual
exclusion; target interrupt masking and native NuttX parking require separate
device qualification. No installed SDK is changed.

## Independent runtime comparisons

`compare.py` prepares one qualification SDK at a time from an already prepared
current production SDK. It does not add product backends or change production
preparation. The candidates isolate these changes:

| Candidate | Queue | Parker | Operation outlining |
| --- | --- | --- | --- |
| `baseline` | Current native transaction | Current native parker | Current |
| `upstream-queue` | Pinned upstream array, with two fixture observations | Current native parker | Current |
| `pthread-parker-init` | Current native transaction | Pinned upstream pthread parker plus raw mutex initialization | Current |
| `no-outlining` | Current native transaction | Current native parker | Remove only patches 0010 and 0011 |

The baseline must be `nightly-2026-11-26` or `esp-1.100.0.0`, with its complete
current source and patch receipts. The target runner currently selects the
single-core `mps2-an521-mock` ARM profile, so use the nightly SDK for these commands.
Use a new output directory for each candidate, outside the checkout:

```sh
python3 tests/nuttx-std/channels/compare.py prepare \
  --prepared-sdk PREPARED --candidate upstream-queue --out NEW_OUTPUT
python3 tests/nuttx-std/channels/compare.py build --out NEW_OUTPUT
python3 tests/nuttx-std/channels/compare.py run \
  --out NEW_OUTPUT --qemu /absolute/path/to/qemu-system-arm

NXRS_STD_COMPARISON_BASE=PREPARED \
  python3 -m unittest discover -s tests/nuttx-std/channels -p test_compare.py -v
cargo test --locked -p nxrs-build comparison_sdks --lib
```

The normal NuttX build prerequisites, including `olddefconfig`, ARM GCC and the
host Rust toolchain, must be on `PATH`. The build uses `cargo nxrs build`, the
selected profile, standard native-source staging and the normal compiled ABI
gate. If other work may rebuild the shared frontend concurrently, `build
--frontend /absolute/path/to/private/nxrs-build` accepts a stable copy of that
same frontend and records its hash. Each SDK records original/final source hashes, retained source bytes,
the exact reversed patches, a generated comparison diff and its baseline
receipt. A toolchain marker and matching receipt bind it to the exact
`qualification-only` fixture manifest; the normal frontend rejects its use by
another application. Build identity also binds the fixture source inventory,
and the firmware, native provenance, ABI results and serial transcripts are
hashed in the retained output. Compiler binaries may share disk storage with
the baseline; the complete Rust library is an independent copy.

Eight existing channel controls run unchanged on target, with explicit worker
stacks. The host-only delayed-notification injection remains in the host runner.
A ninth target control exercises pre-issued notifications, observes a real
native blocked wait, then wakes and joins the worker. It does not count retained
permits: `park` permits spurious returns, and the worker may return and repark
before the observer runs. This tests blocking and wakeup separately from the
bounded queue without claiming an exact permit-count oracle.

For `upstream-queue`, a second fresh boot forces an actual fixed-priority
preemption immediately after the upstream tail reservation, before message/stamp
publication. The producer runs at FIFO priority 120; the awakened receiver at
180 calls `try_recv`. A second observation inside the existing unpublished-slot
loop proves that the receiver spins there. The coordinator at 220 observes both
threads runnable, increasing loop counts and no completed publication, then
raises the producer to 200. The original send/receive operations must finish,
the exact payload must arrive, and both threads must join. A watchdog bounds a
broken fixture. A timeout, watchdog reset or successful log prefix followed by
a fault is a failed experiment, never accepted as the scheduling witness.

`passed: true` for that case means the **finite counterexample was reproduced**;
its result explicitly records `upstream_fixed_priority_safe: false`. The hook
creates the adversarial scheduling boundary and adds observations; it does not
sleep the producer or change the upstream queue's reservation, publication,
backoff or return conditions. This is evidence against adopting that queue for
arbitrary mixed-priority transfers, not a throughput benchmark or proof that
every current application would encounter the schedule.

ARM functional results for the pthread parker or outlining candidate do not
establish Xtensa wake latency, timeout behavior under device load, IRAM/shared
SRAM equivalence or physical real-time budgets. Retiring those production
patches still requires the independently changed candidate on the relevant
physical workload with its linked memory map and scheduling measurements.
The native parker, queue and outlining remain independently reviewable; a queue
failure does not prove that the larger native parker is necessary.

### Recorded ARM comparisons

These runs used Rust nightly `2026-11-26` (compiler commit
`6c709eb428d472a3c3d93681c1ba37d44a1c27cf`), NuttX 13.1.1 and QEMU 10.0.13
on `mps2-an521-mock`. The upstream array preimage SHA256 is
`3b7cce4fa348ff357658c44fa9def9bffe7ed6ff97a6ab4be74da3a0350e804c`.

| Candidate | Native ABI checks | Target functional controls | Reservation/publication result |
| --- | --- | --- | --- |
| `upstream-queue` | 130 passed | 9 passed | Fixed-priority hazard reproduced; priority-only rescue and both joins passed |
| `baseline` | 130 passed | 9 passed | Not instrumented |
| `pthread-parker-init` | 130 passed | 9 passed | Not instrumented |
| `no-outlining` | 130 passed | 9 passed | Not instrumented |

The queue build identity is
`bf28cc2bd85a3f95ea88f6b74579991764430622e174c8e0868e96dfda696c05`;
its comparison SDK identity is
`efbeb773d06eb1d13a78c5dbcff070cb9fb29a7a69bb638756e9a042629d65ed`.
The linked ELF SHA256 is
`a97d2ea0f1ed3e5f87af18ec369cb45e237f9eb126907b63a63d20758291b125`.
The retained fixture source SHA256 is
`c1306f63e7d09a367864b2038ba9d7d3132c95ae5798b5e960957478ad7e2d68`.

The receiver's unpublished-slot observations increased from 7,373 to 83,841
while publication and both operations remained incomplete. Promoting only the
producer allowed delivery of the exact payload and both joins. Both cases used
fresh boots, rejected an invalid command and exited QEMU cleanly. This was the
capture before correcting the separate parker control's printed description;
its retained snapshot/log contains the earlier “coalesced permits” label. That
label does not establish an exact permit count, and the current description
above states the narrower observed contract. The reservation algorithm and
oracle are unchanged by that wording correction.

The other three captures use the corrected fixture source SHA256
`8082828adcf2fb08a0ac859e697e4a2cebc1c8e2d54795df7090046e2bfdf45b`.
Each passed all nine functional controls, rejected the invalid command, and
exited QEMU cleanly after a fresh boot. Their retained identities are:

- `baseline`: build
  `a11d085d945119c6ebc924ce4bca4b8f8690395acdf7f3e3eed6398671862cea`, SDK
  `ecf87184b6d490d1e37cd5db6bccf3442c08afc47bedf941b63cafbb621e944e`, ELF
  `c7f8995a2cddd98fb8570ba162f6fe725e9bc7fef2c0f1f668abeea2cb5ac05e`.
- `pthread-parker-init`: build
  `50028894435525d881432cd6ce171c56686e8fa7f60efa8dd193a958cdf181a7`, SDK
  `6efd6b29077785c2ce332cd001b206e58b66992c16e92d5dacebc5f9f1e0b3e3`, ELF
  `90167350179a38fee58c94d6f08c21bf45d2d8f7da76378ef5948d5430333750`.
- `no-outlining`: build
  `169367ff48d0af50df82dd40eefe9ae1d3c2593b9ce3039a584f16e0893087e2`, SDK
  `fb4fae5cecfe1bc9685415a3b280d9fab094eccb5a9a614cc04bc630c5bd2256`, ELF
  `e2314f6f35ea181e4f08a2b8640611f5c5ee9e8fa2bb5a5d98ee6a2d520294e6`.

The pthread candidate retains the pinned upstream body with SHA256
`0262635045e33a18a0f529739ae696c32266dc4d1d4dcfc64054ffbae69b5765`
plus only the NuttX raw-mutex initialization. Its functional result supports
continuing that narrower qualification independently of the queue. The
outlining result likewise establishes functional compatibility for these ARM
controls. Neither result retires a production patch or establishes the remaining
Xtensa latency, timeout-under-load, IRAM or shared-SRAM budgets.

## Physical measurement fixture

New builds use the canonical
[`tests/mpsc-comparison`](../../mpsc-comparison/README.md) fixture. The patches
below document reproduction of the older frozen cohort; they are already
incorporated in the maintained Rust workload.

The [entry-selection patch](mpsc-fixture-entry.patch) applies to the frozen MPSC
fixture at `a8a1b1baa05aeca76264e882ed31c3b2baa9973d`. Its executable entry selects
std on NuttX and the native queue on Zephyr. The timed engine, arrival schedules,
metrics and native adapters remain shared. Retain the older mixed NuttX image
for its separate native diagnostic; carrying both transports in the measured
image inflates its memory cost. See the
[event-driven performance analysis](../../../docs/std-channel-performance.md).

The complete base fixture, observer overlays and raw device captures are
retained privately; `tests/mpsc-comparison/` is not included in this checkout.
These patches make the fixture corrections inspectable, but do not by themselves
reproduce the hardware tables. The host controls above are a separate executable
validation of the queue body.

The [phase-ordering patch](mpsc-fixture-phase.patch) moves endpoint priority and
policy queries after the existing retirement barrier, once timing and CPU
counters have closed. Priority validation and joined cleanup remain required.
Earlier captures include these diagnostics before the final message is received.
The corrected ordering passed both physical matrices and three fresh boots of
the four-producer paced case per runtime; see the measurement analysis.

Apply the same [fixture IRAM fragment](mpsc-fixture-iram.ld) to both kernels,
alongside their native queue/wait/scheduler IRAM policy. It covers workers,
transport adapters, clock/gate wrappers and scheduled-release callbacks. Verify
the linked symbols and their successful bounded-operation callees, including
literal-loaded Xtensa calls; a section selector alone is not proof. Native
callbacks must resolve to the same qualified hot path. Setup, reporting,
retirement diagnostics, reclamation and unselected channel flavors retain normal
placement. Match logical operations rather than compiled byte counts; record
each image's total IRAM and shared-SRAM cost separately.
