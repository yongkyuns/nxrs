# Physical std-channel measurements and a priority-progress witness

This fixture exercises ordinary Rust `std::sync::mpsc::sync_channel` on nxrs. It
adds the missing blocked-receiver measurement and forces a specific
reserve-before-publication interleaving in the **actual pinned std source**.
It changes no production transport. The [research and optimization
sequence](../../docs/std-channel-performance.md) explains why pthread parking,
semaphore wakeups and scheduling are the first shared performance targets.

| Image / command | Purpose | Expected result |
| --- | --- | --- |
| `latency` | Uninstrumented std; sender timestamps a transfer after independently observing the receiver blocked in `recv` | Every NuttX sample has a verified blocked receiver, valid payload, bounded counter interval and joined threads |
| `witness-reserved` | Private std hook after successful tail reservation, before slot publication; the hook independently wakes a higher-priority consumer | `reservation-stall-observed`; the consumer's `try_recv` does not return until priority recovery lets the producer publish |
| `witness-published` | Control hook after release publication, before normal channel notification; same independent wake | `published-slot-control-completed`, without recovery |

**A successful reservation witness reproduces a failed progress contract.** It
never qualifies the channel for arbitrary fixed-priority use. The published
control proves that control interleaving only. Neither witness image supplies
latency measurements. Host and QEMU runs qualify mechanics, not MCU performance.

The [recorded November 2 cohort](results/2026-11-02-qemu/README.md) includes
64/64 blocked-receiver observations, the reproduced actual-std reservation stall,
and its published-slot control. It preserves two rejected clock measurements
and the corrected rebuilds. The report identifies the tested source inventory,
its later integration/patch renumbering, and separate physical-target build-only
records; it establishes no physical performance ranking.

The [physical ESP32-S3 N16R8 cohort](results/2026-11-02-esp32s3/README.md)
records 3,000 verified blocked receives across three fresh boots, the actual-std
reservation stall and its published-slot control. Raw counter results are
retained; timing qualification remains withheld without independent CPU-clock
calibration. This cohort uses CI firmware whose input hashes match the integrated
PR head, separately from the earlier frozen QEMU builds.

## What is measured and proved

The channel has eight slots and a 16-byte Rust value. Payload validation uses a
sequence and three check words. Setup and sample storage allocate before the
timed loop; the receiver's initial channel wait may allocate std context/waiter
state. Warmup is recorded and excluded from the retained sample distribution.

For each latency sample the higher-priority receiver passes a separate setup
gate and announces that it is entering `recv`. The sender observes that thread's
actual NuttX TCB in `TSTATE_WAIT_SEM`, while the receiver has not completed that
sequence and the sender is outside the channel. The receiver performs no other
explicit wait between its announcement and `recv`. This proves a native wait
**inside the receive call**; it does not identify the private pthread condition
variable's address or claim a count of the parker's internal operations. A
self-reported flag or a preceding sleep alone cannot pass this check.

The retained boundaries are:

- **Send to receive return:** counter immediately before `send` to counter
  immediately after `recv` returns. This includes publication, wakeup, scheduling,
  receive completion and counter-access overhead.
- **Send call:** counter before `send` to counter after it returns. The receiver
  has higher priority and can run before the sender returns; this duration can
  therefore include receiver execution. It is not pure enqueue or context-switch
  cost.

Message construction, blocked-state observation, next-iteration gates, sample
serialization and UART output are outside these boundaries. All successful
output occurs after worker joins. Raw samples are retained; analysis computes
nearest-rank p50/p95/p99 and observed maximum, without subtracting estimated
counter overhead or dividing a round trip. Maxima and p99 are not WCET.

The witness has a low producer at FIFO priority 120, consumer at 180, observer at
220 and deadline watchdog at 240. Both workers are independently observed waiting
on their initial gates. Only the actual std hook posts the consumer's gate. The
observer wakes after 100 ms and requires at least 50 ms since consumer entry.
The reservation case must show both workers ready at their original priorities,
no completed send/receive, and a producer hook that has not returned. The observer
then raises the producer to 200 and lowers the consumer to 100, allows publication
and delivery, and joins both threads before clearing the hook pointer.
`yield` is not used as a supposed donation to a lower priority.

Native gates and the watchdog use monotonic deadlines. A separate host capture
deadline retains failed or incomplete transcripts. It can terminate its owned
QEMU child; a failed physical target may require a board reset. No thread, gate,
channel or ISR path is added to production code.

## Build prerequisites and isolation

Use the prerequisites in the existing [RTOS benchmark
recipe](../../docs/rtos-benchmarking.md) and [NuttX std
guide](../../docs/nuttx-std.md). Initialize the pinned NuttX submodules. ARM uses
`nightly-2025-09-15` with `rust-src` and `arm-none-eabi-gcc`; host checks use Rust
1.90.0. ESP32-S3 uses the hash-verified SDK installed by
`bash tools/install-qemu-tools.sh`, plus the existing kconfig/esptool prerequisites.

Each firmware build and capture requires a **new output directory**. `build.py` prepares a
private SDK, applies the existing NuttX std/libc ABI fixes, and builds through the
normal `tools/build-nuttx-std-app.sh` entry. It never edits an installed SDK.

The complete `array.rs` preimage is pinned by SHA-256 and Git blob identity for
both supported SDKs. Latency preserves those channel bytes. Reserved and
published images add one distinct hook to the actual array implementation;
source drift or a second application is rejected. The build records compiler
and private library identities, the source inventory, resolved configuration,
ELF and flash-image digests, and a build ID emitted by the running fixture.
Source changes during compilation invalidate the receipt.

CI discards private SDK copies and Cargo caches after successful checks; receipts,
source inventories, patches, firmware and logs remain available.

### Functional checks on a host

```sh
python3 -m unittest discover -s tests/std-channel-perf -p 'test_*.py' -v
cargo +1.90.0 fmt --check -p nxrs-std-channel-perf
cargo +1.90.0 clippy --locked -p nxrs-std-channel-perf --all-targets -- -D warnings
CARGO_PROFILE_RELEASE_OPT_LEVEL=2 CARGO_PROFILE_RELEASE_LTO=false \
  cargo +1.90.0 build --locked --release -p nxrs-std-channel-perf
mkdir -p target/std-channel-host
timeout 30 target/release/std-channel-perf latency 64 8 \
  > target/std-channel-host/console.log
python3 tests/std-channel-perf/analyze.py target/std-channel-host/console.log \
  --expect-outcome host-functional-only
```

Linux does not pretend to prove a NuttX TCB wait, and refuses the fixed-priority
witness. These checks require no realtime host privileges.

### Fresh QEMU boots

```sh
for mode in latency witness-reserved witness-published; do
  out="target/std-channel-perf/mps2/$mode"
  python3 tests/std-channel-perf/build.py \
    --platform mps2-an521-mock --mode "$mode" --clock-source baseline --out "$out"
  python3 tests/std-channel-perf/capture.py qemu \
    --build-directory "$out" --mode "$mode" --samples 64 --warmup 8 \
    --output "$out/run"
done
```

The runner owns one emulator and sends one command after a fresh NuttX startup
or NuttShell banner and shell prompt. It requires the complete result and return
to the prompt. Once preflight checks pass and capture begins, it retains
`console.log` and `capture.json` even on failure; successful analysis is in
`analysis.json`.
A captured reservation stall is intentional negative evidence for the channel's
progress contract. QEMU output always has `physical_timing_qualified: false`.

## Physical board recipe

Use `pico2-mock` for a Pico 2 or `esp32s3-service-footprint` for the existing
ESP32-S3 **N16R8** test-board configuration. The latter selects a single 240 MHz
core and PSRAM; it is not a generic profile for every ESP32-S3 board. Select and
record the actual memory placement, caches and workload. The native TCB proof
requires a flat, single-core kernel; other builds are rejected.

```sh
for mode in latency witness-reserved witness-published; do
  python3 tests/std-channel-perf/build.py \
    --platform esp32s3-service-footprint --mode "$mode" \
    --clock-source native-counter \
    --out "target/std-channel-perf/esp32s3/$mode"
done
```

`--app-opt-level 2` is the default. `--kernel-optimization baseline` retains the
profile's kernel optimization and diagnostics. For a separate speed experiment,
use `--kernel-optimization o2` and a separate output directory; the builder
verifies that `-O2` was actually resolved. Do not silently remove assertions or
compare different clock/cache/memory configurations. Full flags remain in the
build provenance.

Flash the image identified by `build.json` using the board's existing verified
flashing procedure. The capture tool does not flash, erase or provision a device.
Create a session JSON alongside the run, with actual values and verification
notes; this example is a template, not measured evidence:

```json
{
  "schema": "std-channel-session-v1",
  "runtime_kind": "physical",
  "measurement_session": "REPLACE-with-session-and-repeat-id",
  "board": "REPLACE-with-board-revision",
  "board_serial": "REPLACE-with-physical-device-id",
  "image_sha256": "REPLACE-with-flashed-image-digest-from-build.json",
  "active_cores": 1,
  "cpu_clock_hz": 240000000,
  "clock_verification": "REPLACE-with-frequency-calibration-method-and-observation",
  "cache_state": "REPLACE-with-actual-cache-configuration",
  "memory_placement": "REPLACE-with-flash-internal-RAM-and-PSRAM-placement",
  "power_management": "REPLACE-with-fixed-frequency-and-sleep-settings",
  "external_load": "REPLACE-with-interrupt-radio-and-other-thread-load",
  "flash_verification": "REPLACE-with-readback-or-verified-flash-receipt"
}
```

Then capture one run:

```sh
python3 tests/std-channel-perf/capture.py physical \
  --build-directory target/std-channel-perf/esp32s3/latency \
  --mode latency --samples 1000 --warmup 100 \
  --port /dev/ttyACM0 --session target/session.json \
  --output target/std-channel-perf/esp32s3/latency/run-01
```

Reset the board **after** the runner says it is ready; it requires a new boot
banner and shell prompt. The NuttX startup banner also works when the optional
NuttShell greeting is disabled. Run each witness with its matching firmware,
`--mode`, and updated session
image digest. Use a new capture directory and fresh boot for every repeat.
This also avoids treating repeated NSH invocations as a qualified std-runtime
restart scenario. No std or Crossbeam channel is invoked from an ISR.

### Clock and interpretation limits

The baseline clock is the native NuttX performance accessor, which may use
scheduler ticks. If that accessor has no initialized frequency, the fixture
explicitly reports a `CLOCK_MONOTONIC` fallback. These coarse-clock results remain
useful for behavior and are not accepted as physical latency qualification.
Explicit `native-counter` uses the target
hardware cycle counter on the supported physical boards, with the configured
clock frequency recorded. Verify that frequency physically and keep it fixed.
The fixture's counter-versus-monotonic sanity probe detects some configuration
errors; it is not independent clock calibration.

Every sample retains the low 32-bit stamps and an independent monotonic upper
bound on the complete interval. Analysis rejects intervals that could span half
the counter range, reversed/ambiguous timestamps, or counter deltas exceeding
the wall-time bound. It also rejects missing blocking proof, inconsistent payloads,
wrong FIFO priorities, stale build IDs, changed ELF/image/config bytes, missing
samples and mixed runs. A counter that does not advance or cannot resolve the
interval cannot produce qualified timing. Cold runs with zero warmup are retained
but excluded from the steady-state timing qualification flag.

Physical identity, flash verification and frequency calibration remain explicit
**operator-supplied evidence**, labeled as such in the output. The qualification
flag expresses these documented prerequisites, not a claim that software
independently authenticated the physical board or established a hard deadline.
Keep raw console, session, build and analysis records together.

These cases establish measurement infrastructure and a specific progress witness.
They do not yet establish a PX5/ThreadX/Zephyr performance ranking. For that next
step, match hardware, API semantics, payload/capacity and timing boundaries, and
also measure ready-path throughput, sender blocking, overload, memory, service
load and energy when claiming an energy benefit. Use the existing
[hot-path benchmark](../rtos-bench) alongside this blocked-path fixture.
