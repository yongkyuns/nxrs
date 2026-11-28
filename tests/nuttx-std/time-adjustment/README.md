# Public time adjustment qualification

This fixture qualifies the repaired NuttX software clock through its public `clock_settime()` and `adjtime()` interfaces for [NXRS #490](https://github.com/yongkyuns/nxrs/issues/490). It follows the [phase 1 characterization](../clock-control/README.md), whose original images, captures, and failures remain separate evidence.

[Rust](src/main.rs) owns the workload and uses `SystemTime`, `Instant`, `thread::sleep`, a bounded rendezvous channel, and a joined worker. The shared [native helper](../clock-control/native.c) translates the configured NuttX ABI for clock controls that Rust `std` does not expose. This image enables both `CONFIG_CLOCK_TIMEKEEPING` and `CONFIG_CLOCK_ADJTIME`, so the helper uses the actual public declaration from `sys/time.h`; its phase 1 private diagnostic declaration is inactive.

The [manifest](Cargo.toml) defines an independent, dependency-free workspace marked `qualification-only = true`. Its NuttX command is `time_adjustment`, with priority 100 and a 32 KiB entry stack. Host execution refuses to run and exits with status 2.

## Backend contract

[Patch 0150](../../../upstream/nuttx/patches/0150-clock-timekeeping-adjustment.patch) makes realtime reads and committed updates project the same correction from elapsed raw timer counts. Its software slew has a **500 ppm cumulative budget**, preserves fractional progress, and keeps the underlying monotonic counter unchanged. Nanosecond quantization introduces less than 1 ns of rounding error; this is not an exact instantaneous slope guarantee over every individual short sample.

Queries, replacement, and cancellation first materialize elapsed progress. A null `delta` preserves the request; a zero `delta` cancels its remaining part. Both returned `timeval` fields are initialized, have consistent signs, and satisfy `abs(tv_usec) < 1_000_000`. Sub-microsecond pending amounts round away from zero so a query does not report completion early. Checked conversions reject invalid or overflowing requests before replacing state. Public get/set operations propagate software-clock failures, and a failed software set does not proceed to an RTC write.

The patch admits `CLOCK_ADJTIME` through `CLOCK_TIMEKEEPING` while retaining its disabled default. The software `adjtime()` symbol is present only when that option is enabled. This fixture exercises the flat MPS2 image; public header admission alone does not qualify protected syscall execution or another board's timer.

## Reproduce

Run from the NXRS repository root with the pinned submodules initialized. Use the host Rust toolchain in [`rust-toolchain.toml`](../../../rust-toolchain.toml), currently 1.100.0, and the target `nightly-2026-11-26` toolchain with `rust-src`. ARM GCC/binutils/newlib, `make`, `kconfig-conf`, `bison`, `flex`, `gperf`, `genromfs`, Python with `kconfiglib`, and `qemu-system-arm` must be available. The native host regression also needs an LP64 host C compiler with UndefinedBehaviorSanitizer support. SDK preparation follows the normal [build system](../../../docs/build-system.md).

```sh
git submodule update --init
rustup toolchain install nightly-2026-11-26 --profile minimal --component rust-src
export NUTTX_STD_COMPAT_FIXES=1

python3 -m unittest discover -s tests/host -p 'test_clock_timekeeping.py' -v
python3 -m unittest discover -s tests/nuttx-std/time-adjustment -p 'test_oracle.py' -v

python3 tests/nuttx-std/time-adjustment/workflow.py build \
  --tick-us 100 --out target/time-adjustment/public-100us
python3 tests/nuttx-std/time-adjustment/workflow.py run \
  --build target/time-adjustment/public-100us
```

The [workflow](workflow.py) invokes `cargo nxrs build` with the existing `mps2-an521-mock` profile, supplies the mechanical C helpers through `--target-c-source`, and validates the effective configuration, target ABI, and applied patch receipt. The target is `thumbv8m.main-nuttx-eabi`; RTC support is disabled. The explicit `--tick-us 100` above matches the default. A different tick interval is a separate experiment and must satisfy the fixture's observation-resolution checks.

Use fresh output directories: builds and captures are not overwritten. `run` starts three fresh QEMU boots by default; `--repeat 1` or `--repeat 2` selects fewer. The target's assertions, shell status, and host analysis must agree. Read `analysis.json` for `characterization_complete` and `qualified`; a build or console success marker alone does not establish qualification.

The [native host regression](../../host/test_clock_timekeeping.py) obtains sources with `git show` at the repository's NuttX pin, applies the registered series in temporary directories, and compiles the actual timekeeping and public-wrapper files. It compares the original failures with the repair at 1,000, 100, and 1 us ticks, including overflow, aliased input/output, query, replacement, cancellation, and error propagation. A separate build disables adjustment. Its deterministic timer and RTC stubs establish arithmetic and state behavior; target ABI, interrupt timing, and physical RTC behavior require other evidence.

## Controlled QEMU conditions

The target experiment uses two explicit controls:

| Control | Meaning |
| --- | --- |
| [SysTick constructor helper](../clock-control/qemu_systick_clock.c) | Supplies 20 MHz to NuttX's SysTick constructor for QEMU AN521, whose emulated clock differs from the pinned board's 32 MHz declaration. The workflow records the helper and `--wrap=systick_initialize` linker override. |
| `-icount shift=3,sleep=off` | Advances QEMU virtual time by 8 ns per guest instruction without host-clock pacing. The workflow adds and records these options for every capture. This is not a cycle-accurate CPU model. |

The independent reference is [CMSDK APB timer 1](../memory-timing/qemu_timer_clock.c), running at 20 MHz in the emulated machine. It has its own counter and measurement path, while both timers run within the declared QEMU virtual-time environment. It is not an external UTC or physical-oscillator reference. The source evidence and timer ownership are described in [memory qualification](../../../docs/nuttx-memory-qualification.md#clock-evidence-and-rejected-attempts).

The APB counter wraps after about 214.7 seconds. The fixture starts a fresh reference epoch and bounds the entire invocation below 60 reference seconds. Keep these captures distinct from phase 1 runs without instruction-count timing, and from physical-board measurements. Neither control calibrates a production board or establishes CPU latency, oscillator accuracy, or power behavior.

## Workload and checks

### Six equal dense observation phases

Every phase samples for at least **4.5 APB seconds**, with its final read ending the interval. Dense loops retain read counts, backward-read counts, minimum realtime increments, maximum read spans, and four pending-adjustment checkpoints at approximately one-second intervals. They do not allocate or print inside the loop. Individual dense read pairs are not retained; the console contains their aggregate observations and checkpoints.

| Phase | Control under observation |
| --- | --- |
| `baseline` | No pending correction |
| `positive` | +2 ms from a whole-second seed |
| `negative` | -2 ms from a whole-second seed |
| `near_second` | -2 ms from a seed 100 us before a wall-second boundary |
| `replacement` | Start +20 ms, wait 1.1 s, then replace the remaining correction with -2 ms |
| `cancelled` | Start +20 ms, wait 1.1 s, then cancel the remaining correction |

At 500 ppm, a 2 ms correction requires four seconds of raw elapsed time. The fixture compares pending progress against that elapsed-time budget, requires a zero residual at the end, and compares the realtime-versus-monotonic change with the requested correction. Replacement and cancellation must return the previously pending amount without undoing progress already observed. Every dense phase requires zero observed realtime and monotonic regressions.

Additional controls request and cancel **+2.500123 s and -2.500123 s** to exercise both signed `timeval` fields; they do not wait for those larger corrections to finish. Another control verifies that setting wall time clears a pending adjustment.

### Four concurrent waits

A worker supplies a start sample through a rendezvous channel, sleeps, and returns an end sample when joined. The main thread waits 50 ms after receiving the start sample before changing the clock. Both the start and completion of the clock action must lie between the worker's recorded endpoints; both are retained in the transcript.

| Wait | Requested sleep | Clock action |
| --- | ---: | --- |
| `positive` | 1.25 s | Request +2 ms |
| `negative` | 1.25 s | Request -2 ms |
| `forward` | 250 ms | Step realtime forward by 3 s |
| `backward` | 250 ms | Step realtime backward by 3 s |

The slew waits compare the endpoint correction with the amount expected during the worker interval and check pending progress after joining, requiring more than 500 us of progress by that query. Together these checks distinguish applied correction from a request waiting to take effect. The step waits require the expected signed realtime jump while `Instant` and APB elapsed time continue to agree. An intentional backward step is permitted in these cases; it is distinct from an unrequested reversal during slew.

Sleep duration is measured against APB. The lower bound allows one tick of observation uncertainty; the upper bound allows 2% beyond the requested duration plus the recorded read uncertainty. These are this fixture's acceptance bounds, not a universal scheduling guarantee.

### Uncertainty and smoothness bounds

Each paired `Instant`/`SystemTime` read is bracketed by APB reads. Endpoint comparisons use two ticks plus both bracket spans as their uncertainty bound. Pending queries also account for the request and query spans, tick resolution, and rounding to whole microseconds.

Every adjustment or query call has local target checks around it: observation must take less than 10 ms, and the uncertainty plus possible 500 ppm progress must remain below 1 ms. The call may advance correction already in progress; it must not introduce an additional wall/monotonic discontinuity. These per-call samples are checked in the target and are not printed individually.

For each nonzero dense correction, endpoint uncertainty must be less than half its magnitude. The wait controls similarly require enough resolution to distinguish the correction actually applied. A coarse tick or a wide observation bracket can therefore reject the experiment even when the image builds. Zero observed regressions applies to the sampled pairs, configuration, and workload; it does not establish continuous hardware smoothness or qualify a raw counter that loses time.

The dense loop also bounds every paired change in realtime minus monotonic time
by two ticks, both read spans, and the possible 500 ppm correction over that
pair's APB interval. It records any excess and requires the largest comparison
bound to stay below 500 us, the original staircase correction. The host oracle
checks aggregate consistency and rejects uncertainty that could conceal the
requested correction or meaningful pending progress. This distinguishes the
observed old discontinuities without claiming sub-tick physical resolution.

## Retained evidence

Output lines begin with `TIME_ADJUSTMENT` and contain schema 1 JSON records. Preserve the whole build/capture directory:

| Evidence | Contents |
| --- | --- |
| `build.json`, `build.log` | Actual command, source identities and hashes, tick interval, constructor override, and build outcome |
| `firmware/nuttx/.config`, `firmware/resolved.config` | Effective NuttX settings and resolved frontend configuration |
| `firmware/abi-report.json`, `firmware/provenance.txt` | Native ABI and prepared-input evidence |
| `firmware/nuttx-patches.json`, `firmware/nuttx-apps-patches.json`, `firmware/std-patch.json` | Applied patch and SDK receipts, including the exact 0150 patch hash |
| `firmware/nuttx/nuttx` | The exact ELF booted by the runner |
| `captures/console-<index>-public.log`, `captures/results.json` | Raw observations, shell status, full QEMU command, and image hash for each fresh boot |
| `analysis.json` | Per-run analysis and verdict, artifact hashes, workflow/harness/oracle identities, emulator identity, and virtual-time settings |

The runner verifies compiled-input and artifact hashes before capture and checks them again afterward. Failed captures remain useful evidence; preserve them with their error and configuration. The numerical observations, assertion coverage, and implementation identities together support the interpretation.

## Measured results

**All three fresh public-API boots passed.** The
[retained analysis](results/public-100us/analysis.json) and
[evidence manifest](results/manifest.json) bind the raw captures to the effective
100 us configuration, the final patch and fixture sources, and the exact ELF.
The build used NXRS `06964ca826bbeb434e6ed63e9da20f9c0cced3cb` plus the recorded
working inputs, NuttX `4268617f28ad0ed7e900a081ea1b33c9347458b3`, Apps
`3c772ea35b9245972e18999828f7b533e6fc57cc`, and QEMU 10.0.13. All 134 normal
target ABI witnesses passed. The ELF is omitted from git; its SHA-256 is
`ed8b64d4dc8049835edcf1c3382e717b2a13de8ea602d8e2e057c3fb5f1777f8`.

| Observation across three boots | Result |
| --- | ---: |
| Dense paired reads across 18 phases | 3,101,718 |
| Realtime / monotonic backward reads | 0 / 0 |
| Dense correction-discontinuity violations | 0 |
| Largest read bracket | 43.75 us |
| Largest dense correction-comparison bound | 283.654 us |
| Largest absolute phase `Instant` / APB rate error | 5.13 ppm, rounded upward |
| Pending checkpoints agreeing with the elapsed 500 ppm budget | 72 / 72 |

The +2 ms endpoints change realtime relative to `Instant` by 1.980–1.982 ms.
The negative and replacement endpoints span -2.027 to -1.939 ms, within their
recorded 0.236–0.281 ms endpoint bounds. All small adjustments finish with zero
residual; cancellation leaves no subsequent correction. The signed larger
requests, residual normalization, replacement, cancellation, and explicit-set
clearing checks pass in each boot.

| Concurrent wait | Requested | Observed APB elapsed, three boots |
| --- | ---: | ---: |
| Positive adjustment | 1,250 ms | 1,250.195625–1,250.195875 ms |
| Negative adjustment | 1,250 ms | 1,250.173050 ms |
| Forward realtime step | 250 ms | 250.182550 ms |
| Backward realtime step | 250 ms | 250.173425 ms |

The slew waits retain +1,400 us and -1,400 us respectively at the post-join
query, consistent with roughly 600 us applied while the worker waited. The
signed three-second steps occur entirely inside the shorter waits. Monotonic
elapsed time agrees with APB within the recorded brackets in all twelve waits.
These instruction-driven observations establish the tested contract, not
physical scheduling latency or a statistical reliability bound.

The [unchanged-image controls](results/icount-control/README.md) retain three
earlier runs that change only QEMU's timing mode. They remove the large ordinary
emulator rate discrepancy while preserving the original fine-tick negative
discontinuity: four backward reads in each negative/replacement phase, down to
-400 us at `shift=3`. Those images already include SysTick patch 0149. No new
timer patch is included in this repair; the original ordinary-QEMU anomalies
are not claimed as physical drift or individually explained by these controls.
The archived control drivers retain their exact historical environment paths
and hashes; the current portable reproduction entry point is `workflow.py`.

The [selected linked-symbol comparison](results/clock-symbols.json) measures
`clock_timekeeping.c` function symbols at 1,480 bytes before and 2,268 bytes
after, and static state at 40 and 48 bytes. This is **+788 bytes of selected code
and +8 bytes of state**, excluding other wrapper functions and linker padding;
the two images use different Rust fixtures, so their whole-image sizes are not
a valid production-footprint comparison. The repair adds no worker, queue or
heap allocation. The maintained native suite passes 14 unittest groups covering
23 contracts at each of three tick sizes, plus six checks with adjustment
disabled, using the actual old/new sources and nonrecovering UBSan.

## Scope and planned `hal/time` boundary

This experiment does not qualify ESP32-S3, Pico 2 W, protected builds, or another timer configuration. It has no enabled RTC or deep-sleep path. RTC validity and oscillator drift, retained time through power states, wake-up restoration, monotonic behavior across suspend, and subsequent synchronization with GNSS or NTP remain separate board and service work. The fixed seed is a test input, not time acquired from an external source.

The planned `hal/time` should expose clock capabilities and typed step, adjustment, query, and cancellation controls, with native ABI translation private to its NuttX provider. Ordinary time reads and waits continue to use Rust `std`. Reusable source selection and synchronization policy belong in `service/time-sync`; hardware RTC access remains in `hal/rtc`. This fixture establishes backend requirements for those boundaries under the existing [HAL and platform architecture](../../../docs/hal-platform-architecture.md); it does not implement those components.

The first adjustment provider must require the repaired software backend and
both public configuration flags. Other architecture/RTC adjustment paths can
affect timer rate and are not admitted by these results. PTPD's period-based
discipline model is also outside this qualification. Existing `rtc::Clock::set`
can synchronize NuttX system time, so the eventual service must coordinate RTC
writes with its single system-clock owner instead of treating them as isolated
retention updates.
