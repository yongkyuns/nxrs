# Event-based service comparison

This temporary ESP32-S3 experiment compares independent services on NuttX C,
NuttX Rust, Zephyr C and Embassy Rust. It adds no production dependency. See
the [RTOS analysis](../../docs/rtos-comparison.md) for conclusions; the earlier
[producer/worker/reply workload](../zephyr-comparison/README.md) is distinct
and its timings must remain separate.

## Workload and contract

Twenty state-owning services publish control, data and status events to three
peers at offsets 1, 3 and 7 modulo 20. Each inbox has one receiver and multiple
senders; there are no replies or collector. One wait-any point covers all
inboxes and the next publication deadline. Ready queues are round-robin; an
iteration handles at most one event and publishes at most one due batch.
The original Embassy policy yields between iterations; natural, budgeted and
chunked controls use the same service contract with timer-backed I/O waits.

The three-queue layout uses 60 queues of eight 64-byte slots; the mailbox
layout uses 20 queues of 24. Both provide 480 slots (30,720 payload bytes).
These sizes are synthetic workload choices, not requirements of nxrs.
Class queues isolate event types; a mailbox shares capacity and FIFO order.
Sends never block, retry or overwrite: a full destination is counted as a
rejection. Normal and burst qualification requires all releases attempted,
all accepted traffic drained, and zero rejections or protocol errors.

Traffic runs for two seconds, followed by a 500 ms drain. Control/data/status
periods are 250/50/100 ms. First release is one period after start plus the
service ID in milliseconds and twice the class ID in milliseconds. All
releases follow the absolute shared-start calendar; late ones are attempted
and measured. Normal/burst attempt 3,900/6,240 deliveries; burst doubles data
events. Overload uses 10 ms data periods,
16 events per release and zero phase; rejections are expected and explicit.
Unattempted releases at drain end invalidate a run.

Each C/Rust handler validates the 44-byte payload, routing and per-flow
sequence, then applies the same wrapping 32-bit state update; host differential
tests compare generated events and handler results. Deadlines are 20/40/80 ms
for control/data/status and are reported separately from delivery.

## Measurement and evidence

The matched 2026-10-04 cohort uses one 240 MHz core, DIO 40 MHz flash, 16 KiB
instruction/32 KiB data caches, `-O2` applications and 10 ms timer resolution.
All timestamp the same 16 MHz ESP32-S3 SYSTIMER. Initialization, staging,
printing and reconciliation are outside the timed interval.

Post-to-handler runs from just before send to handler start, including the
send, queueing and scheduling. Release-to-handler also includes publisher
timing. Publication lateness, handler finish and control response are
separate metrics. Per-service histogram upper bounds are capped at the
observed maximum; summary p99 is the median of per-run p99 bounds, not pooled
samples. RAM separates queues, execution, service state and diagnostics.
NuttX heap high-water is not a full-capacity bound; static reservations count
once, and thread stacks are execution cost.

Measurements use separate frozen workload, control, scheduling, minimal-NuttX
and compiler cohorts. Retain their records and hashes; never pool changed
configurations. The [evidence index](results/README.md) separates current final
records from supporting cohorts. Compiler reports use a private pinned
LLVM/Rust build with source revisions and a patch ledger. Normal builds use
the installed SDK; patches remain inactive pending wider qualification. Raw
serial logs and full-flash backups are private local data, not report artifacts.

## Reproduce locally

### Optional toolchain setup

On Linux or macOS (x86_64/arm64), start with Python 3.12+, Git, a host C
compiler, `bash`, `dtc`, and Rust's `rustup`/Cargo proxies. Install missing
host tools with your package manager (`brew install dtc` on macOS or
`sudo apt install device-tree-compiler` on Debian/Ubuntu). Then:

```sh
python3 tests/event-services-comparison/setup.py --platform all --build
```

This explicitly downloads the pinned Zephyr sources and **only the ESP32-S3
SDK toolchain**, ESP Rust/GCC, espflash, and locked Embassy crates, then builds
`zephyr-c-three` and `embassy-three` under `target/rtos-comparison/images/`.
Use `--platform zephyr` or `embassy` to install just that comparison; omit
`--build` for dependency setup only. `--plan` previews downloads without
creating files or contacting the network. `--layout one` selects the mailbox.

Downloads are SHA256-checked, source commits are checked, and repeated setup
reuses verified downloads. Python packages, Cargo caches and Rust toolchain
registration stay in the ignored setup directory, not your global environment.
Resolved Python packages are recorded in `python-resolved.txt`; fresh setup
uses pinned inputs but does not reproduce a historical host environment or the
private patched compiler used for compiler cohorts.
Use `--root` for an external cache and a fresh `--out` for subsequent builds;
existing images, dirty checkouts and unmanaged directories are never overwritten.
Source the generated `environment.sh` only when using the older manual builders.

No board is flashed, no compiler patch is activated, and normal nxrs builds
and CI do not install these dependencies. NuttX still uses the existing setup
below; the optional installer does not build a private patched LLVM compiler.

For a histogram-free qualification build, add `--instrumentation lean` and a
fresh `--out` to setup or `build.py`. This removes the 102 histogram-bin arrays
but retains scalar counts/maxima, deadline checks and complete delivery
reconciliation. Work, events, capacities, stacks and scheduling are unchanged.
The small remaining checker state is still benchmark storage, not a production
requirement. Lean records omit latency distributions; timing matrix runners
reject lean images. Measure both modes with `control_matrix.py`, selecting
`normal`, `burst` and `saturation`, then export them together:

```sh
python3 tests/event-services-comparison/control_report.py \
  --matrix target/full-runs/control-matrix.json \
  --lean-matrix target/lean-runs/control-matrix.json \
  --out target/instrumentation-comparison.json
```

Both matrices must use the same backup and matched four-platform inputs. A lean
footprint is measured from rebuilt images, never estimated by subtracting fields.

### RAM attribution

Attribute the measured report without rebuilding or flashing:

```sh
python3 tests/event-services-comparison/ram_attribution.py \
  --report target/instrumentation-comparison.json \
  --reference-root target/full-images --lean-root target/lean-images \
  --readelf "$READELF" --out target/attributed-comparison.json
```

Each image root contains the four case directories with `app.elf`,
`build-provenance.json` and, for NuttX, `resolved.config`. ELF/configuration
hashes and section totals must match. Selected resident objects are counted
once; aliases, dummy sections and unused heap gaps are excluded. The committed
October 10 record includes this ledger and exact object sizes.

Execution workspace, event capacity, adapter controls, fixture application state
and checker/coordinator overhead are separate. The fixture's 3,040 B flow/digest
state is not messaging overhead; real services still need their own application
state. NuttX message contents are split across the
static pool and heap; measured full/empty/drained snapshots validate the
allocator model. Zephyr's heap arena is counted once, regardless of occupancy.
Unsplittable linked data and NuttX's remaining live heap stay explicit: the
ledger does not prove a production minimum or that the entire remainder is
necessary. Saved service futures contain this workload's locals, not a fixed
per-task tax. Console/runtime costs and transient stack use are not completely
separable from testing without another controlled rebuild.

### Minimal NuttX configuration

The current size comparison uses [`nuttx-minimal.conf`](nuttx-minimal.conf).
It removes unused shell/board utilities, environment/child-task bookkeeping
and PSRAM, retaining native POSIX queues, `poll`, pthread creation/joining,
LED readback, assertions and stack coloration.
Kernel/libc use `-Os`, as in Zephyr; C/Rust handlers remain `-O2`. The 20 ×
4 KiB worker stacks and 480 × 64-byte queue capacity are unchanged. The
initial application uses an 8 KiB stack, replacing NSH's separate root/app
tasks. A bounded command loop retains the same profiles and native allocator
high-water reporting outside timed windows.

Prepare the pinned NuttX SDK/tree using the
[existing setup](../service-footprint/README.md#prepare-and-build-for-esp32-s3).
Use a resolved common baseline with a 1 ms tick and 10 ms native timeslice;
`nuttx_profile.py` rejects timing, ABI, resource, assertion or flash-driver changes. For
example, set `BASE` to that prepared tree and `READELF` to the pinned Xtensa
readelf executable, then use fresh output paths:

```sh
python3 tests/event-services-comparison/nuttx_profile.py \
  --baseline-config "$BASE/resolved.config" \
  --hal-cache "$BASE/nuttx/arch/xtensa/src/esp32s3/esp-hal-3rdparty" \
  --target-spec "$BASE/xtensa-esp32s3-nuttx.json" \
  --out target/event-minimal-tree
for language in c rust; do
  python3 tests/event-services-comparison/build.py \
    --platform "nuttx-$language" --layout three --timer-ms 1 \
    --nuttx-profile minimal --matched-baseline "$BASE/resolved.config" \
    --nuttx-tree target/event-minimal-tree \
    --baseline-config target/event-minimal-tree/baseline.config \
    --sysroot "$BASE/toolchain" --readelf "$READELF" \
    --out "target/event-minimal-images/nuttx-$language-three"
done
```

The reported October 9 Rust build reused an immutable six-patch compiler
partial link with `--rust-input-bundle`; its ELF, source, target specification
and std optimization identities are checked before relinking. Repeating that
build requires the matching private bundle and provenance. Dependency sources
remain pinned and existing patchsets are applied only in fresh build copies. A
different compiler is a new measurement cohort.
This profile is not a general-purpose std preset: applications needing
randomness, filesystems, environment variables or child-task waiting must
enable and budget them. No production platform configuration is changed.

### Build and device-run recipes

Use the existing pinned platform toolchains and prepared NuttX tree. A new
build or device run is a new measurement cohort; it does not by itself recreate
an old result. Build fresh output directories for both layouts and each
platform, retaining each `build-provenance.json`. The builder checks frozen
inputs and hashes. For example, provide the platform-specific paths required
by `build.py`:

```sh
python3 -m unittest discover -s tests/event-services-comparison -p 'test_*.py'
python3 tests/event-services-comparison/build.py --help
python3 tests/event-services-comparison/run_matrix.py --help
```

Before any device run, make a private full 16 MiB flash backup, verify its
length and SHA-256, and protect it with `chmod 600`. Use the matrix runner
(not the low-level measurement script); it verifies frozen image hashes,
rotates case order, and restores and verifies the original full flash in
`finally`, including after failure. Keep the board under one tool's control.
The command shape is:

```sh
python3 tests/event-services-comparison/run_matrix.py \
  --artifacts target/event-services-images --out target/event-services-runs \
  --backup /private/path/device-before.bin --port /dev/cu.YOUR_BOARD \
  --flasher /path/to/esptool.py --runs 3 --blocks 2 \
  --profile normal --profile burst
python3 tests/event-services-comparison/report.py \
  --matrix target/event-services-runs/matrix.json \
  --out target/event-services-results.json
```

For the capacity/wake controls, use `control_matrix.py` with `--profile
saturation` or matched `--timer-ms 10`/`--timer-ms 1` builds. For the final
I/O/CPU comparison, build the three-queue variants with `--timer-ms 1`; select
`--embassy-scheduling natural` or `budget`, adding `--work-mode chunked` for
the chunked policy. Use `scheduling_matrix.py` with `--runs 2 --blocks 2`
and profiles `normal`, `work-medium`, `work-long` and `io-wait`.
Both matrix runners take the same artifact, output, backup, port and flasher
arguments shown above. Their `--help` lists case names. Export using
`control_report.py` or `scheduling_report.py`, respectively; retain compiler
ledgers separately when evaluating a private compiler.

Never commit or upload the backup; firmware may contain credentials. Preserve
failed runs. An overload run that misses scheduled releases or fails to drain
is not a valid speed result.

### Gap-free distribution images

Flat ESP `.bin` files preserve flash offsets, including required alignment
padding. Do not delete zero/FF runs: they may be real application data or alter
mapping/checksums. The common packer removes only parsed format-defined gaps
from all four frozen images, retaining their fill values and offsets in a ZIP
manifest. `ZIP_STORED` disables compression so size comparisons do not depend
on compressibility. The exported report excludes local paths and validates
the existing frozen artifact identities; no rebuild or device run is needed.
The artifact directory must contain all four named cases; use `--case` to
package a subset instead.

```sh
python3 tests/event-services-comparison/image_package.py pack \
  --artifacts target/event-minimal-images --out target/event-packages \
  --report target/event-image-packages.json
python3 tests/event-services-comparison/image_package.py unpack \
  --package target/event-packages/embassy-three.zip \
  --out target/embassy-flash.bin
```

Packing automatically verifies byte-identical reconstruction for each image.
Unpacking checks chunk hashes and the complete image hash before writing a
fresh output. ZIP packages are **not directly flashable**: use the reconstructed
`.bin` with the existing backed-up matrix workflow. The current tool supports
the tested simple-boot NuttX/Zephyr layouts and single-factory ESP-IDF Embassy
layout; it rejects unexplained tails, signed images and invalid padding.
