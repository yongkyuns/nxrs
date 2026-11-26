# NXRS Memory Explorer

Open a final firmware ELF, its GNU linker map, captured NuttX heap/stack
snapshots, and execution traces in a native egui viewer. The board overview
shows declared storage banks and load/runtime relationships. The address canvas
preserves placement and gaps while you zoom, pan, search, and inspect individual
extents. Source colors connect linked objects and recorded allocation call
stacks to those extents. The timing view shows observed execution intervals,
interrupt nesting, application scopes, and counters on an independent clock.

This is the offline foundation of [issue #442](https://github.com/yongkyuns/nxrs/issues/442).
It shows static placement, discrete memory observations, and imported timing
events. Continuous allocation-event replay into heap geometry, automatic
memory/trace clock correlation, live transport, and physical-device capture
qualification remain follow-up work in that issue.

## Open the viewer

Run from the NXRS repository root with its pinned Rust 1.90 toolchain:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- --demo
```

The demonstration contains invented addresses, sources, snapshots, backing
mappings, and timing events. Its banner identifies it as synthetic. Use it to try the controls
before collecting firmware evidence.

The package is a non-default workspace member. Its library has no GUI dependency
unless `gui` is selected; egui/eframe stays in this host tool. The native viewer
uses eframe 0.33.3 with the Glow renderer and keeps the repository's existing
Rust toolchain. Linux needs a graphical session, an OpenGL implementation, and
the native X11/Wayland development prerequisites; the focused
[workflow](../../.github/workflows/memory-explorer.yml) records the Linux packages
used for CI. Native build/test jobs also cover macOS and Windows.

For an existing build, supply the exact final ELF and its map. For example, after
the normal [NuttX build](../../docs/build-system.md) for `std-demo`:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf target/build/std-demo/mps2-an521-mock/nuttx/nuttx \
  --map target/build/std-demo/mps2-an521-mock/nuttx/nuttx.map
```

The ELF alone is enough to see linked sections and symbol labels. The map adds
retained object/archive contributions. It must come from the same link: the
importer checks matching allocated output-section addresses and sizes, but a
GNU map has no cryptographic build identity of its own.

An empty invocation opens the input form. **Open files** also opens it; paste
paths or drag an ELF, `.map`, and JSON files into the window, then select **Load
memory evidence**. `--help` prints all command options without opening a window.

## Navigate an address space

| Task | Control |
| --- | --- |
| Choose a CPU/load/image window or physical resource | Select its sidebar card |
| Inspect board banks, aliases, and load placement | Select **Board overview**, then a resource or address link |
| Inspect exact range, source, role, and containing object | Click a block |
| Fit a block | Double-click it, or select **Zoom to block** |
| Fit the selected region | **Fit region**, `F`, or `Home` |
| Zoom around an address | Scroll over the canvas; the pointer anchors the zoom |
| Pan | Drag the canvas, scroll horizontally, or press the left/right arrow key |
| Zoom with the keyboard | `+` / `-` |
| Jump to an exact address | Enter hexadecimal in the address field and select **Go** |
| Find a source, section, object, or symbol | Use the sidebar search; select a result to focus it |
| Inspect an allocator arena or task stack | Select it under **Captured arenas and stacks** |
| Choose an observation | Click a timeline point, **Previous** / **Next**, or `Page Up` / `Page Down` |
| Inspect a gap between observations | Click between timeline points |
| Change coordinates | **Runtime / image** or **Physical backing** |
| Hide optional ELF symbol labels | Clear **ELF symbols** |
| Compare heap statistics at two observations | Choose **Compare with** below the memory observation controls |
| Choose the allocation frame used for source coloring | Use **Color allocations by**, or click a recorded frame in the inspector |

Search dims nonmatching blocks without moving them. Byte geometry therefore
stays meaningful during filtering. Small or zero-size symbol labels use ticks;
they are annotations and do not add occupied bytes. The inspector exposes full
labels when a block is too narrow to contain its text.

The canvas has separate layers for linked sections, source objects/symbols,
captured heap or pool state, and task stacks. A task stack contained in an
allocated chunk overlays that allocation; it does not consume the same bytes a
second time. Source colors have a legend, while free, reserved, and unknown
states have distinct styling and explicit labels.

**Caller candidate** selects the first resolved recorded frame outside recognized
allocator files and functions. This is a display heuristic, not proof of semantic
ownership. **Captured source** retains the exporter's original source, and
**Recorded frame N** lets you choose an exact saved frame. Unresolved frames stay
visible and unattributed. The full call stack is resolved from saved PCs using
the matching host ELF; the exporter does not unwind a running target.

Checkpoint comparison reports changes in allocated bytes, free bytes, and the
largest free chunk for each matching arena. It compares the two observed
endpoints. An address reused between observations does not establish that the
same allocation survived. Timeline labels show observation order and phase;
manual coordinates are not presented as measured elapsed time.

## Read the coordinates correctly

| View | What an address means | Evidence required |
| --- | --- | --- |
| CPU window | The ELF's linked virtual memory address, or the address recorded by a snapshot | ELF sections and/or capture |
| Linked load window | A file-backed section's placement derived from a containing `PT_LOAD` segment's `p_paddr` | Consistent ELF section/segment file and virtual-address ranges |
| ELF file offsets | A byte offset in the supplied ELF container | Section file ranges |
| Physical backing | An offset within a named storage resource with a declared capacity | Explicit layout mapping |

CPU addresses do not establish unique physical storage. Two CPU windows can
alias the same SRAM. A supplied layout maps each window to a resource and byte
offset, and the physical view uses that resource's full capacity. Accounting
unions aliased ranges rather than adding their bytes twice.

Without a layout, inferred ELF windows cover the allocated sections visible in
the file. They do not establish total board RAM or flash capacity. The viewer
labels their backing as unknown. Empty space is never inferred to be free heap:
only captured `free` chunks establish that state.

The `.data` section has CPU storage and file-backed initializer bytes. The
overview links its runtime address, linked load address, and ELF file offset.
Distinct load and runtime backing locations consume separate storage; aliases
to the same backing bytes count once even when their CPU addresses differ.
This relationship describes linker placement,
not an observation that startup executed a copy. `.bss` has CPU storage without
file bytes. Other NOBITS sections, including `.heap` and
`.noinit`, are reservations whose initialization policy cannot be inferred from
NOBITS alone. Thread-local storage templates do not establish the addresses or
number of live per-thread instances, so the importer warns and avoids inventing
those CPU allocations.

ELF offsets are not programmed flash offsets. Linked load addresses do not
establish flash technology, bootloader headers, vendor image transformations,
or the contents of a postprocessed binary. File-backed segment padding remains
an occupied load reservation. Reuse of load-only staging RAM would need explicit
lifecycle evidence before those reservations could be released. The importer requires a
final `ET_EXEC` ELF with allocated section headers; relocatable objects, archives,
PIE/`ET_DYN` files needing a load bias, and section-header-free images are rejected.

### Verified MPS2 QEMU layout

The supplied [MPS2-AN521 descriptor](layouts/mps2-an521-qemu.json) records the
QEMU 10.0.13 memory topology and the pinned NuttX board mapping, with source
links and limitations in its `warnings`. Pass it using:

```text
--layout tools/memory-explorer/layouts/mps2-an521-qemu.json
```

The descriptor declares seven resources containing eight banks: 4 MiB SSRAM0,
two adjacent 2 MiB SSRAM banks, four 32 KiB SRAM banks, and the 16 MiB system-RAM
window. Secure/nonsecure and mirror address windows share their declared backing.
The two adjacent banks remain one 4 MiB coordinate resource with a visible bank
boundary, so the allocator's continuous arena and largest free chunk are not
artificially split. Each resource bar has its own explicitly labeled linear
scale. The map describes address translation; it does not certify security
permissions or substitute for a physical-board memory map.

## Enable diagnostic recording

For flat NuttX builds using the default allocator, enable diagnostics in the
product profile:

```toml
[build]
diagnostics = true
```

Use that setting with the normal `cargo nxrs build` workflow.
The diagnostic setting enables heap request counters, stack coloration, and
bounded scheduler, interrupt, and heap instrumentation. Recording is controlled
at runtime by `trace start` and `trace stop`; enabling instrumentation does not
mean that every event since boot was recorded. See the
[capture guide](../../docs/nuttx-memory-qualification.md#diagnostic-builds)
for its buffer, stack, target, and trace-mode requirements.

Addressed heap snapshots are a separate debugger observation. They require
allocator/task debug types and a quiescent stopped target. Allocation call-stack
depth depends on `CONFIG_MM_BACKTRACE`, the selected target unwinder, and the
linked code's unwind information; the diagnostic flag alone is not a claim
that every target supplies a complete Rust call chain. Match all inputs to the
exact final ELF. Qualify the trace clock on each target before interpreting
durations.

## Capture a NuttX lifecycle point

The [GDB exporter](gdb_export.py) reads the default NuttX allocator and task
metadata. Its supported layout is the repository's pinned NuttX revision
`2b5c0de5e933281c209f954a63b99cf10c132346`. Use a Python-enabled target GDB,
the pinned NuttX `pynuttx` dependencies, and the exact final ELF with allocator
and task debug types. Follow the target's debugger procedure to connect and
halt every relevant CPU at a known quiescent lifecycle point.

In that GDB session, with the repository root as the working directory:

```gdb
source external/nuttx/tools/pynuttx/gdbinit.py
source tools/memory-explorer/gdb_export.py
nxrs-memory-export /tmp/ready.json --label ready --time-ns 0
```

Resume the application through the workload, stop again at the intended
observation boundary, and export another file:

```gdb
nxrs-memory-export /tmp/after-session.json --label after-session --time-ns 1
```

Here `0` and `1` are manually supplied ordering coordinates. The exporter does
not sample a target clock or claim a one-nanosecond workload. Supply measured
nanosecond coordinates only when an independent clock establishes them. Every
loaded point needs a distinct coordinate. Each output file is created
exclusively, so a new capture cannot silently overwrite an earlier one.

The exporter discovers registered heaps when available and otherwise uses
`g_mmheap`. Repeated `--heap` options select named globals explicitly, for
example `--heap g_mmheap --heap g_kmmheap`, when those symbols exist in the
selected build. The option accepts symbol names, not arbitrary GDB expressions.

Open the captures with their ELF:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf target/build/std-demo/mps2-an521-mock/nuttx/nuttx \
  --map target/build/std-demo/mps2-an521-mock/nuttx/nuttx.map \
  --snapshot /tmp/ready.json \
  --snapshot /tmp/after-session.json
```

Each snapshot includes the SHA-256 of GDB's loaded host ELF. The importer refuses
to combine it with a different ELF. This guards against mixing host artifacts;
it does not attest that the device is running those bytes.

The exporter checks that every thread GDB exposes is stopped and that raw chunk
extents form a valid arena. It does not call target functions, drain deferred
frees, read allocation payloads, or modify target state. A debugger can hide a
CPU, and valid topology does not prove the allocator was stopped between
transactions. Choose an application quiescence point appropriate to the target.

### What the snapshot establishes

| Record | Meaning and limit |
| --- | --- |
| Arena and chunks | Raw allocator extents including allocator overhead and guard nodes |
| Free chunk | Explicit observed allocator free extent; alignment and metadata can reduce a usable request |
| Deferred free | Remains allocated until the allocator actually reclaims it |
| Allocation source | All recorded backtrace PCs with function, file, and line when resolvable; the original first-frame source is retained for compatibility |
| PID/sequence metadata | Allocator-recorded fields when present; not a durable allocation lifetime ID or current semantic owner |
| Task stack | Usable reserved bounds when they can be represented within the observed containing chunk |
| Stack use/high-water | Unknown in this adapter; no coloration scan is performed |
| Pool backing | Parent allocation visible; individual slot occupancy unknown |

Some NuttX shared-boundary layouts put usable stack bounds outside the strict
raw chunk interval. The exporter preserves their raw task metadata and emits a
warning instead of drawing a false contained stack. Missing backtrace/debug
information leaves source attribution unknown. Static map colors identify
objects or archive members; they do not claim unique original Rust source lines
under LTO, code folding, or merged constants.

Per-arena statistics expose allocated chunk bytes, reserved bytes, free chunk
bytes, largest free chunk, and unobserved bytes. They are scoped to one arena:
free space in a nested pool is not interchangeable with free space in its
parent heap. Requested allocation sizes remain unknown, so this adapter does
not calculate internal fragmentation from guessed payload lengths.

Only exact captured points have runtime state. Between points the runtime layers
are hidden and marked unknown. Pointer reuse and allocate/free operations between
those points cannot be reconstructed from snapshots.

## Qualify a real QEMU capture

The [QEMU harness](tests/qualify_qemu.py) runs the existing `std-demo` MPS2
firmware with a Python-enabled `gdb-multiarch`. It captures three stopped points:
NSH ready, entry to `std_demo_main`, and return to NSH after
`std_demo --case channels`. A separate debugger walk reads allocator headers
and task metadata directly from target memory, compares those facts with the
exported JSON, and checks that the observed metadata is unchanged across each
export. The workload must pass, its entry task must retire, and heap topology
must change across both transitions. No target function is invoked to collect
the observations.

Use a POSIX host, host `readelf`, and QEMU with the corrected MPS2 UART wiring
(present in upstream [9.2.0](https://github.com/qemu/qemu/blob/v9.2.0/hw/arm/mps2-tz.c);
8.2.2 has the old wiring). Reuse an existing diagnostic build, or build
it through the normal frontend. Run these commands from the repository root:

```sh
firmware="$PWD/target/build/std-demo/mps2-an521-mock"
capture="$PWD/target/memory-explorer-qemu/qualification-001"

NXRS_NUTTX_DIAGNOSTIC_UNWIND=1 cargo +1.90.0 nxrs build \
  --app std-demo --profile mps2-an521-mock --out "$firmware"

python3 tools/memory-explorer/tests/qualify_qemu.py \
  --qemu "$(command -v qemu-system-arm)" \
  --gdb "$(command -v gdb-multiarch)" \
  --image "$firmware/nuttx/nuttx" \
  --config "$firmware/resolved.config" \
  --pynuttx "$firmware/nuttx/tools/pynuttx" \
  --output "$capture"

NXRS_MEMORY_QEMU_EVIDENCE="$capture" \
NXRS_MEMORY_QEMU_ELF="$firmware/nuttx/nuttx" \
  cargo +1.90.0 test --locked -p nxrs-memory-explorer \
    --test qemu_capture --test board_projection -- --ignored --nocapture
```

Choose a **new output directory for every run**; the harness refuses an existing
directory. It uses a bounded QEMU/GDB pipe session and records the exact ELF and
configuration hashes, tool versions, console/GDB logs, `raw-0.json` through
`raw-2.json`, `capture-0.json` through `capture-2.json`, and `report.json`.
The Rust test is ignored in ordinary host test runs and requires those real
observations explicitly. Absolute environment paths avoid Cargo's package
working-directory ambiguity.

Open the three points in the viewer:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf "$firmware/nuttx/nuttx" \
  --map "$firmware/nuttx/nuttx.map" \
  --layout tools/memory-explorer/layouts/mps2-an521-qemu.json \
  --snapshot "$capture/capture-0.json" \
  --snapshot "$capture/capture-1.json" \
  --snapshot "$capture/capture-2.json"
```

The coordinates `0`, `1000000000`, and `2000000000` are manually assigned ordering
coordinates, not measured elapsed time. The middle point precedes the Rust
workload; it does not capture its peak or every transient worker. Unchanged
captured metadata establishes no observed exporter mutation of those fields,
not proof about all memory or every possible stopping point. Deferred frees
remain allocated in this read-only view and are not by themselves a leak finding.
The optional board projection checks that aliases and bank boundaries preserve
every captured chunk and arena statistic. Without a layout, CPU backing remains
unknown. This is NuttX/QEMU execution evidence, not physical-device or timing
qualification.

The existing [std-apps ARM workflow](../../.github/workflows/std-apps-self-hosted.yml)
reuses its `std-demo` build for this capture/import gate after the existing
lifecycle checks and retains the memory-explorer evidence with the firmware
artifact. The [qualification guide](../../docs/nuttx-memory-qualification.md#real-qemu-capture-witness)
records the initial real-target witness and its scope.

## Inspect detailed timing

The viewer imports the [Chrome Trace Event JSON format supported by Perfetto](https://perfetto.dev/docs/getting-started/other-formats).
NuttX text dumps use the existing converter:

```sh
python3 tools/nuttx-trace-to-perfetto.py console.txt timing.json \
  --elf path/to/the/captured/nuttx

cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --trace timing.json --view timing
```

Replace the ELF path with the exact final executable for that capture. A trace
can be opened alone, or beside `--elf`, `--map`, `--layout`, and snapshots from
the same build. Explicit conflicting ELF hashes are rejected. An unspecified
trace hash stays unknown; loading two files does not establish their identity.

| Task | Control or meaning |
| --- | --- |
| Navigate time | Pointer-anchored scroll zoom, drag to pan, arrow keys, `+` / `-`, **Fit trace** or `F` |
| Inspect a task, interrupt, or application interval | Click a span; double-click to fit it |
| Measure arbitrary duration | Enable **Measure range** and drag across time, or use Shift+drag |
| Measure an exact recorded interval | Select it and choose **Measure this interval** |
| Find a task or operation | Filter by track, event name, category, or recorded arguments |
| Inspect counters | Enable **Counters** and hover a recorded sample |
| Read the axis | Time relative to the first imported record; the inspector shows exact trace coordinates and source-clock timestamps when an explicit origin is available |
| Assess observation coverage | Per-track union of recorded intervals; missing intervals remain unknown |

CPU and task tracks show scheduled residency. IRQ and application scopes can
overlap that residency, so their durations must not be added together as CPU
utilization. Nested intervals are shown on separate rows, and the selection
summary counts their union once within each track. Counters show samples without
inventing intermediate values. Scheduler switches, nested IRQ entry/exit pairs,
application `B/E/I/C` markers, and supported heap events retain their observed
timestamps and arguments. Heap request events are not replayed into addressed
chunk geometry: a failed request is not an allocation, and complete allocator
identity/size semantics are required for safe replay.

Trace Event JSON `ts` and `dur` fields are **microseconds**, even when
`displayTimeUnit` is `ns`. The importer converts exact decimal numbers to integer
nanoseconds without passing timestamps through floating point. It supports
complete `X`, nested `B/E`, instant `i/I`, counter `C`, and metadata `M` events.
Unsupported or incomplete events fail rather than receiving an invented end.
The NuttX converter instead retains incomplete interval observations as explicit
instant markers with no duration, and records the incomplete count.

`otherData.nxrs` carries explicit clock, source, original timestamp origin,
optional ELF SHA-256, loss status, completeness, and warnings. Missing metadata
is shown as unknown. `--clock`, `--source`, and loss options on the converter are
producer assertions; use them only when supported by the actual capture. A
backward timestamp across paired scheduler/IRQ observations on one CPU fails conversion. Known
loss resets pending interval pairs; loss without a located boundary disables
duration reconstruction. No blank interval is interpreted as idle. Nanosecond
coordinates do not promise nanosecond clock resolution.

Memory checkpoints and timing traces retain independent axes. Matching ELF
identity does not synchronize their clocks. In particular, the three manually
ordered GDB observations above are never aligned to this timing axis. Continuous
heap history and synchronized memory/timing cursor navigation require an explicit
common target clock and event sequence in a future capture adapter.

The current viewer is qualified with bounded diagnostic captures. It loads the
trace into host memory and scans events for filtering and per-track painting.
The importer limits files to 128 MiB, one million input records, and 1,024
nesting levels; those are admission limits, not demonstrated interactive
performance at those sizes. Large-recording indexes and vertical track
virtualization remain follow-up work.

The [native timing witness](../../tests/nuttx-std/memory-timing/) and its
[build](tests/build_timing_nuttx.py) / [capture](tests/qualify_timing_qemu.py)
helpers exercise the importer with actual NuttX/QEMU records. Their dedicated
CMSDK timer adapter is confined to the qualification fixture. It is not a
production clock override. See the
[qualification evidence and commands](../../docs/nuttx-memory-qualification.md#qemu-timing-witness)
for its exact target, clock, rejected-clock controls, and scope.

## File format

Version 1 uses UTF-8 JSON for offline observations and an optional separate
layout file. The authoritative Rust types and validation live in
[model.rs](src/model.rs); the ELF/map adapter lives in [import.rs](src/import.rs).
The model is independent of egui so another capture transport can populate it
without changing geometry or accounting rules.

All addresses, sizes, and coordinates are unsigned 64-bit JSON integer numbers.
Hexadecimal is a display/input convenience in the GUI, not a JSON syntax. Ranges
are nonempty half-open intervals `[start, start + size)`; overflow is rejected.
Producers must preserve integer precision, including values above JavaScript's
exact-number range.

| Snapshot field | Contract |
| --- | --- |
| `schema_version` | Exactly `1`; unsupported versions fail |
| `build` | `elf_sha256` as 64 hex digits, plus optional `label` |
| `regions` | Named address windows: `id`, `name`, `start`, `size`, `kind`, `domain`, optional `physical` |
| `physical_resources` | Optional named resource capacities: `id`, `name`, `size`; optional `banks` partition the resource by `name`, `offset`, and `size` |
| `snapshots` | Ordered observations with `time_ns`, `label`, `spans`, optional `complete_regions` |
| `warnings` | Optional evidence limitations displayed by the viewer |
| `capture` | Optional exporter-specific metadata retained in the input file; not permission to weaken validation |

A span contains `id`, `name`, `region`, `start`, `size`, and `kind`, plus optional
`source`, `parent`, `detail`, and `backtrace`. A backtrace is a list of recorded
`pc` values with optional `function`, `file`, and `line`. Kinds are `code`, `read_only`, `data`, `bss`,
`heap_arena`, `allocation`, `free`, `stack`, `stack_used`, `pool`, `reserved`, and
`unknown`. Parents represent containment; sibling overlaps, missing parents,
cycles, out-of-region spans, and conflicting free/live evidence fail validation.
Symbols stay separate from ownership spans because legal symbol aliases overlap.

`complete_regions` asserts complete chunk coverage of the captured arenas in
those regions. It does not assert that every byte in a larger board RAM window
was observed. Missing arena coverage stays unknown unless a complete-coverage
claim requires the importer to reject it.

### Declare board storage

A layout contains `schema_version`, `regions`, `physical_resources`, and optional
`warnings`. Address domains include `cpu_virtual`, `linked_load`, `image`, and
`physical`; linked load placements are derived from the ELF, not guessed from
the region's name. The
following complete example describes an invented 8 KiB SRAM window at
`0x20000000`; replace it with the selected board/profile's verified map:

```json
{
  "schema_version": 1,
  "physical_resources": [
    { "id": "sram", "name": "Example SRAM", "size": 8192 }
  ],
  "regions": [
    {
      "id": "ram",
      "name": "Example CPU SRAM window",
      "start": 536870912,
      "size": 8192,
      "kind": "ram",
      "domain": "cpu_virtual",
      "physical": { "id": "sram", "offset": 0 }
    }
  ]
}
```

Pass it with `--layout layout.json`. A mapping translates `region.start` to
`physical.offset` in the named resource and preserves byte offsets throughout
the window. Another nonoverlapping CPU window can map to the same backing range
to declare an alias. Bounds and conflicting observations are checked. A larger
layout window may contain a snapshot's smaller heap window; the importer remaps
the capture only when containment and any explicit backing agree.

Version 1 only expresses linear mappings. It does not infer ESP32 IRAM/DRAM
aliases or RP2350 striped bank mappings. Leave unsupported backing unmapped
until a validated board adapter can represent it. This keeps CPU geometry useful
without presenting invented physical precision.

## Verify changes

The model/import tests run without a graphics dependency. On Linux the independent
fixture also requires Python 3, `cc`, and GNU `ld`:

```sh
cargo test --locked -p nxrs-memory-explorer
python3 -m unittest discover -s tools/memory-explorer/tests -p 'test_*.py' -v
cargo test --locked -p nxrs-memory-explorer --features gui --bin nxrs-memory-explorer
cargo clippy --locked -p nxrs-memory-explorer --all-targets --features gui -- -D warnings
```

The linked fixture is built by the system compiler/linker from fixed-address C
inputs. The exporter fixture runs the real Python serializer against explicitly
synthetic debugger objects. Their integration checks exact placement, map
attribution, NOBITS treatment, nested stack accounting, hash mismatch rejection,
and unknown intervals independently of the GUI's demo.

To render that fixture locally:

```sh
python3 tools/memory-explorer/tests/make_elf_fixture.py target/memory-explorer-fixture
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf target/memory-explorer-fixture/fixture.elf \
  --map target/memory-explorer-fixture/fixture.map \
  --layout target/memory-explorer-fixture/layout.json \
  --snapshot target/memory-explorer-fixture/exporter-snapshot.json
```

Add `--smoke-test --screenshot /tmp/nxrs-memory-smoke.ppm` to exercise geometry,
navigation, and available evidence paths, save the actual rendered framebuffer,
and exit. CI runs the demo and linked fixture through a software-rendered Xvfb
session and uploads their PPM images. Inspect those images as well as the test
results. Host fixtures and rendered UI checks do not qualify a live NuttX GDB
capture or a physical board's memory map.

For existing lifecycle retention and stack high-water checks, continue using
[NuttX heap and stack qualification](../../docs/nuttx-memory-qualification.md)
and [Rust allocation qualification](../../docs/memory-qualification.md).
