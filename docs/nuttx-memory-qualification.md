# NuttX heap and stack qualification

This qualification complements the host Rust `GlobalAlloc` measurements in
`docs/memory-qualification.md`. It uses NuttX's own procfs data on the
Cortex-M33/MPS2 qualification image; it does not reinterpret Rust allocator
counters as operating-system memory.

## What is measured

The MPS2 profile explicitly requires `CONFIG_FS_PROCFS=y` and
`CONFIG_STACK_COLORATION=y`, and keeps process/meminfo procfs entries enabled.

For both `std-demo` and `ao-stress`, the QEMU harness reads
`/proc/meminfo`:

1. before the first measured application lifecycle,
2. after one warm-up lifecycle,
3. after each of five checked lifecycles.

NuttX's meminfo implementation reports total, used, free, historical maximum
used, largest free block, used-block count and free-block count for each
ordinary registered heap. Reading meminfo also asks NuttX to reclaim delayed
frees before reporting the heap values.

The first application lifecycle is reported separately because one-time runtime
initialization may legitimately change the baseline. After warm-up, every
checked lifecycle must return `used` and `free` exactly to the warm baseline.
The largest free block may stay equal or improve, but may not shrink. This is a
strict repeated-lifecycle fragmentation/reclamation check; there is no arbitrary
byte tolerance.

## Active-object stack high-water

`ao-stress --stack-report` uses the unchanged workload and an observation-only
completion barrier. Each producer, worker and collector finishes its real
handler loop, then remains alive at the barrier. While every owner is paused,
the main task reads:

- `/proc/<pid>/status` for the NuttX task/thread name;
- `/proc/<pid>/stack` for `StackSize` and `StackUsed`.

NuttX stack coloration retains each owner's lifetime high-water use, so the
measurement happens after the workload without needing a sampling thread in the
hot path. The observer requires exactly the configured owner inventory and
rejects missing/duplicate owners, missing coloration data, zero sizes, or
`StackUsed > StackSize`.

Every detail is emitted as `NUTTX_STACK_RESULT` JSON followed by an independently
checked `NUTTX_STACK_SUMMARY`. The external harness validates all four scenarios
and five repeated runs, then records the worst observed owner usage.

The barrier exists only when `run_observed` is selected. Normal
`stress::run` uses no completion-ready channel/gate and the standard CLI path
is still executed twice in the same boot after the diagnostic cycles.

## Run

The existing firmware qualification invokes this automatically:

```sh
cargo nxrs build --app std-demo --profile mps2-an521-mock
cargo nxrs build --app ao-stress --profile mps2-an521-mock

python3 tests/host/run-std-apps-nuttx.py \
  --qemu "$(command -v qemu-system-arm)" \
  --app ao-stress \
  --image target/build/ao-stress/mps2-an521-mock/nuttx/nuttx \
  --log target/build/ao-stress/mps2-an521-mock/console.log
```

Parser and false-success controls run independently on the host:

```sh
python3 -m unittest discover -s tests/host -p test_nuttx_memory.py -v
```

## Interpretation limits

These values characterize the exact NuttX configuration and QEMU-executed
Cortex-M33 image. QEMU timing is not physical-MCU timing. Stack coloration
measures high-water bytes touched, not a proof that every future call path fits.
The heap lifecycle check can detect retained bytes or degradation of the largest
free block across the exercised cycles, but it is not a proof that arbitrary
future allocation patterns cannot fragment the heap.

The procfs reads themselves have implementation cost. Heap snapshots are taken
only after application termination and use the same command each time; stack
reads occur while owners are intentionally paused after their measured work.
None of these diagnostic observations is used as a throughput or latency
benchmark.

## Inspect addressed snapshots

[Memory Explorer](../tools/memory-explorer/README.md) opens the final ELF and GNU
map alongside addressed heap/task-stack observations in an egui viewer. Its
read-only GDB exporter records raw default-allocator chunk extents and stack
reservations. Unlike the procfs meminfo read above, it does not drain delayed
frees. It also does not sample stack coloration or measure requested payload
sizes, so keep using this qualification for lifecycle reclamation and stack
high-water evidence.

The viewer can navigate independently captured lifecycle points, with unknown
runtime state between them. A host ELF hash prevents accidental artifact mixing;
it does not verify the running device image or prove allocator quiescence.
Physical backing requires an explicit board layout. The tool's guide describes
capture prerequisites, file semantics, controls, and the remaining qualification
work in issue #442.

### Real QEMU capture witness

The [repeatable capture commands](../tools/memory-explorer/README.md#qualify-a-real-qemu-capture)
reuse the existing `std-demo` / `mps2-an521-mock` build. The
[harness](../tools/memory-explorer/tests/qualify_qemu.py) boots that exact ELF,
runs `std_demo --case channels`, and compares every exported chunk boundary and
state, plus represented stack bounds, with an independent raw target-memory
walk. Captured allocator/task metadata must remain unchanged across each export.
The [Rust import test](../tools/memory-explorer/tests/qemu_capture.rs) then checks
the real observations against those raw records, including per-arena accounting
and unknown state between points. That test is explicitly ignored unless its
evidence directory and exact ELF are supplied.

The initial witness used QEMU 10.0.13 and GDB 16.3 with ELF SHA-256
`c08b9a62c164948cb348bf85d546f1a96b66143ba18306722c739158f596435f`:

| Stopped point | Raw chunks | Represented task stacks |
| --- | ---: | ---: |
| NSH ready | 27 | 3 |
| `std_demo_main` entry | 32 | 4 |
| Return after the channels case | 35 | 3 |

The workload passed and all three raw/export comparisons found no change in the
captured metadata during export. These counts describe this image and run, not
fixed future acceptance thresholds. The final capture recorded four deferred
frees; the exporter leaves them untouched, so this is not a leak finding or a
replacement for the drained, repeated-lifecycle gate above. Coordinates `0`,
`1000000000`, and `2000000000` label observation order, not measured durations.
The entry capture precedes the channels workload and does not measure peak
allocation or worker-stack use. No physical backing map is inferred.

The [std-apps workflow](../.github/workflows/std-apps-self-hosted.yml) runs this
additional capture/import gate for its existing `std-demo` ARM build after the
ordinary lifecycle checks. Its firmware artifact retains the exact ELF/map,
capture and independent raw JSON, configuration, hashes, tool versions, and
logs. QEMU execution validates the adapter on this Cortex-M33 runtime proxy;
physical boards and other allocator configurations still require their own
qualification.

## Flat-build pthread key patchset

The pinned NuttX submodule and fork are not modified. At build time,
`tools/apply-nuttx-patches.py` applies the ordered, ordinary unified diff in
`upstream/nuttx/patches/` to the freshly archived `$OUT/nuttx` copy. It checks
the patch against the exact source context, fails closed on a source mismatch
or repeat application, and writes `nuttx-patches.json` with the NuttX revision,
patch digest, and before/after digests of every changed file. When moving to a
newer NuttX version, either the patch still applies cleanly or the build stops
so its semantics can be reviewed; there is no fuzzy application. The same
series is applied to standalone matched C firmware, preserving the kernel
configuration comparison.

The opt-in `CONFIG_TLS_GLOBAL_KEYS` change uses an image-wide pthread key
namespace and destructor table in flat builds. Per-thread TLS values remain
per-thread. Rust std's key-based TLS guard defers `thread_cleanup` until a
later destructor pass, so the build also sets `CONFIG_TLS_DTOR_ITERATIONS=4`
and clears each TLS value immediately before invoking a real destructor.
Keys without destructors keep their values until all passes finish; Rust's
`CURRENT` handle must remain visible to the deferred cleanup guard. The
global key table therefore tracks whether a key is allocated separately
from its optional destructor. Key scope defaults group-local and destructor
passes default to one when left at the upstream default. Nxrs flat Rust std
builds require these settings. The global key limit is
`CONFIG_TLS_NELEM` for the whole image, which is appropriate for the cached
keys in this linked runtime but should be considered before upstreaming.

The [protected CoreS3 qualification](../tests/nuttx-std/imu-protected/README.md#rust-runtime-witness)
uses one direct-init Rust task group with group-local pthread keys and four
destructor passes. Its target runtime witness checks thread cleanup across
repeated worker starts and joins.

The host patch tests exercise application, provenance, source drift, and
reapplication. The MPS2 QEMU check remains the behavioral gate for destructor
execution and complete heap reclamation.

## Previous repeated-launch failure

The [exact-head MPS2 run](https://github.com/yongkyuns/nxrs/actions/runs/36666402422)
passes host tests but fails the strict heap gate for both apps. After warm-up,
`std-demo` rises from 15,160 to 16,656 used bytes on the first checked launch;
`ao-stress` rises from 19,104 to 28,384 bytes. Further launches keep growing.
The assertions remain exact and have not been relaxed.

The failed-run `/proc/memdump` traces identify retained allocations in
`std::thread::Builder::spawn_unchecked_` (`Arc` thread handles) and Rust
thread-local initialization. In this flat NuttX image, Rust `std` caches a
`pthread_key_t` in an image-wide `LazyKey`; NuttX stores key destructors in
each task group's `task_info_s`. A later NSH launch makes a fresh task group
without recreating the cached key, so the Rust thread-cleanup destructor is
absent. That leaves a worker's current-thread handle and other TLS allocations
retained after join. The MPS2 diagnostic image enables NuttX backtraces and
Rust unwind tables to make these allocation sites visible; ordinary Pico 2
build flags are unchanged.

The product model of one Rust `main()` per MCU boot does not exercise this
relaunch mismatch. The exact repeated-launch gate remains unchanged; a larger
leak allowance or one-boot substitute would test a different contract.

Primary pinned NuttX implementation references:

- `fs/procfs/fs_procfsmeminfo.c` at the nxrs-pinned NuttX commit;
- `fs/procfs/fs_procfsproc.c` for `StackSize` / `StackUsed`;
- the nxrs MPS2 platform profile for coloration/procfs qualification.
