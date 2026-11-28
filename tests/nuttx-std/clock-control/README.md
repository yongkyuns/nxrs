# Clock control qualification

This fixture implements phase 1 of [NXRS #490](https://github.com/yongkyuns/nxrs/issues/490): establish what the selected NuttX clocks actually do before adding a reusable synchronization service. It measures explicit wall-clock steps, positive and negative adjustments, replacement, cancellation, pending-adjustment queries, and Rust time reads against an independent emulated timer.

The package is an independent, dependency-free Cargo workspace marked `qualification-only = true`. Its NuttX command is `clock_probe`, with priority 100 and a 32 KiB entry stack. [Rust](src/main.rs) owns the measurement loops; [native.c](native.c) supplies mechanical calls to the configured NuttX APIs. Host execution refuses to run and exits with status 2, so the fixture cannot set the host clock.

This phase does not add a production clock HAL, synchronization service, NTP client, GNSS source, or RTC retention implementation. The design and subsequent work remain tracked in #490.

## Configuration and source scope

The target is `thumbv8m.main-nuttx-eabi`, using the existing `mps2-an521-mock` profile and QEMU's MPS2-AN521 machine. All three variants keep `CONFIG_CLOCK_ADJTIME` disabled. The variant names below identify separate build and capture directories; they are not new NXRS profiles.

| Variant | `CLOCK_TIMEKEEPING` | `USEC_PER_TICK` | SysTick frequency correction | Purpose |
| --- | --- | ---: | --- | --- |
| `baseline` | Disabled | 1,000 us | None | Record the unmodified board/emulator clock rate, explicit steps and sleep duration |
| `software-corrected` | Enabled | 1,000 us | Explicitly enabled | Evaluate the software adjustment implementation with the emulator frequency supplied correctly |
| `software-fine100us` | Enabled | 100 us | Explicitly enabled | Observe behavior that a coarser clock read can conceal, including negative adjustments near a second boundary |

The source review targets NuttX 13.1.1 at `4268617f28ad0ed7e900a081ea1b33c9347458b3` and Apps at `3c772ea35b9245972e18999828f7b533e6fc57cc`. Each build records the actual NXRS revision, both gitlink identities, the working-tree status, and hashes of the fixture inputs. Reassess the findings after changing those inputs; a newer source tree needs its own captures.

### Public API admission

The pinned [scheduler Kconfig](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/sched/Kconfig) admits `CLOCK_ADJTIME` only through `ARCH_HAVE_ADJTIME`, `RTC_ADJTIME`, or `PTP_CLOCK`. Enabling `CLOCK_TIMEKEEPING` does not satisfy that dependency. The `adjtime()` declaration in [sys/time.h](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/include/sys/time.h) and its [protected syscall entry](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/syscall/syscall.csv) remain gated by `CLOCK_ADJTIME`.

However, `CLOCK_TIMEKEEPING` compiles an `adjtime()` implementation in [clock_timekeeping.c](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/sched/clock/clock_timekeeping.c). The software variants call that implementation through an explicitly local diagnostic declaration in `native.c`. Their header records `direct_implementation_only=true` and `adjtime_header=false`. This permits observing the implementation in the flat fixture image; it does not establish a supported application API or protected-build path. No architecture or PTP capability is forced to make Kconfig accept the public API.

The same source rejects a null `delta` and writes only `olddelta->tv_usec`. The fixture preserves the returned result, errno, and both fields of `olddelta`, which start at recognizable sentinels. A successful adjustment request must not hide an incomplete returned `timeval`, and an invalid pair must not be combined into a duration.

### Independent clock and explicit QEMU control

The reference is the existing [memory-timing APB timer adapter](../memory-timing/qemu_timer_clock.c). It owns otherwise-unused CMSDK APB timer 1 at secure address `0x50001000`, running at QEMU AN521's 20 MHz. It checks peripheral identity and stopped state before use, disables timer interrupts, and starts a fresh epoch. Its 32-bit counter wraps after about 214.7 seconds; this fixture bounds the suite to less than 60 reference seconds. See the [memory qualification clock evidence](../../../docs/nuttx-memory-qualification.md#clock-evidence-and-rejected-attempts) for the adapter's provenance and rejected alternatives.

The pinned NuttX [board.h](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/boards/arm/mps/mps2-an521/include/board.h) declares `MPS_SYSTICK_CLOCK` as 32 MHz. QEMU 10.0.13 supplies the AN521 CPU and APB clocks at 20 MHz; the source path is recorded in [qemu_systick_clock.c](qemu_systick_clock.c). This difference affects the OS clock and requested sleeps independently of `adjtime()`. The baseline retains it for observation.

`--qemu-clock-correction` explicitly adds that helper and links with `--wrap=systick_initialize`. The helper passes 20 MHz to the actual NuttX SysTick constructor, with compile-time and runtime checks for the expected AN521 configuration. It leaves the independent APB reference and clock-adjustment algorithm unchanged. This is a fixture control, not a production BSP change or a physical-board calibration. The workflow records whether it was enabled; never merge corrected and uncorrected observations into one result.

## Reproduce

Run from the NXRS repository root on a POSIX host. Initialize the pinned submodules and use the Rust toolchain in [`rust-toolchain.toml`](../../../rust-toolchain.toml), currently 1.100.0. The target build also needs the pinned `nightly-2026-11-26` toolchain with `rust-src`, ARM GCC/binutils/newlib, `make`, `kconfig-conf`, `bison`, `flex`, `gperf`, `genromfs`, Python with `kconfiglib`, and `qemu-system-arm` on `PATH`. QEMU must include the MPS2 UART fixes required by the existing ARM runner; the reference control above targets QEMU 10.0.13. The normal [build system](../../../docs/build-system.md) owns SDK preparation and native configuration.

Use fresh output directories. The workflow refuses to overwrite a build directory or an existing capture directory. It invokes the ordinary `cargo nxrs build` frontend, supplies C helpers through `--target-c-source`, and validates the effective configuration and target ABI report before running the image.

```sh
git submodule update --init
rustup toolchain install nightly-2026-11-26 --profile minimal --component rust-src
export NUTTX_STD_COMPAT_FIXES=1

python3 tests/nuttx-std/clock-control/workflow.py build \
  --mode baseline --out target/clock-control/baseline
python3 tests/nuttx-std/clock-control/workflow.py run \
  --build target/clock-control/baseline

python3 tests/nuttx-std/clock-control/workflow.py build \
  --mode software --qemu-clock-correction \
  --out target/clock-control/software-corrected
python3 tests/nuttx-std/clock-control/workflow.py run \
  --build target/clock-control/software-corrected

python3 tests/nuttx-std/clock-control/workflow.py build \
  --mode software --qemu-clock-correction --tick-us 100 \
  --out target/clock-control/software-fine100us
python3 tests/nuttx-std/clock-control/workflow.py run \
  --build target/clock-control/software-fine100us
```

Each `run` uses three fresh QEMU boots by default; `--repeat 1` or `--repeat 2` selects fewer. The [existing NuttX runner](../run.py) checks the shell identity and command exit status and retains the console transcript. Its `PASS` means that capture completed. The separate `qualified` field in `analysis.json` determines whether the observed backend met the clock gates. A complete characterization can report `qualified=false`.

Recheck the committed evidence and the malformed-transcript, false-continuity and hidden-regression controls without running QEMU:

```sh
python3 -m unittest discover -s tests/nuttx-std/clock-control -p test_oracle.py -v
```

## What the measurements mean

`clock_probe` seeds a fixed test epoch, steps `CLOCK_REALTIME` forward and backward, and observes `SystemTime` and `Instant` around each operation. The seed is a known test input, not an externally acquired UTC estimate. Rust `std` supplies the clock reads and sleeps; its public time API does not provide these clock setters, so the diagnostic bridge calls the native interfaces.

The software variants request +2 ms and -2 ms adjustments after seeding whole-second wall times. Each observation window lasts five APB seconds. They also replace a pending +20 ms request with -2 ms, cancel a separate +20 ms request, and query the remaining adjustment. Samples bracket the Rust reads with APB reads and record the midpoint and full read span. Monotonic time is recorded relative to an `Instant` origin, with regressions checked explicitly rather than saturated to zero.

Dense read loops collect counts, minimum deltas, regressions, and approximately 250 ms checkpoints without allocating or printing. Buffered results are printed afterward as `CLOCK_PROBE` JSON lines. The tick interval, reported realtime resolution, observed minimum positive increments, and bracket spans limit what those observations can establish. A lack of observed backward reads at a coarse tick is not proof of smooth adjustment. Endpoint convergence also does not establish the rate taken to get there; retain the intermediate checkpoints.

The pinned software implementation applies bounded corrections when its wall-time update crosses a second. A negative correction can return that wall time below the boundary, allowing a following update to cross it again. The finer-tick variant is a diagnostic for this source-level concern. The fixture leaves normal scheduler updates in control and never calls `clock_update_wall_time()` itself.

The 100 ms `std::thread::sleep` calls measure actual elapsed time against APB both before adjustments and while a correction request is pending. A pending correction need not be applied during that short sleep. These calls therefore do not establish sleep behavior across an applied slew, across an explicit clock step, or through deep sleep.

### Retained evidence

Keep the entire output directory with each result:

| File or directory | Evidence |
| --- | --- |
| `build.json`, `build.log` | Build command, source identities and hashes, selected mode/tick/correction, build outcome and artifact hashes |
| `firmware/nuttx/.config`, `firmware/resolved.config` | Effective NuttX configuration and resolved frontend inputs |
| `firmware/abi-report.json`, `firmware/provenance.txt` | Target ABI checks and prepared build provenance |
| `firmware/nuttx/nuttx` | The exact ELF booted by QEMU |
| `captures/console-<index>-<mode>.log`, `captures/results.json` | Raw JSON observations, shell status, boot command and image identity for each repetition |
| `analysis.json` | Per-run observations, explicit qualification gates, oracle hash, emulator version/hash and overall completion status |

The runner rejects changed fixture inputs or mismatched retained firmware evidence. Its hashes bind the observations to the built image; an optional in-band `build_id` alone does not. Preserve failing captures and failed gates as evidence instead of filtering them out or treating a zero command status as qualification.

## Recorded findings

The [retained evidence manifest](results/manifest.json) binds nine fresh-boot captures: three for each variant, built from NXRS `8f0db0bbe85af7f6239e6568b16b1efce133ad1e` plus the hashed fixture sources, with the pinned NuttX and Rust toolchains above. All three native builds passed the normal frontend's 134 target ABI witnesses. The committed evidence includes raw logs, configurations, ABI reports, provenance and analyses; large ELF images are omitted, with their hashes retained in the build and capture records. Reproduction creates new complete output directories.

**All nine characterizations completed; none qualified the backend.** The figures below summarize the retained runs, without rescaling either clock to make it agree with the reference. The rate and sleep columns use each run's initial no-adjustment phase and sleep.

| Variant | Initial `Instant` / APB rate | Requested 100 ms sleep, APB elapsed | Negative-adjustment observations |
| --- | ---: | ---: | --- |
| [Baseline](results/baseline/analysis.json) | 0.6110–0.6250 | 161.13–164.25 ms | No adjustment implementation admitted; diagnostic query returns `ENOSYS` |
| [Software, 1,000 us tick](results/software-corrected/analysis.json) | 0.9460–1.0000 | 101.13–101.55 ms | 3–4 backward reads in each negative phase; minimum delta −500 us |
| [Software, 100 us tick](results/software-fine100us/analysis.json) | 0.9014–0.9788 | 102.11–104.54 ms | 9–21 backward reads in each negative phase; minimum delta −500 us |

The baseline is consistent with the source-level 20/32 frequency mismatch, with additional timing variation. The explicit constructor correction removes that fixed mismatch, but the corrected runs still fail rate or continuity gates. In the 1,000 us variant, some later 100 ms sleeps take up to 122.02 ms. Neither the constructor control nor an increasing clock proves accurate elapsed-time accounting.

Both software variants accept positive, negative, replacement and cancellation requests. In every run, all four null-delta queries return `EINVAL`; successful requests leave `olddelta.tv_sec` at its sentinel. Replacement and cancellation report `19500` in the raw microseconds field after the initial +20 ms request, but that incomplete `timeval` is not a valid normalized result.

At the 1,000 us tick, positive and negative endpoints change wall time relative to `Instant` by approximately +2.00 ms and −2.00 ms, within roughly 1.02–1.13 ms endpoint uncertainty. At the 100 us tick, positive endpoints are +1.995 to +2.000 ms, negative endpoints −2.002 to −2.008 ms, replacement endpoints −2.000 to −2.040 ms, and cancellation endpoints −0.067 to 0 ms, with relevant uncertainty of 0.124–0.199 ms. These characterize the amount observed; they do not establish an exact convergence time or a continuous slew rate. The asymmetric intermediate traces are consistent with the repeated-second-boundary concern in the source.

The fine-tick run also exposes **backward wall reads without a negative adjustment**: −100 us deltas occur in the initial baseline of one run and in positive/cancelled phases. The 1,000 us replacement phases include one −1,000 us minimum. Those observations require further timer/read-path isolation; they must not all be attributed to the negative-adjustment algorithm. Read brackets reach about 28 ms in these emulator runs, so dense sampling is not uninterrupted coverage.

No dense phase reported an `Instant` regression, but some operation-continuity gates failed and the rate checks remain unsatisfied. Deliberate wall-clock steps passed their read-window checks. Sleep across an actually applied correction or concurrent explicit step remains unqualified, as do physical RTC retention and wake restoration.

### Required backend follow-up

Before exposing smooth adjustment through production clock control:

1. Isolate the timer/read-path regressions and rate loss using equal-duration no-adjustment controls and the actual selected board timer. Preserve independent reference measurements and worst-regression pairs.
2. Make the software clock's reads and updates share one consistent projection of a bounded correction from **raw elapsed counter time**. Keep fractional carry and unapplied residual; a negative rate must never reverse realtime. Materialize the prior correction before replacement or cancellation. Merely applying smaller negative steps in the periodic update still permits a read immediately before that update to exceed a read immediately afterward.
3. Admit the corrected software backend through the public `CLOCK_ADJTIME` declaration/syscall path. Implement non-mutating pending queries, normalized output of both `timeval` fields, checked input conversion, and explicit replacement/cancellation semantics.
4. Repeat the clock controls with varied tick sizes and boundary phases, and add waits that demonstrably span applied corrections and explicit steps. Qualify each hardware timer and power path separately.

The proposed `hal/time` supplies system-clock control and qualified capabilities; ordinary reads and waits continue to use Rust `std::time` and `std::thread`. The reusable `service/time-sync` owns source selection and synchronization policy, while hardware RTC access remains in `hal/rtc`. These components in #490 should consume demonstrated backend capabilities after the repairs above. This fixture supplies the failure evidence and regression starting point; it does not promote the current implementation to a supported smooth-slew backend.

## Other target capabilities: source review only

These paths identify follow-up candidates at the pinned NuttX revision. This fixture does not execute ESP32-S3 or Pico 2 W, and a Kconfig option is not a timing qualification.

| Target | Source-level capability | Exact checkout paths |
| --- | --- | --- |
| MPS2-AN521 | The NSH defconfig enables `TIMER_ARCH` and ARMv8-M SysTick. The generic timer framework admits `CLOCK_TIMEKEEPING`; the public `CLOCK_ADJTIME` gate remains separate. | [`external/nuttx/boards/arm/mps/mps2-an521/configs/nsh/defconfig`](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/boards/arm/mps/mps2-an521/configs/nsh/defconfig), [`external/nuttx/drivers/timers/Kconfig`](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/drivers/timers/Kconfig) |
| Pico 2 W / RP23xx | Optional `RP23XX_SYSTIMER_SYSTICK` selects `TIMER_ARCH`; optional `RP23XX_SYSTIMER_TICKLESS` selects `ALARM_ARCH`. Both generic paths select `ARCH_HAVE_TIMEKEEPING`. This does not establish support in every profile or qualify a physical slew implementation. | [`external/nuttx/arch/arm/src/rp23xx/Kconfig`](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/arch/arm/src/rp23xx/Kconfig), [`external/nuttx/drivers/timers/Kconfig`](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/drivers/timers/Kconfig) |
| ESP32-S3 | The examined architecture and common Espressif Kconfigs do not select `ARCH_HAVE_ADJTIME` or `ARCH_HAVE_TIMEKEEPING`. NXRS patch 0131 supplies hardware timer reads with the periodic scheduler; it does not add clock adjustment or RTC retention qualification. | [`external/nuttx/arch/xtensa/src/esp32s3/Kconfig`](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/arch/xtensa/src/esp32s3/Kconfig), [`external/nuttx/arch/xtensa/src/common/espressif/Kconfig`](https://github.com/apache/nuttx/blob/4268617f28ad0ed7e900a081ea1b33c9347458b3/arch/xtensa/src/common/espressif/Kconfig), [`upstream/nuttx/patches/0131-esp32s3-read-systimer-with-periodic-scheduler.patch`](../../../upstream/nuttx/patches/0131-esp32s3-read-systimer-with-periodic-scheduler.patch) |

RTC oscillator accuracy, validity after reset, retention across deep sleep, wake-up clock restoration, monotonic-clock behavior across suspend, and resynchronization after reacquiring an external time source require separate board evidence. This QEMU phase establishes none of those properties. Use its findings to settle the native backend requirements in #490 before implementing the synchronization owner under the existing [HAL and platform architecture](../../../docs/hal-platform-architecture.md).
