# P2 event-path timing ladder

This implements the transport and scheduling controls from
[PR #252](https://github.com/yongkyuns/nxrs/pull/252) and the
[P2 benchmark plan](../../docs/event-path-benchmarks.md#p2--a-small-timing-ladder-that-isolates-useful-costs).
It measures the actual `messaging` facade and pinned Crossbeam 0.5.17 with
statically dispatched Rust callers. It does not change a production transport.

## Experiment contract

The baseline is a checked 16-byte payload and capacity eight. The separately
named `imu16` profile carries the real `imu::ImuSample` at capacity sixteen;
each executable records its target `size_of` and alignment. Neither profile
acquires hardware. Both validate all meaningful payload fields and preserve
provider/sequence identity. IMU padding is never hashed or interpreted.

| Family / command | Measured operation and controls |
| --- | --- |
| `ready-{std,messaging,crossbeam}-{try,send}` | Same-thread accepted admission followed by receive. Record admission and receive boundaries separately. `send` and `try_send` are distinct experiments. |
| `full-{std,messaging,crossbeam}` | Fill exactly to capacity outside the timed call, time `try_send`, require the original rejected value, then verify every retained queue entry and FIFO order. Only the rejected call enters the admission counts. |
| `blocked-{std,messaging,crossbeam}-{try,send}` | Empty receive followed by one admission; same worker stack and scheduling relationship. Fresh native `WAIT_SEM` proof for every target iteration, including warmup. |
| `ready-select-{one,three}`, `blocked-select-{one,three}` | Compare Crossbeam direct receive with one-arm and three-arm `select_biased!`. The ordinary queue is active; idle stop/important arms stay connected. |
| `semaphore` | Sequence publication plus native semaphore post, ending at return from `sem_clockwait`. This has zero message payload and is a notification control. |
| `park` | Sequence publication plus `unpark`, ending at the actual `park` return that satisfies the predicate. Count park returns separately from useful completions. |
| `fixed-peer` | Known-peer marker after sequence publication and `yield_now`, with two distinct equal-priority FIFO tasks and a native peer-ready observation. Host execution refuses this contract. |
| `fanin-{std,messaging,crossbeam}-{1,4}` | One/four task-context providers issue finite nonblocking bursts into one shared class queue. Retain accepted and rejected attempts and reconcile actual handling. |

The selector follows the Wi-Fi fixture's stop → important → ordinary ordering.
One-arm selection and direct receive use the same one-queue topology. Three-arm
selection has additional stop capacity one and important capacity N, alongside
ordinary capacity N. That topology cost must not be described as selector-only
overhead. This P2 adapter does not claim the complete P3 owner, fairness,
deadline, closed-arm disabling or registration-race qualification.

All target notification/blocked cases use FIFO sender 120, receiver 180 and
16 KiB worker stacks. The fixed-peer workers both use FIFO 120. The observer
uses FIFO 220 where needed and an independently runnable watchdog uses FIFO
240. Every requested policy/priority is read back. The reused native adapter
refuses SMP or non-flat NuttX at compilation. Linux reports its actual policy
and never invents target blocking, single-core execution or fixed-priority proof.

## Timing boundaries and lifecycle

Setup → excluded warmup → retained work → terminal quiescence → cancellation
and joins is explicit. Payload construction, setup gates, validation, allocation
and console output are outside the named timed calls. Samples are preallocated.
Transport and admission dispatch is monomorphized before the loop; there is no
dynamic transport call inserted into every measurement.

`start_ticks` → `admission_return_ticks` is caller-visible elapsed admission
time. It can include a higher-priority receiver's execution. `start_ticks` →
`receive_return_ticks` is end-to-end delivery. Ready cases also retain the
receive entry timestamp. Notification controls include their sequence
publication, as their operation names explicitly state. Fixed-peer markers
prove that the named peer ran; they do not measure exact intervening switch
counts or pure register-save/restore cost. No result divides a round trip by two.

The receiver announces a fresh sequence immediately before entering the measured
receive/selection/notification wait. On NuttX, the observer requires the matching
unfinished sequence in `TSTATE_WAIT_SEM`. An announcement or setup sleep alone
never qualifies a blocked sample. The final receiver stays behind a separate
gate until the sender records its final return, so thread destruction cannot
enter that last sample. Errors close the measured endpoint, release all other
wait sites and join. Shutdown never requires admission into a full data queue.

Park notifications use Release/Acquire sequence predicates. Early, stale,
coalesced and spurious tokens do not count as delivered work; tests cover
publication between predicate checking and a real `park`, cancellation and
partial-run cleanup. The native counter is initialized once before readers,
including during parallel host tests. Raw 32-bit timestamps have conservative
monotonic bounds and wrap/ordering checks; these are not clock calibration.

Finite fan-in preallocates a record for each attempted sequence. The owner
drains concurrently with `recv_timeout(10 ms)` to observe completion/cancellation
without requiring a sentinel slot or dropping worker resources during active
timing. Its exact receive API is recorded. Admission is published after
`try_send` returns, so the higher-priority owner does not wait for a preempted
producer's completion flag. Every provider's TID/policy/priority is recorded.
Duplicate, corrupt, missing and unadmitted delivery fail reconciliation. Workers
hold at terminal gates until active work is complete. The finite case is capped
at 256 retained attempts and 64 warmups per provider; it is not a sustained
offered-load or coordinated-omission benchmark.

## Build and run

From the repository root:

```sh
cargo +1.90.0 test --locked -p nxrs-event-path-perf
cargo +1.90.0 clippy --locked -p nxrs-event-path-perf --all-targets -- -D warnings
python3 -m unittest discover -s tests/event-path-perf -p 'test_*.py' -v
CARGO_PROFILE_RELEASE_OPT_LEVEL=2 CARGO_PROFILE_RELEASE_LTO=false cargo +1.90.0 build --locked --release -p nxrs-event-path-perf
python3 tests/event-path-perf/run-host.py --binary target/release/event-path-perf --output target/event-path-host
```

The host corpus executes all 27 supported named cases, eight normalized IMU
controls and the expected fixed-peer refusal. Every run gets a new directory
with raw stdout/stderr, a capture receipt and analysis. Reusing an output
directory is rejected. Individual runs accept `CASE SAMPLES WARMUP PROFILE`:

```sh
target/release/event-path-perf blocked-messaging-try 128 16 bytes16
target/release/event-path-perf ready-messaging-try 128 16 imu16
python3 tests/event-path-perf/capture.py host --binary target/release/event-path-perf --case park --samples 128 --warmup 16 --output target/event-path-park
```

With the pinned SDK and target prerequisites from the existing std-channel
fixture installed:

```sh
python3 tests/event-path-perf/build.py --platform mps2-an521-mock --out target/event-path-arm
python3 tests/event-path-perf/capture.py qemu --build-directory target/event-path-arm --case fixed-peer --samples 16 --warmup 4 --output target/event-path-arm/runs/fixed-peer
python3 tests/event-path-perf/build.py --platform esp32s3-service-footprint --clock-source native-counter --out target/event-path-esp32s3
```

Each target build gets a private prepared std copy and Cargo directory. The
builder reuses P0's source/configuration checks in uninstrumented `latency` mode.
It additionally checks the selected Crossbeam and Crossbeam-utils dependency
edges, exact Cargo.lock registry checksums, every unpacked source byte against
the downloaded archives, and actual compiler-artifact paths. Instrumented or
changed source refuses the build. Compiler, configuration, source and image
identities are retained in `event-path-build-v1`.

The [dedicated workflow](../../.github/workflows/event-path-perf.yml) runs the
host corpus, builds ARM, runs representative cases in separate fresh QEMU boots,
and builds the ESP32-S3 image. It does not flash or take physical-board ownership.

## Evidence and pending acceptance

`analyze.py` accepts one ordered `event-path-perf-v1` run. It rejects malformed,
duplicate or mixed records; relabeled APIs/topologies; missing attempts;
incorrect payload checksums; invalid timestamps; fabricated scheduler/wait
proof; absent joins/final holds; and attempts to promote timing or progress
qualification. Retained checksums are independently reconstructed. Counts
include warmup; fan-in's excluded, nondeterministically admitted warmups mean
its whole-run checksum may be firmware-validated without independently
reconstructible warmup detail, which analysis reports explicitly.

Capture reuses P0's console I/O, fresh-boot and independent-clock checks without
editing P0. Serial sessions use schema `event-path-session-v1` with the same
physical identity/calibration fields described in the
[P0 fixture](../std-channel-perf/README.md). Build ID, image hash, clock request,
CPU frequency and source receipts must agree. Artifact hashes are checked before
and after execution. Clock rejection and ROM digest warnings remain visible
without erasing valid functional data. Raw output and failed receipts survive
console deadlines, fixture refusal and serial-cleanup errors.

All executable timing/progress qualification fields remain false. Raw-tick
distributions are characterization, not a speedup, WCET or production acceptance
claim. Physical captures still require P0's corrected independently calibrated,
phase-covered cohort and resource/placement review. P1's no-repair progress gate
is required before production transport adoption. P3 owns full selected-owner
behavior, P4 owns correlated native switch/IRQ attribution, and P5 owns sustained
offered-load and resource/product budgets. The IRQ/timer-to-provider acquisition
boundary remains unqualified; synthetic task-context fan-in is not IRQ evidence.

This work owns `tests/event-path-perf`, its dedicated workflow, and the minimal
workspace member/lock entry. Existing P0 capture/timing code, P1 source hooks,
production modules, shared architecture documents and archived physical evidence
remain under their existing owners.
