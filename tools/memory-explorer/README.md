# NXRS Memory Explorer

Open a final firmware ELF, its GNU linker map, captured NuttX heap/stack
snapshots, and execution traces in a native egui viewer. The overview explains
code, constants, static variables, and the linker's named memory budgets. An
optional physical layout adds storage banks, aliases, and load/runtime
relationships. The memory view shows placement, readable purpose colors, and
per-heap usage history. Timing has a timeline, a searchable event list, and
source summaries with resolved IRQ handlers and allocation callers. Explicitly
matched checkpoint markers link execution times to captured memory.

This is the offline foundation of [issue #442](https://github.com/yongkyuns/nxrs/issues/442).
It shows static placement, discrete memory observations, imported timing events,
and a separately validated heap topology journal. The first replay collector is
an isolated QEMU qualification fixture. Diagnostic product builds record native
scheduler/IRQ/heap events and named IMU/GNSS work. Production heap-topology
recording, live transport, and physical-device capture qualification remain
follow-up work in that issue.

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
retained object/archive contributions and named `MEMORY` region budgets. It must come from the same link: the
importer checks matching allocated output-section addresses and sizes, but a
GNU map has no cryptographic build identity of its own.

An empty invocation opens a new recording tab. **+ New** and **Open recording**
add a fresh input tab. Paste paths or drag an ELF, `.map`,
and JSON files into the window, then select **Load evidence**. A
[prepared capture session](#package-and-reopen-a-diagnostic-session) loads
matching inputs together with `--session session.json` or **Open session**.
`--help` prints all command options without opening a window.

### Keep several recordings open

Each recording has its own tab, evidence, memory selection, timing cursor,
filters, zoom, scroll position, and replay state. Opening another recording
preserves the one already loaded. Switching tabs uses the retained model and
does not reread the capture or align its clock with another recording.

Open several sessions at startup by repeating `--session`:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --session target/baseline-session/session.json \
  --session target/diagnostic-session/session.json
```

You can also drop several session files or directories into the window, or
drop multiple standalone traces. Raw ELF/map/snapshot groups need one clear
owner: drop one build's artifacts together, then load them in a new tab.
An invalid session leaves the loaded recordings intact. Tab tooltips show
the full source path, build identity, and recording identity when available;
tab numbers distinguish recordings with the same filename.
An initial `--view` applies to the first tab; other recordings retain their
own default view, so timing and memory-only sessions can be opened together.

| Task | Control |
| --- | --- |
| Add an empty recording tab | **+ New**, `Ctrl+T` / `Cmd+T` |
| Switch recordings | Click a tab, `Ctrl+Tab`, or `Ctrl+Shift+Tab` |
| Close a recording | Its **×**, middle-click its label, or `Ctrl+W` / `Cmd+W` |

Closing a tab releases its loaded data. Closing the last tab opens an empty
input tab. Recordings remain independent even if their ELF hashes match.

## Navigate an address space

| Task | Control |
| --- | --- |
| Choose a CPU/load/image window or physical resource | Select its sidebar card |
| Inspect board banks, aliases, and load placement | Select **Memory overview**, then a resource or address link |
| Understand firmware without a board descriptor | Open **Memory overview** for named linker regions, section purposes, and direct address links |
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
| See heap usage change | Choose an arena in **Memory usage history**, then Allocated, Free, Largest free, or All |
| Distinguish code, data, allocations, and known free bytes | Keep **Memory purpose** selected; choose source colors for attribution |
| Find the largest live allocations | Clear the selection and use the inspector's largest-chunk links |
| Inspect a gap between observations | Click between timeline points |
| Change coordinates | **Runtime / image** or **Physical backing** |
| Hide optional ELF symbol labels | Clear **ELF symbols** |
| Compare heap statistics at two observations | Choose **Compare with** below the memory observation controls |
| Choose the allocation frame used for source coloring | Use **Color allocations by**, or click a recorded frame in the inspector |
| Open a linked checkpoint on its execution timeline | Select **Show in timing** below the memory observation |
| Reconstruct one admitted heap | Load `--heap-journal`, then select **Heap replay** |
| Distinguish commits sharing one clock tick | Select an exact sequence in the heap replay controls |

Search dims nonmatching blocks without moving them. Byte geometry therefore
stays meaningful during filtering. Small or zero-size symbol labels use ticks;
they are annotations and do not add occupied bytes. The inspector exposes full
labels when a block is too narrow to contain its text.

The default purpose legend explains code, constants, initialized data, BSS,
reservations, allocations, and known free chunks. Section jump buttons focus
small sections without requiring address arithmetic. Source colors remain
available for object and allocation attribution.

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

Memory history accounts for each selected arena separately and excludes stack
overlays and nested arena containers. Missing bytes remain unobserved. The
default Allocated plot uses its own scale, so a large free heap does not flatten
allocation growth. Its peak is the largest retained observation; samples do
not prove an intervening peak. Snapshot points are not joined into invented
continuous state. An admitted replay journal can hold state across its validated
coverage; lost or missing mutations break the history until a complete checkpoint.

## Find the source of timing and allocation work

Use **Timeline**, **Events**, and **Source summary** in the timing toolbar, or
start with `--view timing`, `--view timing-events`, or `--view timing-sources`.
Category shortcuts isolate IRQs, allocation requests, application scopes, or
timers. The minimum-duration filter applies to intervals and retains instants.
The event list is chronological; click for details or double-click to focus
the corresponding timeline range. Source summaries show count, total observed
duration, maximum duration, and request bytes where the producer records them.
Overlapping durations are inclusive and are not CPU-utilization percentages.

Convert with `--elf` and an appropriate `--addr2line` when necessary. An IRQ
inspection then shows its native vector, handler address, resolved function and
file/line, and the separately recorded interrupted task. A shared GPIO or UART
handler does not identify its attached sensor. Diagnostic IMU and GNSS builds
also emit task-side `imu/<device>/...` and `gnss/<device>/...` scopes, so the
timeline can show which named device wakes, drains, performs bus transfers, or
reads/parses/delivers events. Those scopes identify measured work; they do not
assert an unrecorded causal edge from an interrupt.

Heap request details retain the native caller PC, resolved source, operation,
requested size, task, and CPU. A request entry does not establish success or
failure; the viewer reports an unknown outcome unless the producer records it.
Successful heap observations retain their native operation, address, size, and
used-byte counter. They show usage at those events, but inconsistent native
pointer conventions across allocation functions prevent treating them as an
ordered allocation-lifetime journal. The separate validated journal remains
the source for exact heap replay.

**Capture diagnostics** reports native record counts, IRQ/caller resolution
coverage, incomplete intervals, and loss evidence. Missing heap or application
events are visible there. Wake-to-dispatch intervals are emitted only where
the retained wake and dispatch observations establish unambiguous endpoints.

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
the file. With a compatible GNU map, nonoverlapping `MEMORY` declarations instead
supply named CPU regions and their link-time budgets. Neither establishes unique
physical board RAM or flash capacity. Overlapping linker declarations are omitted
with a warning, and explicit layout mappings take precedence. The overview stays
usable in both cases. Empty space is never inferred to be free heap:
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

### Automatic board layouts

The viewer bundles memory descriptors in [layouts](layouts/) and retains their
pinned BSP source material under [upstream/memory-layouts](../../upstream/memory-layouts/).
The [catalog](layouts/catalog.json) maps the repository's
CoreS3, ESP32-S3 DevKit/WROOM, Pico 2/2W, and MPS2 AN521 firmware boards to
their descriptors. Host, web, and NuttX simulator profiles use process address
spaces; they do not acquire a physical MCU bank map.

| Board/profile family | Explicit `--board` ID |
| --- | --- |
| M5Stack CoreS3 | `esp32s3-cores3` |
| ESP32-S3 DevKit / WROOM | `esp32s3-devkit` |
| Pico 2, NSH profile | `raspberrypi-pico-2` |
| Pico 2 / 2 W, USB NSH profile | `raspberrypi-pico-2-usbnsh` |
| MPS2 AN521 QEMU | `mps2-an521-qemu` |

For a normal `cargo nxrs build` output, opening the final ELF is enough to
discover its sibling linker map and a matching bundled layout. The viewer
checks the final ELF and effective configuration against `binaries.sha256`,
then matches the board and pinned NuttX revision recorded in `provenance.txt`.
Source identity and selection notes appear with the memory overview. No network
access is needed to open a recording.

If the build receipts are unavailable, choose the known board in the input
form or pass `--board` with a catalog ID. That is an explicit board declaration;
the viewer labels the descriptor's scope and assumptions. A custom `--layout`
remains available. An automatically discovered adjacent `layout.json` must be
bound to the selected ELF's SHA-256. Conflicting or incomplete metadata leaves
the linker/ELF overview available with an explanation.

Fixed on-chip banks and their known aliases come from the retained BSP sources.
Effective build settings can also declare PSRAM or flash capacity. An external
address aperture alone does not establish those capacities, partition ownership,
or an MMU mapping. Those remain separate from the linker's named budgets and
captured allocator state. Automatic layout enrichment preserves captured region
and journal identities; it never relabels a capture to fit a guessed board.

Maintainers can verify the retained source files and retrieve the exact pinned
upstream copies with:

```sh
python3 tools/memory-explorer/refresh_bsp.py --check
python3 tools/memory-explorer/refresh_bsp.py --download
```

The source manifest retains URLs, revisions, checksums, and original license
headers. Updating a BSP requires reviewing its new memory definitions and the
descriptor together. A catalog coverage check flags a new firmware board that
has no descriptor or explicit non-hardware classification.

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
# Optional: default is 32768 bytes, preserving the start of the recording.
diagnostics-trace-bytes = 65536
# Optional: allocator caller stacks; requires the target's working unwinder.
diagnostics-heap-backtrace-depth = 4
```

Use that setting with the normal `cargo nxrs build` workflow.
The diagnostic setting retains native debug symbols, Rust line tables, and the
linker map. It enables heap request counters, stack coloration, bounded
scheduler/interrupt/heap instrumentation, and application markers. Consumed
IMU/GNSS providers select their existing `probe` implementation to name work.
Recording is controlled
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
| Place a time cursor | Click an event to use its exact start, or click blank time |
| Open observed memory from a verified recording | Select a point in **Memory checkpoints**, then **Open captured memory** |

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
optional ELF SHA-256 and recording ID, loss status, completeness, and warnings. Missing metadata
is shown as unknown. `--clock`, `--source`, and loss options on the converter are
producer assertions; use them only when supported by the actual capture. A
backward timestamp across paired scheduler/IRQ observations on one CPU fails conversion. Known
loss resets pending interval pairs; loss without a located boundary disables
duration reconstruction. No blank interval is interpreted as idle. Nanosecond
coordinates do not promise nanosecond clock resolution.

Ordinary memory checkpoints and timing traces retain independent axes. Matching
ELF identity does not synchronize their clocks. In particular, the three manually
ordered GDB observations above are never aligned to this timing axis. A capture
adapter can establish a link through the explicit contract below.

### Link memory checkpoints to timing

Load an ELF, snapshots, and trace produced by the same qualified recording. The
viewer validates their firmware, recording ID, clock, checkpoint markers, and
exact source timestamps before enabling linked navigation. A mismatch fails
loading with a correlation error; it never falls back to an estimated offset.

The timing view then includes a **Memory checkpoints** strip. Click a point and
select **Open captured memory** to inspect its heap and stack reservations. The
memory view's **Show in timing** action returns to the matching marker. Clicking
an arbitrary time places the shared cursor there: runtime memory appears only
at an observed checkpoint. **Show memory evidence gap** opens the static layout
with runtime blocks hidden. Neither the most recent snapshot nor a blend of
nearby snapshots substitutes for an observation.

The [single-run QEMU lifecycle witness](../../docs/nuttx-memory-qualification.md#linked-qemu-lifecycle-witness)
qualifies this path with four real allocator observations and the existing native
timing fixture. The firmware emits each marker inside a single-core critical
section and pauses at a hardware breakpoint. The harness independently reads
the final note in the native ring, compares raw heap/task metadata before and
after export, then matches that note to the UART dump. A fresh recording nonce
identifies this invocation. Debugger stops pause the admitted QEMU timer;
durations describe that instrumented run, not uninterrupted performance.

Production instrumentation still uses the existing `[build] diagnostics = true`
setting. The stopped-checkpoint adapter is currently qualified only for this
native MPS2 QEMU fixture. Enabling diagnostics on another firmware or board does
not by itself establish the quiescence, clock, or marker evidence required here.

The current viewer is qualified with bounded diagnostic captures. It retains
each opened trace in host memory, uses track indexes, and virtualizes visible
rows. The importer limits files to 128 MiB, one million input records, and
1,024 nesting levels; those are admission limits, not demonstrated interactive
performance at those sizes. Several large tabs add their retained memory costs.

The [native timing witness](../../tests/nuttx-std/memory-timing/) and its
[build](tests/build_timing_nuttx.py) / [capture](tests/qualify_timing_qemu.py)
helpers exercise the importer with actual NuttX/QEMU records. Their dedicated
CMSDK timer adapter is confined to the qualification fixture. It is not a
production clock override. See the
[qualification evidence and commands](../../docs/nuttx-memory-qualification.md#qemu-timing-witness)
for its exact target, clock, rejected-clock controls, and scope.

## Replay one heap

A heap journal records committed allocator geometry and the intervals during
which that geometry was being changed. Open it beside its exact ELF and layout:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf /path/to/nuttx \
  --layout tools/memory-explorer/layouts/mps2-an521-qemu.json \
  --heap-journal /path/to/heap-journal.json \
  --view memory
```

The journal's heap must fit a declared CPU-address region. The existing canvas
and physical-backing mappings preserve its actual address and size. Static ELF
placement remains visible in a separate layer. A fixture arena backed by `.bss`
therefore appears as both a static reservation and runtime chunks; these views
describe the same storage and are not added together.

Select **Heap replay** to inspect only that heap, or **Point snapshots** to return
to independent complete observations. Replay never fabricates a whole-memory
snapshot. Other heaps, pool slots, and task stacks remain unknown in replay mode,
even when their last snapshot has a nearby timestamp. The existing
`MemoryModel::snapshot_at` query still requires an exact observation time.

The replay controls distinguish an observed checkpoint, reconstructed committed
state, an allocator mutation in progress, and unavailable evidence. Every commit
has a sequence number independent of the clock. **Previous**, **Next**, and the
sequence selector preserve intermediate states even when several commits share
one clock tick. A time-only selection cannot disambiguate such states. The
inspector shows the selected sequence and evidence instead of treating a reused
address as the same application allocation.

During an admitted mutation window, geometry is unknown. A missing, duplicate,
reordered, or contradictory transaction also invalidates knowledge. Only a
complete recovery checkpoint can restore it. An absent closing receipt does not
allow the last known layout to continue indefinitely. Free chunks retain their
raw allocator extents, including metadata and alignment constraints; their size
does not guarantee that an equally sized allocation request will succeed.

If a trace is loaded too, its ELF, recording identity, clock, and source origin
must match the journal before the timing cursor can navigate heap state. A
matching ELF alone is insufficient. Debugger-observed mutation windows describe
the instrumented recording; they do not establish production allocation latency.

### Why a separate topology journal

The default NuttX request notes are useful for counters and request timing, but
are not a complete record of committed chunk geometry. On the qualified revision,
realloc shrink can alter boundaries without a heap note, aligned allocation has
an intermediate backing allocation, and free is noted before coalescing. Heap
extension also changes guard geometry. The replay engine therefore consumes
complete, ordered chunk replacements rather than interpreting request notes as
addressed history. The collector must independently establish that every admitted
mutation reached its journal.

The initial collector observes a fixture-owned heap under QEMU. It adds no
production allocator patch, task, polling loop, or global allocation wrapper.
`[build] diagnostics = true` remains the product diagnostic entry point; it does
not silently enable this qualification collector or claim continuous production
heap coverage. See the [qualification guide](../../docs/nuttx-memory-qualification.md)
for its admitted source, configuration, hook coverage, and target evidence.

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
| `snapshots` | Ordered observations with `time_ns`, `label`, `spans`, optional `complete_regions` and `correlation` |
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

### Correlation metadata

An optional additive field on each snapshot carries a checked recording claim:

```json
{
  "correlation": {
    "recording_id": "32c749812a07eafbe1f65ac4dcd4402e",
    "clock": "qemu-an521-cmsdk-timer1-20mhz",
    "checkpoint_id": 1
  }
}
```

This is a snapshot-field example, not a complete capture. The recording ID names
one run and reset epoch; producers must not reuse it for another invocation. It
contains 1–128 printable ASCII characters without whitespace. A checkpoint ID
is a nonzero `u64`, strictly increasing with its source timestamp. For correlated
observations, `time_ns` is the exact source-clock timestamp of the checkpoint
marker. It is never a host timestamp or an approximate time sampled nearby.

The trace must declare matching `recording_id`, `clock`, and `elf_sha256` in
`otherData.nxrs`, plus an explicit `origin_ns`. Each loaded observation must match
exactly one instant named `memory/checkpoint/<checkpoint_id>`, satisfying:

```text
snapshot.time_ns = trace.origin_ns + marker.start_ns
```

`marker.start_ns` is the imported integer-nanosecond coordinate; Trace Event JSON
still encodes `ts` in microseconds. Missing markers, duplicate or noncanonical
IDs, a counter/duration with a reserved marker name, overflow, or even a
one-nanosecond disagreement fail validation. All snapshots loaded together must
either be legacy independent observations or correlated points from the same
recording and clock. Additional trace markers whose snapshots were not loaded
are permitted. Known trace loss does not erase a separately verified checkpoint;
the loss warning and unknown intervals remain visible.

The claim travels with each snapshot and the existing trace metadata, preserving
the current file-loading commands and legacy JSON. A separate manifest or a
user-entered time offset would add another identity relationship without proving
the marker-to-memory boundary. The capture adapter owns that proof; the host
checks its consistency. Continuous allocator replay needs a complete mutation
journal and remains a separate capability.

### Heap journal version 1

`--heap-journal` consumes a separate JSON envelope. All addresses, extents,
sequences, and source-clock nanoseconds are unsigned integer JSON numbers.
The parser rejects unknown topology fields, including unqualified allocation
generation or source fields. It admits one heap instance per file.

| Field | Contract |
| --- | --- |
| `schema_version` | `1` for this journal envelope |
| `build` | Exact final ELF SHA-256 and a display label |
| `provenance` | `nuttx_revision`, effective `config_sha256`, and a `collector` token; metadata alone does not certify collection |
| `recording_id`, `clock` | One recording/reset identity and one source-clock identity |
| `heap` | Stable `id`, string `epoch`, declared CPU `region`, `start`, and `size` |
| `initial` | Complete `chunks` at `time_ns`, establishing `sequence: 0` |
| `records` | Ordered transactions, complete checkpoints, and explicit loss reports |
| `closing` | Final `heap_epoch`, `sequence`, and covered `time_ns`; `null` means unclosed |

Each chunk is `{ "start": ..., "size": ..., "state": ... }`, with state
`allocated`, `free`, or `reserved`. The list must tile its declared range in
address order. Guard chunks and allocator overhead stay in their raw extents.
An allocator control structure before the first guard belongs to its backing
reservation, outside the journal's chunk region.

A `transaction` record carries `sequence`, `heap_epoch`, `start_ns`, `commit_ns`,
the affected `start` and `size`, exact `expected` old chunks, `replacement`
chunks, and an optional `label`. Both lists must tile the affected range; the
old list must match current boundaries and states. Replacement is atomic.
Unchanged geometry still consumes a sequence when the collector observed an
allocator release. Dropping a cancelling allocation/free pair therefore creates
a detectable sequence gap even when endpoint geometry is identical.

A `checkpoint` carries `heap_epoch`, `sequence`, `time_ns`, complete `chunks`,
and an optional `label`. It can restore knowledge after loss, but cannot make
the missing interval known. A `loss` record carries `heap_epoch`, `time_ns`,
and `reason`. Detection time does not locate the first lost mutation, so the
unknown interval begins after the preceding proven state boundary.

The library exposes `HeapReplay::heap_state_at(heap_id, cursor)`. Use
`ReplayCursor::Time(source_ns)` for a source-clock coordinate,
`ReplayCursor::Sequence(sequence)` for committed order, or
`ReplayCursor::Point(point_id)` to distinguish observations sharing a sequence.
`HeapState::Known` returns the chunk geometry, evidence kind, boundary, and
coverage. Its `spans()` method returns only that heap's arena and chunks.
`HeapState::Unknown` explains the missing evidence. A syntax/header error rejects
the file; a damaged transaction invalidates the affected suffix until recovery.

The reader limits input to 32 MiB, 4,096 records, 16,384 chunks in a state, and
one million retained chunk records across states. These are host import bounds,
not a target recorder allocation budget. The QEMU fixture separately limits
itself to 256 allocator transactions.

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

## Package and reopen a diagnostic session

Keep a recording's inputs together and open them with one path:

```sh
python3 tools/memory-explorer/prepare_capture.py \
  --elf target/cores3-trace/nuttx/nuttx \
  --console cores3-console.txt \
  --output target/cores3-session

cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --session target/cores3-session/session.json
```

The output parent must already exist; the session directory must be new. The
preparer copies the exact ELF, original console, discovered or explicitly
supplied map/layout, and converted trace. It resolves recorded IRQ handlers and
heap caller PCs against the ELF;
use `--addr2line /path/to/target-addr2line` when automatic discovery cannot find
the target tool. The generated session has only relative filenames, so the whole
directory can be moved without changing paths.

Add `--snapshot path/to/observation.json` for each memory observation and
`--heap-journal path/to/journal.json` for an independently recorded heap journal.
Their explicit ELF hashes must match the selected executable. The preparer also
rejects conflicting host-ELF hashes in snapshot capture metadata and conflicting
ELF identities in native console frames. It does not manufacture recording IDs
or synchronize clocks. The viewer still validates each format, map placement,
memory geometry, and any claimed timeline correlation when opening the session.

The preparer uses the same [automatic board discovery](#automatic-board-layouts)
as the viewer and packages the selected descriptor, its provenance, and the
discovery notes. The resulting session remains self-contained when moved away
from the original build directory. Supply `--layout path/to/verified-layout.json`
for a custom board map, or use `--board` for a known catalog entry when build
metadata is unavailable. For the qualified QEMU machine, the ID is
`mps2-an521-qemu`. These two options are mutually exclusive. Without a matching
layout, the ELF/linker view remains available.

`session.json` uses schema version 1 with `build.elf_sha256`, `elf`, optional
`map`, `layout`, `trace`, `heap_journal`, a `snapshots` array, and a `files` object
mapping every retained filename to `{ "sha256": "...", "bytes": 123 }`.
Optional `automatic_layout` and `automatic_map` flags retain discovery behavior
when reopening: captured region IDs remain stable, and an incompatible optional
map is skipped with an explanation. Older sessions retain their original
explicit-input behavior. Prepared sessions keep their verified inventory and do
not search neighboring files again when opened.
The reader checks the inventory before loading. Inputs are copied from stable
regular files; symlink inputs, duplicate JSON fields, existing output paths,
oversized files, and mismatched identities fail without publishing a partial
session. Publication uses an atomic rename that cannot replace an existing
directory. Bounds are 512 MiB for the ELF, 128 MiB each for console and trace,
64 MiB for the map, 8 MiB for a layout, 32 MiB for each snapshot or journal, up to
128 snapshot files, and 1 GiB total retained content.

A hash-bound package proves which files the viewer used. An unbound console log
still relies on the producer to select the firmware that actually ran; packaging
does not turn that assertion into target attestation. The original capture and
source-attribution warnings remain visible in the converted trace.
