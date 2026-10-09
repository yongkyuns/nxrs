# Why the LED latency changed between C and Rust images

The large gap came from **instruction fetching in the flash-resident linked
image**, not a similarly large cost in Rust event validation. Code placement
alone can change this result. That means a future application can regress
without changing its service algorithm or compiler.

This investigation identifies the dominant mechanism, not the exact cache
set, evicted line or stall-producing instruction. It does not establish a
general Rust penalty, a worst-case deadline guarantee, or a production fix.

## The evidence

The same ESP32-S3 ran the same LED pipeline, with three or twenty services.
Each cell below is the median of three fresh-boot runs of 1,000 messages at
a requested 2 ms input period. Latency runs from command creation to LED
level readback. These are run means, not individual-event percentiles.

| Control | C: 3 services | Rust: 3 services | C: 20 services | Rust: 20 services |
| --- | ---: | ---: | ---: | ---: |
| Original images, repeated | 35.66 µs | 98.41 µs | 308.12 µs | 379.76 µs |
| Same Rust image plus zero-byte layout control | — | 98.43 µs | — | 379.76 µs |
| Same Rust image plus 16 unused flash-code bytes | — | 131.86 µs | — | 422.39 µs |

The sixteen bytes are never executed. A derived linker script places them at
the start of flash instructions; symbol addresses verify a sixteen-byte shift
of the worker, native wait, `poll` and MQ functions. The zero-byte control
preserves their original addresses and timing. Neither control adds a hot
call, changes the worker logic or rebuilds Rust. This proves that layout alone
can cause a sizeable regression: about **33 µs / 43 µs** here.

The earlier [same-image entry/worker experiment](results/esp32s3-same-image-entry-2026-10-07.json)
already showed a much smaller difference when unchanged C and Rust workers
shared one firmware: about 0.25 µs with three services and 2.86 µs with twenty.
Changing the entry from normal Rust to diagnostic C had at most 0.08 µs effect.
Those traced images changed the layout, so their absolute times cannot be
substituted for the original measurements.

### Hardware-counter and internal-RAM control

New diagnostic images count instruction-fetch waits and register-window
exceptions over the whole run. The helpers execute in internal RAM and run
only around startup/shutdown; they do not wrap the event loop, wait, send,
receive, HAL or coordinator. A second pair links the same compiled worker,
native adapter, LED HAL, MQ and `poll` input sections, including their literal
pools, into internal instruction RAM (IRAM).

This is a placement experiment, not a new messaging implementation. Both
pairs retain the same configuration, kernel archive bytes, C options, Rust
input ELF/compiler and 4,096-byte worker stacks.

| Paired diagnostic placement | C: 3 services | Rust: 3 services | C: 20 services | Rust: 20 services |
| --- | ---: | ---: | ---: | ---: |
| Hot path in flash | 35.58 µs | 112.37 µs | 308.10 µs | 387.37 µs |
| Hot path in IRAM | 35.55 µs | 35.90 µs | 311.36 µs | 314.45 µs |
| Remaining IRAM Rust − C | | **0.36 µs** | | **3.08 µs** |

Counter totals below are medians per complete 1,000-message run, in millions
of cycles. **Do not read them as per-event latency.**

| Instruction-fetch waiting | C: 3 services | Rust: 3 services | C: 20 services | Rust: 20 services |
| --- | ---: | ---: | ---: | ---: |
| Hot path in flash | 0.46 M | 24.40 M | 0.47 M | 24.17 M |
| Hot path in IRAM | 0.26 M | 0.28 M | 0.28 M | 0.32 M |

Removing flash fetching from the hot path removes about **99% of Rust's
recorded fetch waits**, alongside nearly all of the large latency gap. C does
not get faster simply because more code is in IRAM. This is stronger evidence
than treating an instrumentation-induced speedup as an optimization.

Other controls limit alternative explanations:

- Register-window exceptions are approximately 59,400 with three services and
  268,170 with twenty, for **both** languages in the flash counter experiment.
  The large gap is not accompanied by a large increase in window spill/fill
  activity. This does not exclude all small ABI or dispatch costs.
- A separate data-wait experiment records approximately 0.29–0.30 M cycles C
  versus 0.67 M Rust, not the roughly 24 M extra instruction-fetch waits.
  It uses the same counter images; data and instruction totals are collected
  in separate runs, not added into one event's elapsed time.
- Both ELF images resolve `memcpy`, `memset`, `memmove`, `memcmp` and unsigned
  64-bit divide/remainder helpers to the same chip ROM addresses. A different
  Rust memory-copy implementation does not explain this result.
- The compiler stays frozen. Neither worker formats text or allocates in its
  hot loop. No LLVM, Rust std or NuttX dependency patch was added here.

The [compact evidence](results/esp32s3-fetch-layout-2026-10-07.json) records
source/firmware hashes, function addresses, matched-kernel identities,
counter selectors and every numerical run. Diagnostic results intentionally
make no footprint claim. The legacy original C build lacks a recorded kernel
header hash; that limitation is explicit in the export. The new counter pairs
record matching header hashes as well as matching kernel archives.

## What “flash-fetch/layout” means

The processor executes much of this firmware through the flash instruction
cache. This build uses a 16 KiB instruction cache, eight ways, 32-byte lines
and DIO flash at 40 MHz. Linked addresses influence which instructions share
cache lines and which instructions compete for available cache space. Adding
unrelated code can therefore alter the time taken by an unchanged event path.
See the [ESP32-S3 technical reference manual](https://www.espressif.com/sites/default/files/documentation/esp32-s3_technical_reference_manual_en.pdf)
for the flash/cache architecture; the stated settings come from the measured
resolved NuttX configuration.

The counter is deliberately called **instruction-fetch waiting**, not a raw
SPI-cache miss count. Selector 4, mask `0x23`, combines instruction-cache-miss,
instruction RAM/ROM busy and uncached-fetch wait conditions as defined by
[Espressif's Xtensa selectors](https://github.com/espressif/esp-idf/blob/v5.4.2/components/xtensa/include/xtensa/xt_perf_consts.h).
Register access follows [Espressif's performance-monitor implementation](https://github.com/espressif/esp-idf/blob/v5.4.2/components/perfmon/xtensa_perfmon_access.c).
The ESP32-S3 SPI cache is outside the CPU's LX7 core. These counters cannot
tell us which external cache line was evicted or identify a particular
conflicting pair of functions.

Counters start at worker-zero readiness and stop before the first join.
Their scope includes startup capacity checks, idle/interrupt activity, the
terminal monitor and stop delivery; it is broader than input → LED readback.
No counter overflow was observed. Absolute times differ from the original
images because even these small helpers change the linked image. Compare
the two counter treatments with each other, not as replacements for normal
application performance.

## Avoiding silent regressions

1. Measure the **actual final firmware** on the device after changes to linked
   code, configuration, compiler or dependencies. A separate benchmark image
   can have a favourable layout that the product does not share.
2. Keep fresh-boot repeats, the traffic contract and configuration constant.
   Track both ordinary latency and deadline misses. An average passing its
   budget does not establish a hard deadline.
3. Keep an explicitly accepted normal-image baseline. The local screen below
   compares the median run means; it does not automatically bless a new one.
4. If a regression appears, use the padding/counter/IRAM controls to distinguish
   a layout problem from more work in the service. Do not fix it by retaining
   arbitrary padding or diagnostic helpers.

```sh
python3 tests/service-qualification/latency_guard.py \
  --baseline "$SQ_ACCEPTED_NORMAL_RESULT" \
  --candidate "$SQ_NEW_NORMAL_RESULT" \
  --relative-limit-percent 10 --absolute-limit-us 5
```

This example permits an increase of the larger of **10% or 5 µs**, per
language/service-count/event-count cell. The command's default relative limit
is 20%; select and record a product-appropriate budget explicitly. It requires
at least three independent samples in each compared cell, matching timing and
kernel identities, successful delivery, and non-diagnostic public results.
It returns failure on a regression or invalid evidence and reports exactly
which cells were compared. Candidate subsets are allowed; it does not imply
coverage of omitted topologies or traffic volumes. An unchanged-image repeat
passes all four 1,000-event cells in the recorded baseline.

This is a **local mean-latency screen**, not CI, a tail-latency checker or a
product qualification suite. A deadline gate still needs its own miss/max
policy and appropriate sustained, overload and interrupt tests.

IRAM placement is a possible product technique, but this deliberately broad
control consumed approximately 7.5 KiB more resident RAM in both languages
than the flash counter pair. That is a real tradeoff for a 250 kB device, not
a free production fix. Choosing a smaller critical IRAM set, increasing cache
capacity or changing flash settings requires a separate matched memory/timing
evaluation. No such production choice was applied.

## Local reproduction

Start with the frozen input and prepared matched kernel from the
[normal build instructions](README.md#local-reproduction), using fresh output
directories for every link and measurement:

- Original pair: normal links, no diagnostic options.
- Layout controls: normal C reference; link Rust with `--layout-pad-bytes 0`
  or `--layout-pad-bytes 16`. Measure only Rust with `--language rust`.
- Flash counters: link **both** languages with `--perfmon`; measure with
  `--pm-mode fetch`. Repeat the same images with `--pm-mode data` for the
  data-side control.
- IRAM counters: link **both** with `--perfmon --hot-iram`; measure with
  `--pm-mode fetch`. Do not combine these counters with `--trace`.

All measurements here use `--blocks 3 --events 1000`. The builder derives a
private linker script, leaving the dependency's script untouched, and records
its hashes and selected input sections. Rust reuses the identical compiled
input. The exporter checks final symbol placement and artifact hashes:

```sh
python3 tests/service-qualification/layout_report.py \
  --case flash-fetch "$SQ_FLASH_RUN/report.json" "$SQ_C_PM" "$SQ_RUST_PM" \
  --case iram-fetch "$SQ_IRAM_RUN/report.json" "$SQ_C_IRAM" "$SQ_RUST_IRAM" \
  --prefix "$SQ_GCC_PREFIX" --out "$SQ_PUBLIC_DIAGNOSTIC_RESULT"
```

The normal footprint exporter rejects padding, counter and IRAM diagnostics.
All six measurement matrices restored and verified the original complete
16 MiB firmware. Do not publish backup images, host paths or raw transcripts.
The existing [fault-path and IRQ qualification gaps](README.md#open-qualification-work)
remain open; this successful-path diagnosis does not close them.
